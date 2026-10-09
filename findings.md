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

## Phase 3 — Tight-laycan comparison: does weather trade off against schedule, not just fuel

**Motivation:** the no-deadline result above is real but incomplete — `optimize_speed_profile()`
always converges to the minimum speed bound absent time pressure, so weather could only ever show
up as extra fuel at a fixed transit time, never as a scheduling effect. The actually interesting
real-world question — "does bad weather force a reallocation of speed to still make a laycan?" —
needs a binding deadline to even be observable, since the resistance model's wave/headwind terms
scale with speed (`b·v²·H`, `c·v·max(W,0)`), making the marginal cost of going faster higher on a
rough leg than a calm one.

**Design:** `scripts/run_weather_aware_speed_profile.py`'s new Step 6 sets each corridor's deadline
at 15% margin over its fastest-possible flat-out transit (`total_dist_nm / V_MAX_KNOTS`) — tight
enough to force real per-leg speed variation, not a token constraint the optimizer can ignore — then
runs `optimize_speed_profile()` twice per corridor (calm-water, real-weather) with `max_transit_hours`
set to that deadline, and prints fuel/transit/success/per-leg speed range for both.

**Validated two ways before trusting it:**
1. Synthetic sanity check (ad-hoc, not a committed test): a hand-built 4-leg corridor (100nm/leg,
   400nm total) with one deliberately rough leg (4.0m waves, 20kn headwind) and the rest calm, run
   through this exact code path. Result: `calm speeds: [13.91, 13.91, 13.91, 13.91]` vs.
   `real speeds: [13.97, 13.74, 13.97, 13.97]` — the weather-aware optimizer slowed down specifically
   on the rough leg and sped up on the calm ones to still land on the same 28.75h deadline, cheaper
   than holding a uniform speed would be. Confirms the reallocation mechanism works as designed
   before spending real CMEMS-fetch time/compute on it.
2. Real run against all 3 Rotterdam↔Ceyhan corridors and real June 2024 CMEMS weather:

   | Corridor | Deadline | Calm-water | Real-weather | Delta | Speed range (calm → real) |
   |---|---|---|---|---|---|
   | 1 | 262.9h | 569.3t / 262.9h | 580.4t / 262.9h | +11.1t / +1.9% | 13.9–13.9kn → 13.8–14.0kn |
   | 2 | 263.6h | 570.8t / 263.6h | 581.9t / 263.6h | +11.1t / +1.9% | 13.9–13.9kn → 13.8–14.0kn |
   | 3 | 265.0h | 573.9t / 265.0h | 585.0t / 265.0h | +11.2t / +1.9% | 13.9–13.9kn → 13.8–14.0kn |

**Sanity-checked, not just "it ran":** every corridor's achieved transit time lands exactly on its
deadline in both scenarios (`success=True`, transit == deadline to the printed decimal) — confirms
the deadline constraint binds as expected rather than being slack, consistent with going faster than
required only burning fuel for nothing. The calm-water run's speed range collapses to a single value
(13.9kn on every leg) — expected, since with no weather to react to and a shared deadline, the
convex fuel cost is minimized by splitting speed evenly across legs (same closed-form property
`test_binding_deadline_on_symmetric_two_leg_corridor_splits_evenly` verified in Phase 2, extended
here to a real ~387-leg corridor). The real-weather run visibly widens that range (13.8–14.0kn) —
the reallocation signal the synthetic test predicted, now confirmed on real data: speed comes down
on weather-exposed legs and goes up elsewhere to still hit the same deadline.

**The fuel delta shrank versus the no-deadline case (+1.9% here vs. +3.2% with no deadline) — worth
noting since the direction wasn't obvious a priori.** Both scenarios in this comparison already run
close to `V_MAX_KNOTS` (13.9kn against a 16kn max, a much smaller margin than the no-deadline case's
minimum-speed-bound baseline), so there's less room for the optimizer to trade speed between legs,
and less of a spread between the cheapest and most fuel-efficient way to spend the same time budget.
This isn't a contradiction of the no-deadline result — it's a different, and now directly measured,
regime: near minimum speed, weather is almost pure added fuel cost (+3.2%, no scheduling response
possible); near a binding deadline, some of that cost gets absorbed by reallocating speed instead of
paying it outright, which is exactly why the percentage is smaller here, not evidence the effect is
weaker than expected.

This confirms the open item flagged at Phase 3's close: weather now demonstrably competes against
schedule, not just fuel, when a deadline is tight enough to bind — closing that item with real,
checked data rather than leaving it as a hypothesis.

## Phase 4 — Pareto trade-off analysis (`optimization/pareto.py`)

**Why epsilon-constraint, and why it needed almost no new solver:** `optimize_speed_profile()`
already minimizes fuel subject to `max_transit_hours` — that IS the epsilon-constraint method
applied to the duration objective. Sweeping `max_transit_hours` and collecting (fuel, duration)
pairs traces the frontier for free, reusing Phase 2's optimizer rather than building a second
solver. This module adds three things on top: a weather-risk objective computed from data the
optimizer already produces (no new sampling), the sweep loop across epsilon values and across the
k diverse corridors from Phase 2's diversity feature, and non-dominated filtering across all three
objectives at once — a corridor/epsilon combination optimal on fuel-vs-duration can still be
weather-riskier than a different combination at a similar cost, which a 2D trade-off alone would
miss.

**Weather-risk metric, chosen and documented rather than assumed:** significant-wave-height
exposure in metre-hours (`sum(leg.wave_height_m * leg.transit_hours)`), read directly off
`SpeedProfileResult.legs` (already populated per solve — no new weather sampling needed). Chosen
over a per-leg-maximum ("worst single moment") because time-weighted exposure is the more
actionable signal for voyage planning ("how long does this route spend in rough water"), and it's
directly derivable from data the optimizer was already computing. A per-leg-maximum metric answers
a different, legitimate question and isn't implemented here.

**Bug caught before delivery, not after — a real numerical-tolerance issue, not a logic error:** a
synthetic two-corridor sanity check (one corridor always calm, one uniformly rough over the same
route/distances — the rough one should be dominated everywhere) initially showed some rough-corridor
points surviving on the frontier. Root cause: at each corridor's flat-out floor epsilon, both
corridors' speed is forced to the same value by the deadline, but two *independent* SLSQP solves
landing on that same value don't produce bit-identical floats — observed noise was ~7.5e-11 hours
between the two solves' `total_transit_hours`. `_dominates()`'s original exact `<=` comparison
treated that sub-nanosecond-scale noise as a genuine schedule difference, which was enough to make
the rough corridor's clearly-worse point (same effective duration, +4.75t fuel, +62.5 m·h risk)
register as "not dominated." Fixed with per-objective tolerances
(`FUEL_TOL_TONNES`/`DURATION_TOL_HOURS`/`RISK_TOL_M_HOURS`, all `1e-3` — far above observed solver
noise, far below any difference this project's fuel model would ever call meaningful). Re-ran the
same synthetic check after the fix: 0/8 rough-corridor points survive on the frontier, exactly as
expected. Added `test_dominance_survives_sub_tolerance_floating_point_noise` as a direct regression
using the actual noisy floats observed, not a hypothetical case.

**Tests:** 14 new (`test_pareto.py`), 115/115 project-wide, zero regression. Closed-form checks, not
just "does it run": `weather_risk_m_hours` against hand-built legs; `default_epsilon_sweep`'s floor
and spacing against the known distance/v_max; a sweep's fuel is verified non-increasing as epsilon
relaxes (a corridor's optimizer always has the tighter solution's speeds available as a strictly
looser-feasible option, so fuel can never go up); infeasible epsilons are skipped, not raised;
hand-built dominance cases (a point dominated on all three objectives is removed; two genuinely
non-dominated trade-off points both survive; ties on all three survive both; dominance is checked
across the pooled set, not per-corridor); the floating-point-noise regression above; and an
end-to-end composition check on a corridor with one rough leg confirming every retained point's
`weather_risk_m_hours` traces back to exactly that leg's `wave_height_m * transit_hours`.

**Not yet done / next phase's problem:** `scripts/run_pareto_sweep.py` (real CMEMS weather, all 3
Rotterdam↔Ceyhan corridors, 10-point epsilon sweep per corridor, pooled frontier written to
`data/processed/pareto_frontier_rotterdam_ceyhan.csv`) has not yet been run against real data —
same discipline as every other phase, first real run happens on the user's machine, read its
printed output before trusting it. The sweep's weather field is fetched once and shared across all
corridors/epsilons (same `open_weather_field_remote()` call as Phase 3), so this should run in
minutes given the `_FieldIndex` performance fix already in place, not the multi-hour wait Phase 3
hit before that fix existed.

**Update — first real run appeared stuck (3+ hours, no output) — real gap in the delivered script,
not (as far as could be verified) a wrong performance estimate.** Before delivery this was checked
only on small synthetic graphs for correctness, never timed at the real corridor's scale (~390
legs) or under a realistic worst-case SLSQP iteration budget — the same category of mistake as
Phase 3's original `.sel()` performance bug, caught again here because the discipline of "benchmark
before trusting a performance claim" wasn't applied a second time before shipping.

Benchmarked directly once the user reported the stall, using a synthetic ~390-leg corridor and the
real `fuel_tonnes_for_leg`/`optimize_speed_profile` code paths (not assumed): one constrained solve
at SLSQP's default 100-iteration cap measured at 1.3s (11 iterations, 4257 function evaluations) to
converge from a cold start; a single full 390-leg objective evaluation (real
`fuel_tonnes_for_leg` calls, not a stand-in) measured at 0.56ms, giving a worst-case projection of
~2.4s/solve even at the full 100-iteration budget, ~1-2 minutes for the whole 30-solve sweep. This
does not explain a 3-hour stall by any plausible multiple — Windows overhead, real (rather than
random-noise) weather-field structure, or general machine speed differences could plausibly cost a
few times more, not 100x+ more. The true root cause on the user's specific machine/run could not be
identified from this sandbox (no way to attach to or reproduce the live process), so this is
reported as an unresolved discrepancy, not a diagnosed root cause — consistent with this project's
standard of not asserting a specific explanation without evidence for it.

**What was fixed regardless, since it was a real gap either way:** the script gave zero output
during a corridor's entire 10-epsilon sweep (only printing once all 10 finished), making a normal
multi-minute wait and a genuine multi-hour hang indistinguishable to whoever's watching it run — a
real design miss, independent of whatever the actual root cause of the 3-hour stall turns out to
be. Fixed two ways:
1. `sweep_epsilon_constraint()` gained an optional `on_progress` callback, invoked after every
   single epsilon attempt (feasible or not) with `(index, epsilon_hours, point_or_None,
   elapsed_seconds)` — opt-in, `None` by default, zero behavior change for existing callers/tests.
   `run_pareto_sweep.py` now passes one that prints a line per epsilon (status, elapsed time, fuel
   if feasible), so a live run is never silent for longer than one solve.
2. `optimize_speed_profile()` gained an optional `maxiter` parameter (forwarded to
   `scipy.optimize.minimize`'s `options`), `None` by default (scipy's own default, zero behavior
   change for existing callers). `sweep_epsilon_constraint()` now passes
   `DEFAULT_SWEEP_MAXITER = 60` unless overridden, bounding each solve's worst case in a
   many-solves-back-to-back context specifically, without touching `optimize_speed_profile()`'s
   default behavior anywhere else in the project. Any residual precision loss from capping
   iterations early falls within `pareto._dominates()`'s existing tolerances (see the numerical-
   noise bug entry above), so it can't change which points the frontier reports.

**Tests:** 5 new (`test_sweep_defaults_to_bounded_maxiter_not_unbounded`,
`test_sweep_calls_on_progress_once_per_epsilon_in_order`,
`test_sweep_on_progress_reports_none_for_infeasible_epsilon` in `test_pareto.py`;
`test_maxiter_defaults_to_scipy_default_unbounded`,
`test_maxiter_is_actually_forwarded_to_the_solver` in `test_speed_profile.py` — the latter proves
`maxiter=1` measurably changes the solved outcome versus uncapped, not just that the parameter is
accepted). 120/120 project-wide, zero regression.

**Still open:** if the per-epsilon progress printing shows one specific epsilon taking dramatically
longer than the others on a rerun, that would be real evidence pointing at a specific cause (a
particular corridor/deadline combination hitting a pathological SLSQP path) rather than a uniform
slowdown — worth capturing and investigating specifically if it recurs, rather than treated as
resolved by the `maxiter` cap alone.

**Update — real per-epsilon timing came in, and it retracts the "must be a hang" conclusion
above.** The rerun with progress printing measured the real (not synthetic-benchmark) cost of ONE
epsilon on corridor 1's real ~390-leg path: 217.7s, converged (`success=True`). This directly
contradicts the earlier ~2.4s/solve worst-case projection — that projection used a stand-in
`weather_lookup` (`random.uniform()`) far cheaper than the real one (a spatial KD-tree query, a
time binary search, a NaN-fallback check, and a trig-based headwind projection, called twice per
leg for wave and wind), and implicitly assumed fast SLSQP convergence that a real, high-dimensional
(~386 free speed variables), only-piecewise-smooth (weather sampling steps at nearest-neighbor time
bins, not a smooth function of arrival time) problem doesn't necessarily get. With that real
per-evaluation cost and iteration count, a multi-hour total across 30 solves is consistent with
genuine computation — the earlier 3-hour report was very likely real work the whole time, not a
hang. Documented here plainly as a retraction of the earlier claim, per this project's standard of
not asserting an explanation without evidence for it (the earlier claim was exactly that mistake,
just caught later than it should have been).

**What was tried next, and what honestly worked vs. didn't:**
1. **Warm-starting** (`optimize_speed_profile()` gained an opt-in `initial_speeds_knots` parameter;
   `sweep_epsilon_constraint()` now starts each epsilon after the first from the previous feasible
   epsilon's converged per-leg speeds, falling back to a cold start after any infeasible/
   non-converged epsilon). Benchmarked directly on a synthetic 200-leg corridor before trusting it:
   cold-start sweep 34.3s vs. warm-started 31.7s — only ~1.08x. Root cause of the small effect:
   SLSQP's finite-difference gradient makes PER-ITERATION cost (on the order of legs+1 full-corridor
   evaluations, regardless of where the solve starts) the real driver, not iteration count — warm-
   starting shaves iterations, but each remaining one costs the same either way. Shipped anyway
   since it's a strict opt-in (existing callers/tests unaffected), fully tested (5 new tests
   including a direct spy-based check that warm-starting is actually happening, not just accepted
   and ignored), and has no downside — but reported honestly here as a marginal win, not a fix.
2. **`SWEEP_N_POINTS` reduced from 10 to 5** in `run_pareto_sweep.py`. The one lever that reliably
   and proportionally cuts wall-clock time without touching solver internals — half the epsilons,
   roughly half the total time. Not elegant, but honest: this is what actually moves the needle
   right now, everything else tried is a smaller effect on top of it.
3. **Not attempted, flagged as the real fix for later:** supplying an analytic gradient to
   `scipy.optimize.minimize` instead of relying on finite differences would cut per-iteration cost
   from O(legs) evaluations to O(1) — the actual lever that would matter most, per the diagnosis
   above. Not implemented in this session: the fuel-rate function IS analytically differentiable in
   speed at FIXED weather (cubic polynomial), but weather itself is sampled at a nearest-neighbor
   time index that shifts with cumulative arrival time, which is itself a function of every earlier
   leg's speed — making a fully rigorous analytic gradient more involved than a quick patch, and not
   something to rush into a numerically-sensitive optimizer path without a way to verify correctness
   against real data from this sandbox. Left as an open, clearly-scoped item rather than attempted
   and possibly shipped wrong.

**Update — the `maxiter` cap alone was NOT enough on real data, proven directly, and the `max_seconds`
fix that followed had a real bug of its own before it actually worked.** Real per-epsilon timing
from a live run: epsilon 1 (the tightest/floor deadline) converged in 217.7s; epsilon 2 (a LOOSER
deadline, normally the easier case) took 1941.1s — 32 minutes, ~9x longer, and did not converge even
at the `maxiter=60` cap. This is direct proof that iteration count does not bound wall-clock time
here: identical iteration budget, ~9x different cost, because per-ITERATION cost itself varies
depending on how the real (not synthetic) weather field's structure interacts with SLSQP's
finite-difference gradient at that particular point in the search — not something an iteration cap
can see or bound.

**First fix attempt (checking the clock via scipy's `callback`) was verified to fail before being
shipped, not after:** a direct test (artificially slow per-leg `weather_lookup`, a real deadline
forcing genuine SLSQP iterations) showed a `callback`-based check — which scipy calls once per
ITERATION — let 22+ full-corridor objective evaluations happen before ever getting a chance to check
the clock, because one iteration's finite-difference gradient needs on the order of (legs + 1)
evaluations. Measured overrun: 37.2s of actual work against a 2.0s budget — an 18x miss, on a
corridor with only 60 legs (real corridors are ~390). This would not have caught the real problem at
all; caught by testing the mechanism directly against the exact failure shape before trusting it,
not by reasoning about it abstractly.

**Actual fix:** moved the time-budget check inside `_evaluate()`'s per-leg loop itself — checked
before EVERY leg, not once per call and not once per iteration. `optimize_speed_profile()` gained an
opt-in `max_seconds` parameter (forwarded via `args=(deadline,)` into `_objective`, `None` by
default — zero behavior change for every existing caller); on timeout, raises an internal
`_TimeBudgetExceeded` carrying the in-progress speeds, caught and turned into a real
`SpeedProfileResult` with `success=False` and a message naming the timeout — never a raised
exception a caller has to handle, and never a silently dropped point. Re-ran the exact scenario that
caught the callback bug: 2.61s elapsed against the same 2.0s budget — genuinely bounded (the small
remaining overrun is one leg's processing time plus the final result-construction pass, both
expected and documented).

`sweep_epsilon_constraint()` now defaults to `DEFAULT_SWEEP_MAX_SECONDS = 180.0` alongside
`DEFAULT_SWEEP_MAXITER` (kept as a secondary, now-redundant-but-harmless safeguard). This makes the
sweep's total worst case an actual, plannable number for the first time:
`SWEEP_N_POINTS x k_corridors x max_seconds` — with the reduced 5-point sweep and 3 corridors, ~45
minutes worst case, versus the genuinely open-ended risk before this fix. `run_pareto_sweep.py`'s
progress printer now labels a timed-out point distinctly ("TIMED OUT (result kept,
unproven-optimal)") rather than lumping it in with ordinary non-convergence.

**Tests:** 4 new (`test_max_seconds_defaults_to_unbounded`,
`test_max_seconds_bounds_wall_clock_time_even_with_many_slow_legs` — a direct regression reproducing
the exact scenario that caught the callback bug, asserting the fix holds with a real (not
coincidental) margin — in `test_speed_profile.py`). 127/127 project-wide, zero regression.

**Honest state of Phase 4 as of this update:** the sweep now has a real, bounded worst-case runtime
and never hangs or silently drops a point, but a `max_seconds`-timeout point is an unproven-optimal
result, not a converged one — its fuel/duration/risk numbers are real (computed from a real,
feasible-or-near-feasible speed profile) but not guaranteed to be the TRUE minimum-fuel point for
that epsilon. This is disclosed in the sweep's own printed output (the "TIMED OUT" label) and should
be treated as lower-confidence on any frontier point it appears on, until/unless the deeper fix (an
analytic gradient, flagged above) removes the need for a timeout at all.

**Update — real run completed successfully within the new bound, closing out Phase 4's runtime
problem: total wall-clock ~42 minutes (15 solves), versus the previous run's 3+ hours for less
progress.** Real results, all 3 corridors, 5 epsilons each:

| Corridor | ep1 (228.6-230.4h) | ep2 | ep3 | ep4 | ep5 (365.7-368.7h) |
|---|---|---|---|---|---|
| 1 | 749.2t (ok, 149.5s) | 580.4t (**timeout**, 180.0s) | 469.6t (**timeout**, 180.0s) | 392.0t (ok, 177.5s) | 336.6t (**timeout**, 180.2s) |
| 2 | 751.3t (ok, 130.9s) | 581.9t (timeout) | 470.8t (timeout) | 393.0t (timeout) | 337.6t (timeout) |
| 3 | 755.5t (ok, 74.5s) | 585.0t (timeout) | 473.3t (timeout) | 395.1t (timeout) | 339.4t (timeout) |

Pooled and filtered: **all 5 non-dominated frontier points come from corridor 1** — corridors 2 and
3 are strictly dominated at every comparable epsilon (marginally longer/costlier lateral variants of
the same route, consistent with Phase 2's finding that this corridor's "diversity" is mild lateral
spread, not a genuine routing choice with a large cost gap). Fuel decreases smoothly and
monotonically as the deadline loosens (749.2 -> 580.4 -> 469.6 -> 392.0 -> 336.6t), matching the
expected shape with no discontinuities — a real sanity check the timeout-affected points still
passed despite not being proven-optimal.

**Honest caveat on the reported frontier: only 4 of 15 solves converged (`success=True`); on the
5-point frontier itself, only 2 of 5 (epsilon 1 and epsilon 4) are proven-optimal — the other 3 are
real, feasible, `max_seconds`-timeout results.** Their fuel numbers are genuine (computed from an
actual feasible speed profile satisfying that epsilon's deadline), but not verified as the true
minimum for that epsilon — the true frontier could sit at or below these values, never above. Worth
raising `DEFAULT_SWEEP_MAX_SECONDS` for a future run focused only on corridor 1 (now known to be the
only one that matters for this frontier) if a fully-converged frontier is needed, rather than
accepting this one as final.

**A property of the `weather_risk_m_hours` metric worth flagging, not a bug:** in this run, risk
climbed monotonically WITH duration (185.0 -> 194.2 -> 231.2 -> 286.3 -> 322.0 m*h) as fuel dropped.
This is expected given the metric's definition (`wave_height_m x transit_hours` per leg, summed) —
running the same physical conditions more slowly mechanically accumulates more metre-hours of
exposure, independent of whether the route is actually "riskier" in any exposure-severity sense.
So on this frontier, the fuel-cheap/slow end and the risk-cheap/fast end are the same axis almost by
construction, not two independently-discovered trade-offs — worth being explicit about this when
this frontier is shown to anyone, since "risk" here measures time-in-weather, not danger.

**Not yet done:** a rerun focused on corridor 1 alone with a larger `max_seconds` (to get a fully
converged frontier); the analytic-gradient fix that would make this fast rather than merely bounded,
still flagged above as the real unresolved item.

## Phase 4 addendum: severe-weather-hours risk-budget column

Added `severe_weather_hours(result, threshold_m=4.5)` to `pareto.py` as a companion to
`weather_risk_m_hours` — a threshold-gated metric ("hours spent at/above a stated wave-height
limit") rather than the continuous metre-hours integral, because that's the shape a commercial risk
budget / charter-party heavy-weather clause actually asks for. Reads the same per-leg
`wave_height_m`/`transit_hours` data already in `SpeedProfileResult.legs`, so no new weather
sampling was needed. Wired through: `ParetoPoint` gained a `severe_weather_hours` field (defaulted
to 0.0 so existing direct-construction call sites, e.g. `test_pareto.py`'s `_point()` helper, didn't
need updating), `sweep_epsilon_constraint()` gained a `severe_weather_threshold_m` parameter
(default `DEFAULT_SEVERE_WAVE_THRESHOLD_M = 4.5`) and now populates the field on every point it
returns, and `run_pareto_sweep.py`'s CSV writer gained a `severe_weather_hours_ge_4.5m` column. 8 new
tests added (closed-form threshold checks including the `>=` boundary case, a custom-threshold
override, and two sweep-integration checks against hand-built rough/mild legs). Full suite: 133
passed.

**Not yet done — this needs a real rerun to be useful:** the CSV currently checked in
(`pareto_frontier_rotterdam_ceyhan.csv`) predates this column and has no per-leg wave-height data to
retroactively compute it from (only aggregate `weather_risk_m_hours` was written out). Getting real
severe-weather-hours numbers requires rerunning `run_pareto_sweep.py` once (bounded to ~45 min per
the fix above; the already-known result from the prior run — only corridor 1 survives the frontier,
2 of 5 points proven-optimal — will likely repeat). Once that rerun produces a CSV with this column,
a different severe-weather threshold does NOT need a rerun: `df[df["severe_weather_hours_ge_4.5m"]
<= budget]`-style filtering, or recomputing at a different threshold from the underlying `.legs` data
if that's ever persisted, both work off the one real run.

## Phase 5 — Airflow-orchestrated batch entry point for the Phase 4 Pareto sweep

Goal: run Phase 4's sweep on a schedule (Airflow) rather than only interactively,
without duplicating the sweep logic between an Airflow task and the standalone
script.

**Refactor first, DAG second.** Extracted `scripts/run_pareto_sweep.py`'s `main()`
into `navi_opt.orchestration.pareto_sweep_task.run_pareto_sweep_task()`, an
importable function taking a `SweepConfig` dataclass (origin/destination ports,
cache paths, weather window, every sweep parameter) and returning a `SweepResult`
(output CSV path + the in-memory `all_points`/`frontier` lists, so a caller isn't
forced to re-read its own just-written CSV to get them back). The script is now a
thin wrapper: builds a `SweepConfig` from its existing module-level constants,
supplies the print-based progress callbacks for interactive use, and catches
`FileNotFoundError`/`ValueError` to keep printing the same friendly messages it
always did. The task function itself *raises* those instead of printing-and-
returning, deliberately — a scheduled run has no one watching stdout to notice a
silent early return; an Airflow task needs a real exception to mark itself failed.
8 new tests in `tests/test_pareto_sweep_task.py`, all mocking the four external
calls (`load_graph`, `load_anchors`, `open_weather_field_remote`,
`make_weather_lookup`) so they run offline against a trivial two-leg graph —
solver/sweep/frontier correctness is already covered directly in `test_pareto.py`;
this file only covers the wiring/I/O this module adds. Full suite: 141 passed.

**Scope decision, made explicit rather than silently deferred:** this is still ONE
corridor (Rotterdam<->Ceyhan), not a loop over a fleet of routes, even though the
DAG is titled "batch" and the README's stated ambition is fleet-planning. The
project has exactly one cached real-weather digraph, built for one specific bbox.
Looping the new `origin_port`/`destination_port` parameters over hardcoded port
pairs would silently pretend to support fleet planning while actually routing
every "different" pair across a graph that was never built to cover them — worse
than not doing it, since it would look like progress on the stated goal while
being wrong. A real fleet batch needs either a much larger shared ocean grid or a
per-route graph-building step first; documented here so this isn't rediscovered
as an oversight later, same pattern used throughout this project for scope calls
like this.

**Airflow needs its own image — not the stock `apache/airflow`.** Traced every
`import`/`from` transitively reachable from `run_pareto_sweep_task()` (through
`graph_builder.py`, `port_anchors.py`, `pareto.py`, `speed_profile.py`,
`k_shortest.py`, `vessel_profiles.py`, `weather_field.py`, `cmems_client.py`) to
build a deliberately NARROWER pip requirements list than `environment.yml`
(`infra/airflow/requirements.txt`): numpy, scipy, pandas, pyarrow (the
`pd.read_parquet` engine `port_anchors.py`'s cache format needs — easy to miss,
not obvious from `environment.yml` since conda's pandas build bundles a working
parquet engine differently than a bare pip install does), networkx, shapely,
pyproj, h3, PyYAML, xarray, copernicusmarine. Left out on purpose: geopandas,
netcdf4/h5netcdf (only needed by the local-file weather path this task doesn't
use — it calls `open_weather_field_remote()`, the zarr/streaming path),
matplotlib/ortools/pyomo/fastapi/uvicorn/streamlit/plotly/pytest/confluent-kafka
(other phases' or the interactive/dashboard side's problem, not this image's).
`infra/airflow/Dockerfile` builds `apache/airflow:2.9.3-python3.11` (matching this
project's Python 3.11 requirement — the stock tag defaults to 3.8) plus this
requirements file. `docker-compose.yml`'s `x-airflow-common` now has `build:` +
a shared `image:` tag (so all three Airflow services build once, not three times),
`env_file: ../.env` (the SAME .env the standalone scripts already use — so CMEMS
credentials reach the containers without duplicating them into the compose file),
`PYTHONPATH: /opt/airflow/src` (same "point at `src/` instead of pip-installing
the package" convention `pyproject.toml`'s `pythonpath = ["src"]` already uses for
pytest), and two new volume mounts (`../config`, `../data` as siblings of
`../src` inside the container) — required because `vessel_profiles.py` /
`port_anchors.py` / `graph_builder.py` all resolve their config/cache paths as
`Path(__file__).resolve().parents[N] / "config"|"data"`, relative to their own
location under `src/`, unchanged from local execution.

**Not yet verified — flagged plainly, not glossed over:** none of this (image
build, DAG load, real task execution) has been run against a real Airflow
instance from the sandbox that wrote it — no Docker there. The `apache/airflow
:2.9.3-python3.11` tag's exact existence, whether `pip install`ing this
requirements list into that base image actually succeeds without a missing
system library (geopandas-adjacent packages have historically needed system GDAL
on Debian-slim bases; this list avoids geopandas specifically to sidestep that,
but copernicusmarine's own transitive deps haven't been verified against a slim
base either), and whether the DAG actually loads and runs correctly in a real
Airflow scheduler — all real open questions, called out in both the Dockerfile's
and the DAG's own docstrings, to be resolved by an actual `docker compose up -d
--build` + a real triggered DAG run, not assumed working from this sandbox.

**Deliberate design choices worth flagging for a reader, not just a note to
self:** the DAG names each run's output CSV with Airflow's own `ds` (execution
date) rather than overwriting one well-known path — a scheduled batch job's whole
point is keeping every run's output around to compare, unlike the interactive
script (which is fine overwriting the same file on repeated manual runs).
`retries=0` and `execution_timeout=60min` on the DAG's `default_args` are also
deliberate: a naive `retries=1` would silently double the real cost of a
transient CMEMS hiccup (another ~45 minutes) with no better odds of success, so
this fails loudly instead and leaves re-triggering to a person's judgment; the
60-minute timeout is a safety net just above the sweep's own known ~45-minute
worst case, not an arbitrary round number.

**Not yet done:** Phase 6 (Kafka event-driven reoptimization) — the actual next
piece the README's stated architecture calls for; a genuine multi-route fleet
loop (needs the bigger-ocean-grid-or-per-route-graph decision above first); and
pushing results into Postgres instead of/alongside a CSV, which stays out for now
specifically to avoid mixing domain data into Airflow's own metadata database
without a real schema decision behind it.

## Phase 5 correction — the requirements.txt trace missed a real transitive dependency

First real `docker compose up -d --build` + DAG load on the user's actual machine
caught something this sandbox couldn't: `pareto_sweep_dag.py` failed to load with
`ModuleNotFoundError: No module named 'geopandas'`, traced to
`graph_builder.py -> h3_ocean_grid.py -> land_mask.py -> import geopandas`.

The root cause was a mistake in how the original dependency trace was done, not an
environment difference: the `grep` used to find `navi_opt`'s import chain
explicitly *excluded* any line matching `navi_opt\.` — meaning it only ever
looked at third-party imports, never followed navi_opt-internal imports to see
what THEY pulled in transitively. `graph_builder.py` imports
`navi_opt.grid.h3_ocean_grid` (for `cell_centroid`/`neighbors`, used inside
`load_graph()`'s dependents elsewhere in that file) and `navi_opt.grid.land_mask`
directly, and `h3_ocean_grid.py` itself imports `land_mask.py` — so importing
`graph_builder.py` at all executes `land_mask.py`'s `import geopandas as gpd`,
regardless of whether `load_graph()` (the only function the sweep task actually
calls) ever exercises any land-masking logic. Python has no lazy-import-by-default
behavior; a module-level import runs at import time, full stop — the original
trace's reasoning ("only needed by the land-masking path this task doesn't use")
was simply wrong about how Python import execution works, not about what
`load_graph()` does at runtime.

Redid the trace properly with an AST walk (`ast.walk` over every file's
`Import`/`ImportFrom` nodes, following `navi_opt`-internal imports recursively)
instead of eyeballing grep output, and found a second real miss the same way:
`land_mask.py` also imports `navi_opt.grid.download_natural_earth`, which imports
`requests` — three hops down, easy to miss by inspection, not by an automated
walk. Both added to `infra/airflow/requirements.txt` with the actual import
chain documented in-line.

**Lesson for future dependency tracing in this project:** grep-based "what does
this import" checks are only trustworthy for direct, single-file imports;
anything crossing a module boundary needs either an automated transitive walk or
an actual import attempt in the target environment — this is exactly the gap
between "looks right on inspection" and "confirmed by running it" this project's
`findings.md` has flagged before (Phase 4's sweep timing, Phase 1's Windows HDF5
conflict) and now again here. The image otherwise built and ran cleanly on the
first real attempt — the base image tag, the rest of the dependency list, the
`env_file`/`PYTHONPATH`/volume-mount wiring in `docker-compose.yml`, and the six
containers coming up healthy all worked as designed; this was the one real gap.

## Phase 6 — Kafka event-driven reoptimization

**What this closes:** the README's stated architecture always named Kafka as
the event-driven layer sitting alongside Airflow's batch layer; Phase 6 builds
it — a voyage departs, its state is tracked in-process, real CMEMS weather is
polled on a schedule, and a meaningful forecast change on an edge still ahead
of a tracked voyage triggers a real bounded re-solve of that voyage's remaining
legs, publishing the before/after result.

**Two scope decisions made explicit up front, both taken from the recommended
option when asked (see the two `AskUserQuestion` calls that kicked this phase
off), not defaulted to silently:**

1. **Real polling against live CMEMS, not synthetic weather events.** The
   producer (`weather_producer.py`) does a genuine `open_weather_field_remote()`
   fetch on every poll and diffs it against the previous poll's sample — no
   synthetic "inject an event" test harness stands in for this. The trade-off,
   stated plainly: every poll costs a real network round-trip and CMEMS forecast
   revisions are not sub-hourly, so `DEFAULT_POLL_INTERVAL_SECONDS = 3600.0` is
   the honest default, with a documented `--poll-interval-seconds` override for
   demoing without a real hour's wait.
2. **In-memory voyage state in the consumer process, not Postgres-backed.**
   `VoyageRegistry` is a plain in-process dict — the simplest real
   implementation, explicitly not durable. Restart the consumer and every
   registered voyage's state (beyond its original departure, which IS
   replayable — see below) is gone. A Postgres-backed registry is a real,
   deferred option, not built now because it's premature durability/schema
   design before the core reoptimization logic itself was proven.

**The three-clock model (`voyage_registry.py`) — the one genuinely new design
problem this phase had to solve.** A voyage's weather sampling needs the SAME
absolute calendar reference every `optimize_speed_profile()` call in this
project has always used (`voyage_start_time`, fixed to whatever historical/
forecast CMEMS window was fetched — June 2024 throughout this project). But
"how far along is this voyage right now" needs some notion of elapsed real time
to progress at all within a demo session that runs minutes, not the
~230–410 real hours findings.md's Phase 4 entries measured for the actual
corridor. Conflating those two clocks would be wrong — real "now" in 2026 has
no meaningful relationship to a fixed 2024 CMEMS window. So a third, explicit
clock exists: `demo_departed_at` (the real wall-clock moment a voyage was
registered), scaled by `DEMO_TIME_SCALE = 600.0` (one real minute = 10
simulated voyage hours) into voyage-timeline hours via `elapsed_hours()`. This
is openly a demo acceleration mechanic, not a claim about real voyage
tracking — a real deployment would replace `elapsed_hours()`'s wall-clock
computation with real AIS/position-report data and drop the demo clock
entirely; `DEMO_TIME_SCALE` exists specifically to keep that swap legible as a
placeholder, not to quietly ship as if it were real position tracking.

**Event-as-trigger, not event-as-ground-truth (`reoptimization_consumer.py`).**
`handle_weather_update()` deliberately does NOT reoptimize against the
`WeatherUpdateEvent`'s own embedded previous/new wave-height values — those
exist to decide *whether* to react, not what to solve against. The actual
reoptimization always uses a freshly-built `weather_lookup` the caller
constructed for that event (a real `open_weather_field_remote()` +
`make_weather_lookup()` pair — see `run_reoptimization_consumer.py`'s main
loop). A real event-driven system re-derives the truth it acts on rather than
trusting a notification payload for a decision with real solver cost attached
(a 180s-bounded solve per affected voyage, `reoptimizer.
DEFAULT_REOPTIMIZE_MAX_SECONDS` — same bounded-solve reasoning as
`pareto.py`'s sweep budget, reused rather than re-derived since a live
reoptimization is if anything more time-sensitive than a batch sweep point).

**Decoupled producer, by design, not by oversight (`weather_producer.py`).**
The producer has no knowledge of `VoyageRegistry` or any specific voyage's
schedule — it samples every edge of the cached digraph at a single fixed
reference time (`elapsed_hours=0`, i.e. "what would a voyage departing right
now see"), which is a real, defensible proxy for "has the forecast changed"
(CMEMS forecasts genuinely do get revised for the same target dates as new
observations arrive), not a claim that it tracks every voyage's own per-leg
scheduled time. Producers and consumers being mutually unaware of each other's
internal state is Kafka's whole point, not a gap to close; a more complete
system sampling at each active voyage's own scheduled time is a real,
documented architecture change for later, not implemented here.

**Warm-starting preserved across reoptimizations.** `reoptimize_voyage()`
warm-starts SLSQP from the committed profile's remaining-leg speeds — same
"the previous solution is typically close to the next one" reasoning already
used for `sweep_epsilon_constraint()`'s own warm start (see the Phase 4 timing
entry above), now applied to a live re-solve rather than a sweep point.

**Already-late handling, not swallowed.** If a voyage's elapsed time already
exceeds its original deadline by the time a reoptimization fires (e.g. after a
prior reoptimization already used up all the slack), `reoptimize_voyage()`
does not clamp to zero (would misrepresent "immediate arrival required" as a
real constraint) or raise (a late voyage still needs a best-effort profile,
arguably more urgently than an on-time one) — it drops the deadline constraint
for that one solve and returns the optimizer's unconstrained-fuel-minimizing
profile, leaving "this came back already-late" as a signal the caller can act
on (an alert, a flagged event), not something this function decides how to
react to.

**Plain JSON over the wire, not Avro/a schema registry** — `serialization.py`
recurses `SpeedProfileResult`/`LegResult` (both `typing.NamedTuple`s) into
plain dicts via `._asdict()`; the two event dataclasses
(`WeatherUpdateEvent`, `ReoptimizationResultEvent`) are already flat enough for
`dataclasses.asdict()` directly. Same "don't add complexity a problem this
size doesn't need" standard this project has applied before (`pareto_frontier
()`'s O(n²) filter, the STRtree/joblib non-decision in Phase 2) — a
single-node demo Kafka cluster with a handful of message types doesn't need
schema evolution machinery, and plain JSON is directly inspectable in Kafka UI
(already running per `infra/docker-compose.yml`), which matters for a project
meant to be demoed and read.

**Restart-survives-partially, documented rather than silently gapped
(`run_reoptimization_consumer.py`).** In-memory state means a naive restart
loses every registered voyage. The consumer mitigates this by replaying
`voyage-events` from the earliest offset on startup (`_consume_departure_
backlog()`) to rebuild the registry before joining the live loop —
`weather-updates` is read live only (`latest`), since replaying old weather
diffs on every restart would just re-trigger reoptimizations already handled.
This is a real but partial fix: a voyage's REOPTIMIZED profile is never itself
republished to `voyage-events`, so a restart mid-voyage rebuilds each voyage
from its ORIGINAL departed profile, not its latest reoptimized one. Closing
that gap properly means either also replaying `reoptimization-results` (last
result wins) or the Postgres-backed registry this phase already scoped out —
left open here, stated plainly, not silently masked by the backlog replay
looking like full durability when it isn't.

**Module layout, mirroring the reasoning in each module's own docstring:**
`serialization.py` (JSON round-trips) → `voyage_registry.py` (what a voyage
IS, the three-clock model) → `reoptimizer.py` (the one piece with real
mathematical content — a bounded, warm-started `optimize_speed_profile()`
call, independently unit-testable against a real solve on a tiny graph) →
`weather_producer.py` (the polling loop's pure diff logic, `poll_once()`'s
real-fetch glue kept thin and untested from this sandbox) →
`reoptimization_consumer.py` (message-handling logic, pure functions taking
already-parsed dicts and a registry — no Kafka client inside it, same
network-glue-stays-thin pattern). Three CLI scripts
(`depart_demo_voyage.py`, `run_weather_producer.py`,
`run_reoptimization_consumer.py`) wire the pure modules to real
`confluent_kafka.Producer`/`Consumer` instances and the real Kafka broker from
`infra/docker-compose.yml`.

**Tests:** 21 new tests across the five streaming modules (5 serialization, 14
voyage_registry, 7 reoptimizer, 9 weather_producer, 7 reoptimization_consumer
— 42 total; some already covered in earlier entries this session before the
consumer module existed), all offline: `reoptimizer.py`'s tests run a real
`optimize_speed_profile()` solve on a small synthetic graph (same "real solver
call on a tiny fake graph, not mocked" standard as `test_pareto.py`/
`test_pareto_sweep_task.py`); everything network/Kafka-facing
(`weather_producer.poll_once()`'s real fetch, and the three CLI scripts' real
`Producer`/`Consumer` loops) is deliberately kept thin and untested from this
sandbox — same "the decision logic underneath is fully covered, the glue
needs a real broker/network to exercise" split as every earlier phase's
network-facing code. Project-wide: 183/183 passing, zero regression.

**Not yet verified — flagged plainly, not glossed over:** none of Phase 6 has
been run against the user's real, already-running Kafka broker or real CMEMS
from the sandbox that wrote it — no network route to either from here. Whether
`confluent-kafka` (already in `environment.yml`, so likely no new Docker image
needed the way Airflow required one — but not yet confirmed) actually connects
cleanly to the KRaft-mode broker at `localhost:9092`, whether a real
`depart_demo_voyage.py` run solves and publishes correctly, whether
`run_weather_producer.py` actually detects a real forecast revision within a
reasonable demo session, and whether `run_reoptimization_consumer.py`'s
backlog-replay and live dispatch loop behave correctly end-to-end against the
real broker — all real open questions for the user's machine to answer, the
same "read the printed output before trusting it" discipline every prior
phase's first real run has needed.

**Not yet done:** a genuine multi-voyage / multi-corridor fleet demo (nothing
in Phase 6 prevents running `depart_demo_voyage.py` multiple times with
distinct `--voyage-id`s, but it hasn't been exercised); pushing
`reoptimization-results` anywhere durable beyond the Kafka topic itself (a
dashboard, Postgres) — out of scope for the reasoning above; the fixed June
2024 CMEMS window means `run_weather_producer.py` polls the same historical
forecast repeatedly rather than a genuinely live-updating one — real forecast
revisions for a fixed past window are far rarer than for a live near-term
window, so a demo may see few or no real weather-update events without either
a longer observation period or (a real option, not implemented) shifting the
polled window to something closer to "now" specifically to see live revisions.

## Phase 6 — first real run on the user's machine: what it caught

Run against the real Kafka broker and live CMEMS on 2026-10-09/10. The pipeline
worked end to end (departure published, consumer registered it, an event
triggered a real CMEMS re-fetch and a bounded re-solve, a result was published),
and the run caught four real problems — three in my code or config, one a
misleading number. None were visible from the sandbox that wrote the code.

1. **Kafka had never started (Phase 0 config bug).** `CLUSTER_ID:
   navi-opt-kraft-cluster-1` in `docker-compose.yml` is not a valid KRaft cluster
   ID (must be a 22-character base64 UUID; that string decodes to 18 bytes), so
   the broker crash-looped from the day the stack was created. It went unnoticed
   because nothing had exercised Kafka until Phase 6, and `docker compose ps`
   showing a container as "Up" between restarts is not the same as the broker
   serving requests. Fixed with a valid ID (`MkU3OEVBNTcwNTJENDM2Qk`); no data
   was lost because the volume had never been initialised. Lesson: a service
   with no healthcheck being "up" is not evidence it works.
2. **`depart_demo_voyage.py` had an unbounded solve.** Every other solve on this
   corridor is capped (Phase 4 measured 217s to 32+ minutes uncapped), but the
   departure script's first solve was not, and ran for 10+ minutes before being
   stopped. Now uses the reoptimizer's budget (`maxiter=60`, `max_seconds=180`).
   The capped result misses its own deadline slightly (344.2h vs 342.9h): an
   unproven-optimal timed-out iterate, still a real, usable profile, published
   as such.
3. **`_MAX_POLL_EXCEEDED`.** Handling one weather event (about a minute of CMEMS
   re-fetch plus up to 180s of solve per affected voyage) exceeds Kafka's default
   5-minute `max.poll.interval.ms`, which dropped the consumer from its group
   right after it finished the work. Raised to 30 minutes in the consumer config.
4. **The printed before/after was not like-for-like.** The console showed
   `fuel 369.7 -> 235.5t, transit 344.2 -> 217.0h` for a voyage that was simply
   ~37% sailed — the old figures were the whole voyage, the new ones only the
   remaining 240 of 386 legs. That reads as a 36% saving and is nothing of the
   kind. `ReoptimizationResultEvent` now carries `old_remaining_fuel_tonnes` /
   `old_remaining_transit_hours` (the old profile's matching tail), the
   console prints those against the new figures, and a regression test pins it.
   The whole-profile `old_*` fields stay for the record. No claim about savings
   should ever be made from the whole-profile fields.

**What the injected-event run does and does not show.** CMEMS's fixed June 2024
window did not revise, so the weather event was a labelled test injection
(`scripts/inject_weather_event.py`, event carries `"injected": true`). The
consumer re-fetches real weather regardless (event-as-trigger), so an injected
event proves the plumbing — event, affected-voyage lookup, bounded re-solve,
commit, result published — and NOT that the optimizer responds to a storm.
Demonstrating a genuine response would need a weather lookup that really
differs from the one the voyage was planned against; not built.

Not yet checked: that the consumer rejoins and keeps working after the
`_MAX_POLL_EXCEEDED` drop (the fix removes the cause; recovery behaviour itself
was not observed), and `run_weather_producer.py` publishing a genuine event.
