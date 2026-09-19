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

## Phase 2 — Wiring the real ocean graph into routing (`routing/graph_adapter.py`)

**Schema mismatch caught before wiring anything together, not after:** `build_ocean_graph()` and
`CalmWaterAStar`/`YenKShortestPaths` were built and tested independently against their own small
synthetic fixtures, and their interfaces don't actually match:

1. `build_ocean_graph()` returns an **undirected** `nx.Graph`; the routing classes call
   `graph.successors()` / `graph.in_edges()` / `graph.out_edges()`, which only exist on
   `nx.DiGraph` — would have raised `AttributeError` the moment the real graph was passed in.
2. `build_ocean_graph()`'s nodes are bare H3 cell-ID strings with no `lat`/`lon` attributes (only
   edges carry geodesic data) — `CalmWaterAStar._heuristic()` reads `graph.nodes[node]["lat"]`,
   so this would `KeyError` immediately, the same failure shape as the node-attribute bug caught
   earlier in `k_shortest.py`, but this time a genuine interface gap between two modules rather
   than a bug in either one.
3. A naive `nx.Graph.to_directed()` (the obvious fix for #1) would copy each undirected edge's
   attribute dict verbatim onto both resulting directed edges — including
   `bearing_cell_to_nbr_deg`/`bearing_nbr_to_cell_deg`, whose meaning depends on which endpoint
   happened to be the outer-loop `cell` when `build_ocean_graph()` first added that edge
   (iteration-order-dependent, not part of that function's documented contract, and not safe to
   infer from outside it).

**Fix:** `to_routable_digraph()` builds the directed graph explicitly — sets `lat`/`lon` on every
node from `cell_centroid()`, and for each undirected edge adds two directed edges with a single
`bearing_deg` per direction, recomputed fresh via `pyproj.Geod` rather than trusting the
undirected edge's two stored bearing fields. `dist_nm` is copied as-is (direction-independent).

**Tests:** 8/8 passing, `test_graph_adapter.py`, run against a real `build_ocean_graph()` output
(synthetic land fixture, same style as `test_graph_builder.py`) — not just against the adapter's
own hand-built fixtures. Checks: result is a `DiGraph`; every node's `lat`/`lon` matches
`cell_centroid()`; every undirected edge becomes exactly two directed edges with matching
`dist_nm`; `bearing_deg` matches an independently-recomputed `Geod.inv()` call per direction (the
actual point of this module); reverse-direction bearings are ~180° apart, not equal. The last two
tests are the real proof: they feed the adapted graph directly into `CalmWaterAStar.solve()` and
`YenKShortestPaths.find_k_paths()` and confirm a real route comes back with no `KeyError`/
`AttributeError` — the exact failure this module exists to prevent. Project-wide: 77/77 passing.

**Region scope decision:** `scripts/build_real_ocean_graph.py` targets the Rotterdam <-> Ceyhan
corridor first (bbox `west=-8, south=33, east=38, north=55`, resolution 3 — matching the Phase 2
benchmark's scale) rather than a bbox covering all 16 ports in `ports.yaml`. Those 16 ports span
essentially the whole globe (Rotterdam to Singapore to Houston to Santos); one bbox covering all
of them would be close to a whole-globe H3 build, directly contradicting `h3_ocean_grid.py`'s own
documented design rationale for being region-scoped in the first place. Rotterdam <-> Ceyhan was
chosen because it's a real MR2 product-tanker trade lane and it exercises the Dover Strait /
Gibraltar Strait land-bridge pruning logic already built and unit-tested on synthetic data — a
meaningful first real-data test, not just the cheapest possible one.

**Not yet done / next phase's problem:** `scripts/build_real_ocean_graph.py` has not actually been
run yet — this sandbox has no route to the Natural Earth CDN (confirmed: `naciscdn.org` is
blocked by the outbound proxy here, same limitation noted in the Phase 2 graph-builder benchmark
entry above), so the first real run of this exact code path happens on the user's machine. Read
its printed stats (ocean cell count, edges pruned as land-bridges, corridor count found) before
trusting the output, same discipline as every other phase.

**Update — first real run, and what it actually took:**

- Resolution 3 (`west=-8..east=38, south=33..north=55`): confirmed the coarseness concern above —
  Rotterdam and Antwerp snapped to the same H3 cell.
- Resolution 4, same bbox: better, but left the North Sea/Channel split from Gibraltar/the Med —
  382 vs. 1125-node components, ports on opposite sides. Added the step 6b connectivity
  diagnostic (component sizes, per-port component membership, per-component lon/lat footprint,
  closest cross-component cell pair) specifically so the next failure would say *why*, not just
  "no path found."
- Resolution 5 with a wider bbox (`west=-15, south=30, east=40, north=56`) is what actually
  worked: one dominant component (17692 nodes) containing Rotterdam, Gibraltar and Ceyhan
  together, enough Atlantic water west of Iberia for the route to round Cape Finisterre / Cape St.
  Vincent without clipping the bbox edge. `BBOX`/`RESOLUTION` in the script now reflect this.
- Antwerp still comes back as an isolated singleton component even at resolution 5 — the Scheldt
  estuary is narrower than one hex edge at this resolution, and `snap_ports_to_ocean_cells()` only
  checks "is this cell's centroid ocean," with no visibility into `build_ocean_graph()`'s
  land-bridge pruning, so a technically-ocean cell can still end up with zero usable edges. Fixed
  with a step 6c: any port anchor outside the graph's dominant weakly-connected component gets
  re-snapped to the nearest cell that *is* in it, with a distance printed and a loud warning if the
  re-snap is implausibly large (>50 nm) — re-snapping silently would be worse than the original
  bug, since it'd produce a route that looks fine but starts somewhere the port doesn't actually
  reach by water.

**Diversity problem, found on this same real run:** with the corridor connected, `k=3` came back
with 3 "different" corridors that had identical calm-water time and identical fuel-optimized cost
— Yen's algorithm was technically doing its job (finding the k lowest-cost loopless paths) but on
a fine hex grid the lowest-cost alternatives to a shortest path are frequently a one-hex lateral
shift with negligible cost difference, not a genuinely different navigational choice (e.g. north
vs. south of an obstacle). See the `k_shortest.py` diversity-feature entry above/below for the fix
(`enforce_diversity=True`), now wired into this script's step 8 smoke test since this is the exact
case that motivated the feature.

## Phase 2 — Corridor diversity in Yen's k-shortest-paths (`routing/k_shortest.py`)

**Problem:** the real-data run above surfaced a genuine failure mode, not a code bug — Yen's
algorithm finds the k lowest-cost loopless paths, which on a fine hex grid are frequently
near-duplicates of each other (a one-hex lateral detour with negligible cost difference), not
genuinely distinct navigational choices. For the Pareto front this feeds in Phase 5 to mean
anything, the k corridors need to actually represent different trade-offs (e.g. routing north vs.
south of an obstacle, offshore vs. hugging the shelf), not the same track sampled three times.

**Design, opt-in via `enforce_diversity=False` by default (zero behavior change unless asked for):**

1. **Corridor exclusion buffer.** Once a corridor is accepted into the result set, every node
   within `diversity_buffer_hops` graph-hops of any node on that path (`nx.ego_graph`, default 1
   hop) is marked "penalized." Any candidate path touching a penalized node has its search-time
   cost multiplied by `1 + diversity_penalty_factor` (default 0.20, i.e. +20%) — this biases Yen's
   spur search away from that corridor's neighborhood without excluding it outright (a route that
   *must* pass through a strait still can).
   - Used graph-hop distance (`nx.ego_graph`), not H3's `k_ring`/`grid_disk` as originally
     proposed, so `k_shortest.py` stays generic and testable against small synthetic non-H3 graphs
     (its existing test style) rather than taking a hard H3 dependency. On an H3 hex grid the two
     are equivalent for hop radius 1 since each hex's graph neighbors are its `k_ring(cell, 1)`
     neighbors by construction.
2. **Jaccard similarity gate.** Before a candidate is promoted into the accepted set, it's checked
   against every already-accepted path: `|P1 ∩ P2| / |P1 ∪ P2| < diversity_jaccard_threshold`
   (default 0.80) against *all* of them, not just the most recent. Candidates that fail are
   skipped (not discarded from the heap — Yen's keeps generating further spurs) until a genuinely
   diverse one is found or the candidate heap is exhausted.
3. **Graceful degradation.** If no sufficiently diverse candidate remains, `find_k_paths()` returns
   fewer than k paths rather than either erroring or silently accepting a near-duplicate — same
   pattern already used elsewhere in this project (e.g. speed optimizer's infeasible-deadline
   check) of failing visibly rather than returning something that looks fine but isn't.

**Refinement beyond the original proposal — search cost vs. reported cost must never mix.** The
+20% penalty exists purely to steer *which* path Yen's search finds next; it must never leak into
the cost the corridor is reported and compared with, or corridors would look artificially more
expensive than they actually are just for being near an earlier pick, corrupting exactly the
Pareto-front comparison this feature exists to make meaningful. So the penalized evaluator
(`_search_cost_evaluator`) is used only inside the pathfinding search itself; every corridor's
`total_cost` actually reported is always recomputed via the original, unpenalized
`cost_evaluator`, replayed against the real path with accumulated time state
(`_compute_path_cost_and_time`) — same mechanism already used to fix the `root_path_cost` bug
earlier in this file. Verified directly:
`test_diversity_enabled_still_reports_true_cost_not_penalized_cost`.

**Bug found and fixed during implementation:** the first version recomputed a candidate's true
cost by replaying the *entire* candidate path (root + spur) through `_compute_path_cost_and_time`.
But that call happens while Yen's has temporarily removed the root path's nodes from the graph (a
core part of how it forces the spur search to explore alternatives) — replaying the full path hit
those removed nodes and raised `KeyError`. Fixed by recomputing only the spur segment (whose nodes
were never removed) and adding it to the root segment's cost, which was already computed safely
before node removal. Caught by the full regression suite (7 pre-existing tests failed) before any
new diversity-specific test was even run — same "run the whole suite after every change" discipline
that's caught every other bug in this project.

**Tests:** 8 new tests added to `test_k_shortest.py` (85/85 project-wide, zero regression) against
a hand-built graph with two near-duplicate paths (Jaccard 0.6 against each other) and one
genuinely distinct one (Jaccard 0.33 against either): Jaccard computation on known sets;
`_is_diverse_enough` threshold behavior; the search evaluator penalizes only buffered-node edges
and leaves others untouched; `_grow_penalty_buffer` correctly expands to 1-hop neighbors; enabled
end-to-end, the two near-duplicates collapse to one accepted path (pairwise Jaccard checked
directly against the threshold); the true-cost-not-penalized-cost property above; graceful
degradation to fewer than k when no diverse alternative exists; and `enforce_diversity` defaults
to `False` with identical output to the pre-feature behavior.

**Not yet done:** not yet exercised against the real ocean graph (`enforce_diversity=True` is now
wired into `scripts/build_real_ocean_graph.py`'s step 8 smoke test, but that script hasn't been
rerun since); the diversity threshold defaults (`0.20`/`1`/`0.80`) are reasonable starting points,
not yet tuned against how corridors actually look on the real Rotterdam↔Ceyhan grid.

**Update — validated against the real ocean graph:** rerun after the diversity feature above and
the script's bbox/resolution sync (`west=-15..east=40, south=30..north=56`, resolution 5). All 4
ports resolved, Antwerp's step 6c re-snap fired again (30.5 nm, under the 50 nm warn threshold —
consistent across runs). `k=3` with `enforce_diversity=True` returned 3 corridors with pairwise
Jaccard similarity 0.693–0.776 (all below the 0.80 acceptance threshold, added as a printed
diagnostic to the script specifically so this could be checked directly rather than inferred from
cost alone) — genuinely distinct node sequences (387/388/390 nodes), with total cost close but not
identical (228.6–230.4h, 281.4–283.7t). That's expected, not a residual bug: this corridor has no
real chokepoint choice besides Gibraltar itself (which every route must cross), so "diversity" here
is mostly mild lateral spread across open Atlantic/Med water — nothing forces a large cost gap
between alternatives the way a genuine north/south-of-an-island choice would. Confirms the feature
composes correctly with real data, closing out Phase 2 end-to-end.

## Phase 3 — Real CMEMS weather wired into the speed-profile optimizer (`weather/weather_field.py`)

**What this closes:** `speed_profile.py`'s own docstring flagged this as pending — `calm_weather_lookup()`
there is an explicit placeholder ("no weather data available", never to be read as a forecast), and
Phase 1's CMEMS ingestion (`cmems_client.py`) only pulls raw NetCDF subsets to disk with no lookup
interface on top. `weather_field.py` is that missing adapter: `(u, v, arrival_time_hours) ->
(wave_height_m, headwind_knots)`, matching `optimize_speed_profile()`'s existing `WeatherLookup`
type exactly, so it's a drop-in replacement for `calm_weather_lookup` with no change to
`speed_profile.py` itself.

**Scope decision — weather feeds the speed optimizer, not the corridor search.** Chose to wire
real weather into `optimize_speed_profile()` only, leaving `CalmWaterAStar`/`YenKShortestPaths`
on the static calm-water time evaluator they already use. This isn't a shortcut — it's the same
Phase 1/Phase 2 split already validated end-to-end (see the Phase 2 speed-profile entry above:
"proving the Phase 1 (static corridor generation) / Phase 2 (dynamic weather-fuel evaluation) split
actually composes as designed"), extended rather than abandoned now that the weather is real
instead of synthetic. `CalmWaterAStar`'s heuristic is only admissible under a time-based,
weather-independent edge cost (its own docstring is explicit about this); making the search itself
weather-aware would mean re-deriving that admissibility argument under a moving weather field, a
meaningfully harder problem this phase doesn't need to solve to deliver its actual goal — corridors
generated once, geometrically, then each one's fuel/time re-evaluated (and, per-leg speed,
re-optimized) against real conditions.

**Design:**
1. `WeatherField` wraps the two opened CMEMS `xarray.Dataset` objects (wave height, wind
   components) and samples both via nearest-neighbor in space and time — not interpolated.
   Chosen deliberately: CMEMS's ~0.08–0.125° native grid resolution is already fine relative to
   this project's H3 resolution-5 cells (~0.08°), and linear interpolation risks NaN propagation
   across any land/coastline mask edge in the source data, which nearest-neighbor avoids.
2. Converts wind from CMEMS's native m/s to the knots this project's fuel model and vessel speeds
   use throughout (`vessel_profiles.yaml`, `resistance_model.py`) — one conversion, done once,
   inside `WeatherField.sample()`, so nothing downstream has to remember to.
3. `make_weather_lookup(digraph, weather_field, voyage_start_time)` is the actual adapter: samples
   weather at a leg's departure node and absolute departure time, then projects the sampled wind
   vector onto that leg's real travel bearing (read off the edge's `bearing_deg`, set by
   `graph_adapter.to_routable_digraph()` per direction — not a naive undirected copy, per that
   module's own fix) via the already-existing `resistance_model.relative_headwind_component()`.
   Mirrors, rather than reinvents, `optimize_speed_profile()`'s own documented per-leg-at-departure-time
   sampling simplification.
4. NaN samples raise `ValueError` immediately rather than substituting a fallback — a NaN this
   deep means the query point is outside the fetched field's actual coverage (bad node lat/lon, or
   a fetch window/bbox that didn't cover the corridor), which is worth investigating, not masking.
   Same "fail visibly rather than return something that looks fine but isn't" pattern used for
   Yen's diversity graceful-degradation and the speed optimizer's infeasible-deadline check.

**Tests:** 12 new tests, `test_weather_field.py`, using small in-memory `xarray.Dataset` fixtures
with CMEMS's actual confirmed variable/coordinate names and units (not a live pull — this sandbox
has no route to CMEMS's servers either, same limitation as Natural Earth). Closed-form checks, not
just "does it run": exact wave-height readback at a known grid point; m/s->knots conversion
verified against the exact `MS_TO_KNOTS` constant; nearest-neighbor selection confirmed against a
query point/time deliberately offset from two different grid points/timesteps; NaN in either
dataset raises with a clear message; missing variable/coordinate names raise with the actual
dataset's variable list surfaced (a genuinely wrong CMEMS assumption would fail loud, not silent);
the departure-time-vs-arrival-time-hours wiring itself verified by giving two different absolute
times distinguishable wave heights and confirming the right one gets picked; headwind computed
via `make_weather_lookup()` end-to-end checked two ways — a pure tailwind case asserted exactly
zero (per `relative_headwind_component()`'s documented clip), and a pure headwind case asserted to
equal the wind speed exactly, cross-checked against calling `relative_headwind_component()`
directly with the same inputs (not just "some non-zero number came back"). Project-wide: 97/97
passing, zero regression.

**Not yet done / next phase's problem:** not yet run against a real CMEMS pull — `scripts/run_weather_aware_speed_profile.py`
fetches over the corridor's bbox for a fixed historical window (`VOYAGE_START = 2024-06-01`,
16-day margin) since the wind dataset (`cmems_obs-wind_glo_phy_my_l4_0.125deg_PT1H`) is the
multi-year REPROCESSED product, not near-real-time — "now" is very likely not covered, a
limitation already flagged in `cmems_client.py`'s own module docstring, not new to this phase.
First real run happens on the user's machine; if the fixed window falls outside either dataset's
actual coverage, `WeatherField.sample()`'s NaN check will say so explicitly rather than silently
returning zero. The script prints a calm-water-baseline-vs-real-weather-aware comparison per
corridor specifically so a real run's output is checkable against expectations (weather should
increase fuel burn, never decrease it, given the fuel model's structure), not just "it ran".

**Update — first real run hit a Windows HDF5/netCDF4 DLL conflict, fixed by removing the local
file round trip entirely, not by patching the local install.** `fetch_wave_field()`/
`fetch_wind_field()` (Phase 1) download a local `.nc` file via `copernicusmarine.subset()`; reading
it back required `netCDF4` (or the `h5netcdf` fallback added to `weather_field.py`'s
`_open_dataset_with_fallback()`), and on this user's Windows conda env neither worked:
`netCDF4`'s compiled extension failed to load (`DLL load failed while importing _netCDF4`) even
after a clean `--force-reinstall`; installing `h5netcdf` as a fallback engine then failed too,
because the CMEMS *download* step itself (an internal `xarray.Dataset.to_netcdf()` call inside
`copernicusmarine`) started picking `h5netcdf` as its write engine once installed, and `h5py`
(needed by `h5netcdf`) failed its own import for the same underlying reason; a further
`--override-channels --force-reinstall` of the entire HDF5 stack from conda-forge (`hdf5`,
`libnetcdf`, `netcdf4`, `h5py`, `h5netcdf` together, with `channel_priority strict`) *still* hit the
identical `h5py` import failure. Three successive attempts to patch the local install each moved
the same DLL conflict to a different package rather than resolving it — a real, unresolved Windows
conda environment problem, not a code bug, and not something fixable by guessing at more conda
installs one at a time.

**Fix:** stopped trying to make local HDF5/netCDF4 work at all. `copernicusmarine.open_dataset()`
streams a subset directly from CMEMS's remote store into an in-memory `xarray.Dataset` via
zarr/fsspec — a pure-Python path with no local HDF5/netCDF4 dependency whatsoever. Added
`cmems_client.open_wave_field()`/`open_wind_field()` (calling `open_dataset()`, then `.load()` to
pull the whole bbox/time-window subset into memory in one transfer, so `WeatherField.sample()`'s
repeated per-leg point queries during SLSQP's iterative solve never trigger repeat network
round-trips) and `weather_field.open_weather_field_remote()` as the one-call entry point
`run_weather_aware_speed_profile.py` now uses instead of the fetch-then-open path. The
local-file functions (`fetch_wave_field()`/`fetch_wind_field()`, `open_weather_field()`,
`_open_dataset_with_fallback()`) are left in place — legitimate for anyone with a working local
HDF5 install who wants the files cached to disk — but the module docstring and this script now
point at the remote path as the default, specifically because it sidesteps a real, encountered
failure mode rather than one that's only theoretical.

**Trade-off, stated plainly:** the remote path re-fetches over the network on every script run
(no local disk cache), and needs network access every time rather than just the first time. Worth
it here — correctness and actually running beats a cache that only helps once the local install is
fixed, which may not happen. If the HDF5 DLL conflict gets resolved on this machine later (a fresh,
conda-forge-only environment built from scratch is the most likely fix, not attempted here since
the remote path made it unnecessary), the local-file functions are still there to switch back to.

**Update — second real run got past the network/HDF5 issue cleanly, then hit a genuine coastline
mismatch between two different coastline sources, not a bug.** Every corridor's very first leg
(near the Rotterdam/Antwerp approach) sampled a NaN wave height. Cause: this project's H3 ocean
grid is built from Natural Earth coastline polygons (`land_mask.py`), while CMEMS's wave model has
its own, independently-drawn land mask at its own resolution — an H3 cell can be legitimately
"ocean" by Natural Earth's coastline and still fall inside CMEMS's masked-out near-shore band,
especially at river-delta/estuary port approaches (the same category of place that caused
Antwerp's graph-connectivity gap in Phase 2 — a recurring pattern: real geospatial datasets don't
agree with each other exactly at coastlines, and every phase that's touched a coastline so far has
had to handle that explicitly rather than assume it away).

**Fix:** `WeatherField` now builds a KD-tree (`scipy.spatial.cKDTree`) of grid points with real
(non-NaN) data, once per field at construction time — not searched fresh on every call. When
`sample()`'s exact nearest point is NaN, it looks up the nearest point on that tree and **re-queries
it at the actual requested time** (not the reference timestep the tree was built from, since CMEMS's
land mask is static across time but there's no reason to assume every ocean cell has data at every
timestep) via `_select_with_fallback()`. Still raises `ValueError` — never silently substitutes
zero — if no valid point exists anywhere in the field, or if even the fallback point turns out NaN
at that specific time (a real, different failure worth its own distinct error message, not silently
conflated with the coastline-mismatch case). Prints a one-line note the first time a given fallback
location is used (not once per call — would be noise given the same near-shore leg gets sampled
repeatedly across SLSQP's iterative solve), so the substitution is visible without flooding output.

**Tests:** 4 new (`test_sample_falls_back_to_nearest_valid_point_when_exact_point_is_nan`,
`..._reraises_the_actual_requested_time_not_the_reference_time`,
`..._raises_when_fallback_point_is_also_nan_at_the_requested_time`,
`..._prints_a_note_once_per_location`), plus the 2 pre-existing NaN tests reworked to use
genuinely all-NaN datasets (the only case where no fallback is possible and a raise is still the
correct outcome — with the old NaN-at-one-point fixtures, the new fallback logic would legitimately
find a nearby valid point instead of raising, which is the fix working as designed, not the test
being wrong). Used a 3x3 non-uniformly-spaced grid fixture (`LATS3`/`LONS3`) rather than the
existing 2x2 one specifically so "nearest valid point" is unambiguous — a symmetric 2x2 grid ties
two neighbors at equal distance from any corner, which would make the fallback's actual choice
implementation-dependent rather than a real assertion. 101/101 project-wide, zero regression.

**Update — real run got past the coastal-masking NaNs cleanly (11 fallback locations logged, all
legitimate strait/coastline chokepoints: Dover Strait, Ushant, Cape Finisterre, Gibraltar, the
Sicily Channel, Ceyhan's approach), then turned out to be far too slow to be usable: corridor 1's
weather-aware optimization alone took roughly two hours, with corridors 2 and 3 still pending.**
Root cause: `WeatherField.sample()`'s per-point lookups went through
`xarray.Dataset.sel(method="nearest")`, which carries real per-call overhead (label alignment,
Dataset wrapping/unwrapping) — fine occasionally, but `optimize_speed_profile()`'s SLSQP solve
calls the weather lookup on the order of (legs x solver iterations) times per corridor. For a
~387-leg corridor with numerical (finite-difference) gradients, that's on the order of several
million to tens of millions of calls — `.sel()`'s overhead at that volume, not CMEMS or the
network, is what turned into hours.

**Fix:** added `_FieldIndex`, a precomputed lookup built once per `WeatherField` construction (not
per sample) — a `scipy.spatial.cKDTree` over the FULL lat/lon grid for O(log n) nearest-space
lookup, plus `np.searchsorted` on the sorted time coordinate for O(log n) nearest-time lookup, both
resolving directly to plain numpy array indices (the underlying data was already loaded into memory
via `open_wave_field()`/`open_wind_field()`'s `.load()` call). `WeatherField.sample()` and the
NaN-fallback logic (`_select_with_fallback()`) now go through this index instead of `.sel()` —
same nearest-neighbor semantics, same fallback behavior, same public API, just without re-paying
xarray's per-call overhead on every leg. Benchmarked directly (not just asserted): on a synthetic
grid matching a real CMEMS pull's size (314 x 663 x 128, i.e. ~0.083° over the corridor bbox x
16-day/3-hourly time window), `sample()` averaged ~55 microseconds/call across 20,000 calls —
roughly a 20-40x speedup versus the `.sel()`-based path (estimated from the ~1-2ms/call `.sel()`
typically costs at this data size), which should bring a corridor's optimization down from multiple
hours to roughly single-digit minutes. Not yet re-verified against a live re-run of the full
corridor pipeline (next step); all 101 existing tests pass unchanged, confirming the same
nearest-neighbor/fallback/NaN-handling behavior, just computed faster.

**Update — full real run succeeded end-to-end after the speed fix, closing out Phase 3.** All 3
corridors optimized without incident (no multi-hour wait this time):

| Corridor | Calm-water | Weather-aware | Delta |
|---|---|---|---|
| 1 (387 nodes) | 281.4t / 406.3h | 290.5t / 406.3h | +9.1t / +3.2% |
| 2 (388 nodes) | 282.2t / 407.4h | 291.3t / 407.4h | +9.1t / +3.2% |
| 3 (390 nodes) | 283.7t / 409.6h | 292.9t / 409.6h | +9.2t / +3.2% |

Sanity-checked, not just "it ran": weather increased fuel in every corridor, never decreased it
(the fuel model's wave/headwind terms are structurally non-negative, so a decrease would have meant
something was wired backwards); transit time is identical to the calm-water baseline in every
corridor, which is the *correct* outcome, not a missed effect — with no deadline set,
`optimize_speed_profile()` always converges to each leg's minimum speed bound regardless of
weather (slower is always more fuel-efficient absent time pressure — the same closed-form property
`test_no_deadline_picks_minimum_speed_bound_exactly` verified in Phase 2), so weather shows up
purely in the fuel number, never the schedule. The ~3.2% uplift is consistent within a tight band
across all three corridors, sensible since they're lateral variants of the same route through the
same weather-exposed stretches (Bay of Biscay, Gibraltar Strait, the Sicily Channel).

This closes Phase 3: real CMEMS weather is now wired end-to-end into the speed-profile optimizer,
validated against real data, with the coastal-masking mismatch and the SLSQP-volume performance
issue both diagnosed and fixed rather than worked around. Open items for whenever they're
prioritized: `max_transit_hours` hasn't been exercised with real weather yet (would make the
transit-time-identical-to-calm-water result above actually change, since a binding deadline forces
speeds above the minimum bound, which is where weather-driven fuel cost and schedule pressure
would start trading off against each other); the fixed `VOYAGE_START = 2024-06-01` window means
this hasn't been tested across a season with meaningfully different sea states (winter Biscay/
Atlantic weather would be a much more interesting stress test than a June baseline).
