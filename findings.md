# M01 — NAVI-Opt: Findings & Design Decisions

Living log of design decisions, bugs caught, and honest results as the project develops.

## Phase 0 — Repo scaffold

- Airflow is run only via Docker (official `apache/airflow` image), never pip-installed into the
  conda env — mixing Airflow's strict dependency pins with OR-Tools/GeoPandas/Pyomo in one env
  is a well-known source of dependency hell, and Airflow only needs to see DAG files + `src/`
  via a mounted volume, not share a Python environment with the solver.
- Kafka runs in KRaft mode (no Zookeeper) — simpler single-node setup, fewer containers.
- pip was slow during environment setup due to a stale global `pypi.ngc.nvidia.com`
  `extra-index-url` (leftover from an earlier ML project) that doesn't resolve on this network;
  every package paid a 5-retry DNS-timeout tax before falling back to pypi.org. Removed from
  the global `pip.ini`.

## Phase 1 — H3 ocean grid, land masking, port data, CMEMS ingestion client

**Data sourcing decision (per project honesty policy):**
- Land mask: real, public Natural Earth 1:50m coastline polygons
  (naturalearthdata.com, official CDN mirror).
- Ports: real coordinates for ~15 major tanker ports (anchorage/approach positions, not exact
  berths — public knowledge, not proprietary).
- Wave height: real CMEMS data — `cmems_mod_glo_wav_anfc_0.083deg_PT3H-i` (VHM0, 3-hourly,
  GLOBAL_ANALYSISFORECAST_WAV_001_027).
- Wind: real CMEMS data — `cmems_obs-wind_glo_phy_my_l4_0.125deg_PT1H`
  (WIND_GLO_PHY_L4_MY_012_006, hourly, multi-year reprocessed — chosen over the near-real-time
  product so the historical window matches whatever wave-hindcast period is pulled).
- Exact in-file wind variable short names are deliberately NOT hardcoded in `cmems_client.py` —
  call `describe_dataset()` once interactively before wiring a fixed variable list into a
  pipeline run. CMEMS has renamed variables between dataset versions before; a hardcoded guess
  here would fail silently in a way that's hard to trace downstream. This will be resolved and
  the variable names pinned once real CMEMS credentials are available and `describe_dataset()`
  has actually been run.

**Design decisions:**
- Ocean grid generation is region-scoped (bbox → `h3.LatLngPoly` → `h3.polygon_to_cells`)
  rather than whole-globe. A tanker's operating area is a small fraction of Earth's surface;
  scoping keeps cell counts (and therefore Phase 2's graph size) tractable at finer H3
  resolutions, and avoids walking ~41k global res-3 cells just to throw most of them away as
  land.
- H3's native (lat, lng) argument/return order is isolated entirely inside
  `h3_ocean_grid.py` — every function in that module accepts/returns (lon, lat), matching
  shapely/GeoJSON/pyproj convention used everywhere else in the codebase. This is a deliberate
  boundary to stop a lat/lon mixup from silently propagating into Phase 2's graph or Phase 3's
  distance calculations — verified explicitly by `test_cell_centroid_returns_lon_lat_order`.
- `land_mask.py` exposes a `prepared_land()` wrapper (shapely `prepared` geometry) specifically
  because both the H3 ocean-cell filter (thousands of point-in-polygon checks) and Phase 2's
  upcoming edge-pruning step (one intersects() check per candidate graph edge) call this in a
  tight loop — an unprepared geometry would make both noticeably slower without changing
  correctness, so it's worth doing from the start rather than retrofitting later.
- Antimeridian-crossing bboxes are explicitly rejected with a clear error
  (`generate_ocean_cells_in_bbox` raises `ValueError` if `west > east`) rather than silently
  producing the wrong (inverted) region. Trans-Pacific lanes need two bbox calls unioned
  together — documented in the function's own docstring so this doesn't get rediscovered the
  hard way in Phase 2.

**Bug caught before shipping:**
- All three modules that compute a path relative to the project root
  (`download_natural_earth.py`, `land_mask.py`, `cmems_client.py`) initially used
  `Path(__file__).resolve().parents[4]`, which is one level too far up (lands in the parent of
  the project directory, not the project directory itself). Caught by `test_ports.py` failing
  with `FileNotFoundError` pointing at the wrong directory. Fixed to `parents[3]` and verified
  directly against `Path.parents` output for a file at that exact depth
  (`src/navi_opt/grid/ports.py`) before re-running the suite. All 9 tests pass after the fix.
  Documented here per the project's debugging-discipline standard: read the shape of the
  failure (a wrong-but-plausible-looking path, not a crash) before touching code, then verify
  the fix against ground truth rather than guessing again.

**Port-to-ocean-cell anchoring (`port_anchors.py`):**
- A port terminal sits right at the coastline, so its direct H3 cell (`h3.latlng_to_cell`) is
  frequently classified as land by the centroid-in-ocean filter. Added `snap_ports_to_ocean_cells()`:
  direct-hit fast path, then expanding `h3.grid_disk` ring search (k=2→6) filtered against the
  ocean-cell set, nearest candidate chosen by geodesic distance (`pyproj.Geod`, consistent with
  the rest of the project — not a raw haversine approximation). A port that can't be snapped
  within the ring cap raises `PortAnchorError` loudly rather than being silently dropped, since a
  missing anchor would otherwise surface much later as an unreachable graph origin/destination.
- Bug caught in the test suite itself, not the implementation: the first version of the
  "port too far from any ocean cell" test placed the port only ~106 km from open water, well
  within the ring cap's reach at H3 resolution 4 — so it wrongly snapped instead of raising.
  Fixed by moving that test case outside the region the ocean-cell set was built for, which is
  also the realistic real-world trigger for this error path.
- API correction applied before writing any code: `h3.geo_to_h3()` and `h3.k_ring()` are the
  removed v3 API — the installed `h3-py` v4 uses `h3.latlng_to_cell()` and `h3.grid_disk()`.
  Verified directly against the installed package rather than assumed.

**CMEMS variable names — confirmed against the live catalogue (2026-09-17):**
- Ran `scripts/describe_cmems_datasets.py` with real CMEMS credentials. Wave: `VHM0` (as
  expected). Wind: `eastward_wind`, `northward_wind` (CF standard names, as expected — no
  surprise renaming this time, but worth having checked rather than assumed, since CMEMS has
  renamed variables between dataset versions before). Both pinned into `cmems_client.py` as
  `WAVE_VARIABLES` / `WIND_VARIABLES` module constants.

**Tests:** 13/13 passing — `test_land_mask.py` (3), `test_h3_ocean_grid.py` (4), `test_ports.py`
(2), `test_port_anchors.py` (4, including the ring-expansion and error-path cases). Run against a
synthetic land polygon fixture, not the full Natural Earth download, so the suite stays fast and
network-independent; the real Natural Earth data is exercised manually via
`download_natural_earth.py` during actual development.

**Not yet done / next phase's problem:**
- CMEMS dataset IDs and variable names are confirmed; an actual `fetch_wave_field()` /
  `fetch_wind_field()` data pull (not just `describe()`) is still pending — that will happen
  naturally once the weather-tensor precomputation needs real data to precompute from.

## Phase 2 — Ocean graph builder (land-bridge edge pruning, benchmarked)

**Land-bridge pruning implemented as planned:** `graph_builder.build_ocean_graph()` adds an edge
between two H3-adjacent ocean cells only if the straight-line segment between their centroids
does not intersect `land_union` (via `land_mask.prepared_land()`). The check is factored into a
standalone `_segment_crosses_land()` so it's unit-testable independent of H3 grid adjacency.

**Test caught a flawed test, not a code bug:** the first version of the land-bridge test built a
"strait" as two separate landmasses with an open-water gap between them — but that gap IS
legitimately navigable water (a real strait), so nothing was ever pruned there and the test's own
sanity check (`edges_pruned_land_bridge > 0`) correctly failed, catching the flawed premise before
it shipped. Replaced with a direct construction: take a real pair of H3-adjacent cells (verified
via an unconstrained empty-land grid first), then build a thin land strip that crosses the
straight line between their two centroids without containing either centroid — confirmed by
assertion that both cells remain classified as ocean, then confirmed the edge is absent from the
built graph. This is a more reliable test than trying to approximate a real strait's geometry by
hand.

**STRtree/joblib question settled empirically, not by guessing:** benchmarked
`build_ocean_graph()` against a synthetic land geometry deliberately built *more* complex than
the real Natural Earth 1:50m dataset (~252k vertices across ~46k polygons, vs. the real
dataset's ~135k vertices), since the sandbox used for this benchmark can't reach Natural Earth's
CDN. Result, over a bbox covering the full North Atlantic + Mediterranean + North Europe:

| Resolution | Ocean cells | Candidate edges | Pruned (land-bridge) | Build time |
|---|---|---|---|---|
| 3 | 5,035 | 14,335 | 612 | 0.54s |
| 4 | 35,220 | 101,715 | 3,261 | 3.56s |

The plain prepared-geometry `.intersects()` loop is fast enough at this scale that STRtree and
`joblib`/multiprocessing parallelization (both floated as possible improvements) are not needed —
adding either would be complexity spent on a problem that doesn't exist at the scale this project
actually runs at. Documented here so the question doesn't get reopened without new evidence
(e.g. a genuinely global, very-fine-resolution grid, which this project doesn't build).

**Caching:** `save_graph()`/`load_graph()` use plain `pickle`, not `nx.write_gpickle()` —
confirmed that function (and `read_gpickle`) was removed in the installed NetworkX 3.6, not
assumed from memory of older NetworkX versions.

**Tests:** 17/17 passing — added `test_graph_builder.py` (4 tests: the land-crossing detector in
isolation, a clear-path negative case, the constructed land-bridge-pruning integration test, and
basic graph-shape properties).

**Directional bearing added to graph edges:** the graph is undirected, but bearing (needed for
headwind calculations) is direction-dependent — the bearing from cell A to cell B is not the
bearing from B to A (not even exactly 180° apart, due to ellipsoid geometry). Rather than pick
one direction arbitrarily or recompute geodesic azimuth at solve time, both directions' bearings
are stored as edge attributes (`bearing_cell_to_nbr_deg`, `bearing_nbr_to_cell_deg`), taken
directly from the same `GEOD.inv()` call already made for distance — `az_fwd`/`az_back` come free
from that one call, confirmed empirically (due-east forward azimuth = 90°, matching back-azimuth
from the destination = -90°, i.e. precisely the reverse bearing, not just `az_fwd + 180`).

## Phase 2 — Resistance / fuel-consumption model

**Implemented as planned:** `resistance_model.py` computes
`F(v, H, W_headwind) = a·v³ + b·v²·H + c·v·max(W_headwind, 0) + d` (tonnes/day), scaled by
`cargo_condition_factor` (laden/ballast) and converted to tonnes/hour via
`hourly_conversion_factor` — both read from `vessel_profiles.yaml`, never hardcoded, continuing
the pattern from Phase 0/1.

**Headwind, not raw wind speed:** wind resistance uses the component of wind directly opposing
the vessel's travel direction, not the raw wind speed magnitude — a beam wind or tailwind
shouldn't cost the same as a dead-ahead headwind. Computed via vector projection of the wind
vector (from CMEMS's native eastward/northward components) onto the vessel's heading, with the
heading itself taken from the new `bearing_cell_to_nbr_deg`/`bearing_nbr_to_cell_deg` edge
attributes (whichever matches the direction actually being traversed).

**Deliberate simplification, documented rather than silently baked in:** a tailwind is clipped to
zero added resistance, not modeled as *reducing* resistance below the calm-water baseline. A real
tailwind-reduces-drag effect exists physically, but modeling it credibly needs added-resistance
curves broken out by relative wind angle that no public source provides at a fidelity this
project can honestly claim — so the model treats "wind either hurts or does nothing" rather than
overclaim a benefit it can't back up. Same honesty standard as the synthetic-but-literature-
grounded fuel curves themselves.

**Validation-first API:** `fuel_rate_tonnes_per_day()` raises on a negative wave height, a
negative headwind (the caller should have clipped via `relative_headwind_component()` already —
a negative value reaching this function is a bug, not a valid input), a speed outside the
vessel's `speed_bounds_knots`, or an unrecognized cargo condition — rather than silently
producing a wrong number for a bad input.

**Tests:** 39/39 passing — added `test_resistance_model.py` (17 tests: wind vector conversion,
headwind projection at 0°/90°/45°/180° relative angles including the tailwind-clips-to-zero case,
fuel-rate monotonicity in speed/wave/wind, the cubic term's dominance at higher speed, laden vs.
ballast scaling matching the configured factor exactly, all four validation-error cases, and the
leg-level fuel/transit-time calculation including that the default path genuinely reads
`hourly_conversion_factor` from the YAML rather than a hardcoded fallback) and
`test_vessel_profiles.py` (5 tests covering the loader).

## Phase 2 — Routing (Yen's k-shortest / A*) — design decision + bugs caught before shipping

**Design decision: A* stays scoped to static (calm-water) cost; dynamic weather-fuel
optimization is Phase 2's MILP/NLP, not a second graph search.** An early draft named the A*
class `TimeDependentAStar` and threaded a `current_time_hours` argument through
`cost_evaluator`, implying it correctly handled dynamic, weather-dependent cost. It doesn't, for
two independent reasons:

1. Node settling is single-label (`best_g_cost` keyed only by node) — a label reaching a node at
   lower cost-so-far but later arrival time always evicts one that arrived earlier at higher
   cost, even when the later arrival means running straight into weather the earlier label would
   have dodged. Only safe when `cost_evaluator`'s output doesn't meaningfully depend on the time
   it's evaluated at (true FIFO/monotonic cost) — calm-water distance/time satisfies this
   trivially; dynamic weather-fuel cost does not.
2. `_heuristic()` returns a lower bound on **transit time** (great-circle distance ÷ v_max,
   hours), added directly into `f = g + h`. That's only a valid admissible bound when `g` itself
   accumulates time. If `cost_evaluator` returns fuel tonnes instead, `g + h` mixes tonnes and
   hours — not a lower bound on anything, voids A*'s optimality guarantee outright, silently.

Renamed to `CalmWaterAStar` to stop the name overselling a guarantee the settling logic doesn't
provide, and documented in both `a_star.py` and `k_shortest.py` that `cost_evaluator` must return
calm-water distance/time, never a dynamic fuel cost. This settles the Phase 1/Phase 2 split
originally proposed: Yen's (`k_shortest.py`) generates k geometrically diverse corridors over
static cost only; Phase 2's MILP/NLP evaluates dynamic weather-fuel cost along that small, fixed
set of corridors — a search space small enough that FIFO-unsafe settling isn't a risk, since
Phase 2 evaluates fixed paths rather than searching a new graph under time-dependent cost. A
genuinely time-dependent, FIFO-safe search (multi-label over `(node, arrival_time)`, plus a
fuel-based admissible heuristic) is explicitly out of scope unless a future phase needs a new
graph search under dynamic cost, not just cost evaluation along fixed corridors.

**Bugs caught in `k_shortest.py` before shipping:**
- `root_path_cost` was referenced without being computed (an earlier draft used
  `spur_res["total_cost"]` for the prefix cost, which is the spur segment's own cost, not the
  root path's) — candidate total costs would silently ignore the accumulated cost of the path
  prefix, breaking Yen's cost-sorting order. Fixed by `_compute_path_cost_and_time()`, which
  replays `root_path` through the same `cost_evaluator` with accumulated time state carried
  forward — not a static edge-weight sum, since cost per leg depends on time-of-arrival even in
  the calm-water case (start_time offset matters for anything that becomes genuinely
  time-dependent later).
- `from src.navi_opt.routing.a_star import ...` — inconsistent with every other module's
  `from navi_opt.X.Y import ...` convention (the `src` layout makes the installed package root
  `navi_opt`, not `src.navi_opt`); would have raised `ModuleNotFoundError` at runtime. Fixed.
- Node removal/restoration (the temporarily-remove-root-path-nodes step, standard to Yen's)
  captured and restored each removed node's incident edges but not the node's own attribute dict
  (`lat`/`lon`). `self.graph.add_node(node)` on restore recreated a bare node with no attributes,
  so the *next* spur search in the same `find_k_paths()` call would `KeyError` on `node['lat']`
  inside `CalmWaterAStar._heuristic()`. Caught by an end-to-end synthetic-graph run (single A*
  solve succeeded; the second spur search inside `find_k_paths` failed) — not caught by a
  standalone syntax check, since the bug only manifests once a node is actually removed and
  restored mid-search. Fixed by capturing `dict(self.graph.nodes[node])` before removal and
  passing it back via `self.graph.add_node(node, **node_data)` on restore; verified by asserting
  the graph's node attributes are bit-for-bit identical before and after a `find_k_paths()` call
  on a synthetic diamond graph, not just that no exception was raised.

**Tests:** 56/56 passing project-wide — added `test_a_star.py` (9 tests: `haversine_nm` symmetry
and a known closed-form quarter-great-circle distance, `solve()` preferring lower cost over fewer
hops on a diamond graph, unreachable/missing-node cases returning `None`, the `start_time_hours`
offset actually propagating into `arrival_time_hours`, and total cost matching an independently
recomputed sum of leg costs) and `test_k_shortest.py` (8 tests, including two written as direct
regressions for the bugs caught above: `test_every_candidate_total_cost_matches_independent_recomputation`
uses a graph deliberately built so one candidate spurs off a node with nonzero root-path cost
(`spur_idx > 0`) — exactly the case the `root_path_cost` bug got wrong — and asserts every
candidate's `total_cost`/`arrival_time_hours` matches an independent from-scratch recomputation;
`test_graph_is_left_unmodified_after_find_k_paths` and
`test_find_k_paths_is_safely_reusable_on_the_same_graph` assert the graph's node attribute dicts
are bit-for-bit identical before and after a call, and that a second `find_k_paths()` call
succeeds — exactly what the node-attribute-loss bug broke). Verified both regression tests
actually catch their bug, not just coincidentally pass: reintroduced each bug in a scratch copy
of `k_shortest.py` and confirmed the corresponding test fails (the node-attribute one reproducing
the exact same `KeyError: 'lat'` hit during manual end-to-end testing) before reverting.

**Not yet done / next phase's problem:** these tests cover `CalmWaterAStar` and
`YenKShortestPaths` in isolation against small synthetic graphs with hand-placed lat/lon — not yet
exercised against a real ocean graph (`build_ocean_graph()` output) or wired into any pipeline.
That integration is still pending; the speed optimizer that consumes these corridors is done (see
below).

## Phase 2 — Speed-profile optimizer (`optimization/speed_profile.py`)

**NLP, not MILP — and why:** `fuel_rate_tonnes_per_day` is cubic and strictly increasing in speed
for any speed > 0 (every `fuel_curve` coefficient in `vessel_profiles.yaml` is positive), and legs
are coupled only through cumulative arrival time (which decides which weather snapshot applies to
the next leg). That's a smooth, sequential nonlinear program — discretizing speed and time into
bins for a MILP formulation would throw away resolution the model doesn't need. Solved with
`scipy.optimize.minimize(method="SLSQP")` (already in `environment.yml`, no new dependency),
finite-difference gradients, bounds from `vessel.speed_bounds_knots`, and an optional nonlinear
inequality constraint for a total-transit-time deadline (e.g. a laycan window).

**Validation-first, same standard as `resistance_model.py`:** an infeasible deadline (tighter than
every leg run at the vessel's max speed in calm water) raises `ValueError` up front, with the
actual minimum-possible transit time in the message — rather than handing SLSQP an infeasible
problem and silently returning a constraint-violating "solution."

**Weather is injected, not fetched:** `weather_lookup: (u, v, arrival_time_hours) -> (wave_m,
headwind_kn)`, mirroring the `(u, v, current_t)` convention already used by `cost_evaluator` in
`navi_opt.routing`. This module has no CMEMS dependency of its own — Phase 1's
`fetch_wave_field()`/`fetch_wind_field()` pull is still pending. `calm_weather_lookup` (always
`(0.0, 0.0)`) is a named placeholder for "no weather field wired in yet," explicitly documented as
not a forecast, so it can't get mistaken for one downstream. Weather is sampled once per leg at
that leg's departure time (not midpoint, not re-sampled mid-leg) — a deliberate simplification
given legs are short relative to how fast weather fields change; documented in the module rather
than silently assumed.

**Reuses `fuel_tonnes_for_leg` directly**, not a reimplementation of the fuel math — verified by
`test_each_leg_fuel_matches_resistance_model_directly`, which recomputes a leg's fuel
independently via `resistance_model.fuel_tonnes_for_leg` with the optimizer's own chosen
speed/weather and checks it matches bit-for-bit.

**Tests:** 13/13 passing, added `test_speed_profile.py`. Several are closed-form checks, not just
"does it run": with no deadline, fuel's strict monotonicity in speed means the optimal speed is
provably exactly the vessel's minimum bound (`test_no_deadline_picks_minimum_speed_bound_exactly`,
and confirmed to hold even under heavy weather); with a binding single-leg deadline, the required
speed is `distance / deadline` in closed form
(`test_binding_deadline_forces_exact_required_speed_single_leg`); a symmetric two-leg corridor
under a binding deadline should split speed evenly by convexity, not arbitrarily
(`test_binding_deadline_on_symmetric_two_leg_corridor_splits_evenly`). Project-wide: 69/69 passing.

**End-to-end integration check (not a committed test, a one-off verification script — same
discipline as manually sanity-checking `fuel_rate_tonnes_per_day` before trusting it):** built a
6-node synthetic graph with real `dist_nm` edge attributes (via `haversine_nm`, as
`build_ocean_graph()` produces), ran Yen's (`k=3`) to get 3 geometrically diverse corridors ranked
by calm-water time, then ran the speed optimizer independently on each corridor against a shared
weather field with a storm concentrated near one node. Confirmed the corridor routed through the
storm came out correctly and substantially more expensive in Phase 2's fuel-optimized ranking
(63.85 t vs. ~28.3 t for the other two, which were nearly tied on both calm-water time and
fuel-optimized cost) — proving the Phase 1 (static corridor generation) / Phase 2 (dynamic
weather-fuel evaluation) split actually composes as designed, not just that each piece passes its
own unit tests in isolation.

**Not yet done / next phase's problem:** not yet run against a real ocean graph or real CMEMS
weather; `weather_lookup` sampling at leg-departure-time only (not midpoint/re-sampled) should be
revisited once real, fast-changing weather data is wired in, if legs turn out to span enough time
for that simplification to matter in practice.
