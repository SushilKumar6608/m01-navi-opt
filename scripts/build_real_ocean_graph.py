"""
Builds the real ocean graph over the Rotterdam <-> Ceyhan corridor (North
Sea -> English Channel -> Bay of Biscay -> Gibraltar Strait -> Mediterranean
-> Ceyhan), snaps the corridor's real ports to it, and runs an end-to-end
smoke test: Yen's k-shortest corridors, then the speed optimizer on each.

Run once from the project root, with the conda env active:
    python scripts/build_real_ocean_graph.py

Requires internet access to naturalearthdata.com's CDN mirror for the first
run (downloads ~1MB of coastline polygons, cached afterward). This can't be
verified from the sandbox that built this script (no route to that CDN from
there — see findings.md) so this is the first time this exact code path
runs against real data; read the printed stats before trusting the output,
same as every other phase in this project.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import networkx as nx

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from navi_opt.grid.download_natural_earth import download_land_polygons  # noqa: E402
from navi_opt.grid.graph_builder import build_ocean_graph, save_graph  # noqa: E402
from navi_opt.grid.h3_ocean_grid import cell_centroid, generate_ocean_cells_in_bbox  # noqa: E402
from navi_opt.grid.land_mask import build_land_union  # noqa: E402
from navi_opt.grid.port_anchors import save_anchors, snap_ports_to_ocean_cells  # noqa: E402
from navi_opt.grid.ports import load_ports  # noqa: E402
from navi_opt.optimization.speed_profile import (  # noqa: E402
    calm_weather_lookup,
    optimize_speed_profile,
)
from navi_opt.routing.a_star import haversine_nm  # noqa: E402
from navi_opt.routing.graph_adapter import to_routable_digraph  # noqa: E402
from navi_opt.routing.k_shortest import YenKShortestPaths  # noqa: E402
from navi_opt.weather.vessel_profiles import load_vessel_profile  # noqa: E402

# Rotterdam, Antwerp, Gibraltar, Ceyhan, with margin for the real sailing
# route between them (English Channel, Bay of Biscay, Iberian coast,
# Alboran/Med, Aegean approach to Ceyhan) — not just a box around the four
# point coordinates.
#
# west=-15/south=30 (rather than the originally-planned -8/33) is what it actually took to give
# the vessel enough navigable Atlantic water to round Cape Finisterre and Cape St. Vincent without
# the great-circle-ish shortest path clipping the bbox edge — confirmed against a real run (see
# findings.md).
BBOX = dict(west=-15.0, south=30.0, east=40.0, north=56.0)
# Resolution 3 (~59.8 km average hex edge — H3's published table, not the ~105km this project's
# earlier docstring estimate assumed) is coarser than this corridor's actual chokepoints: the
# Strait of Gibraltar is ~13 km wide at its narrowest, the Dover Strait ~34 km. A first real-data
# run at resolution 3 confirmed this isn't theoretical — Rotterdam and Antwerp, ports on opposite
# sides of the Rhine-Scheldt delta, snapped to the SAME H3 cell, meaning the grid couldn't even
# resolve that local geography, let alone a clean Gibraltar passage. Resolution 4 (~22.6 km edge)
# still left the North Sea/Channel disconnected from Gibraltar/the Med on a real run (382 vs. 1125
# node components). Resolution 5 (~8.5 km edge) is what actually produced one dominant component
# containing Rotterdam, Gibraltar and Ceyhan together (17692 nodes) — confirmed against a real
# run; Antwerp still ends up isolated in its own singleton component even at this resolution (the
# Scheldt estuary is narrower than a single resolution-5 hex edge), which is what step 6c below
# exists to handle rather than pushing resolution higher just for one port.
RESOLUTION = 5
PORT_KEYS = ["rotterdam", "antwerp", "gibraltar", "ceyhan"]

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "processed"
DIGRAPH_PATH = DATA_DIR / "ocean_digraph_rotterdam_ceyhan.pkl"
ANCHORS_PATH = DATA_DIR / "port_h3_anchors_rotterdam_ceyhan.parquet"

V_MAX_KNOTS = 16.0


def calm_water_cost_evaluator(digraph):
    def _evaluator(u, v, current_t):
        hours = digraph[u][v]["dist_nm"] / V_MAX_KNOTS
        return hours, hours

    return _evaluator


def main() -> None:
    t0 = time.perf_counter()

    print("1. Downloading Natural Earth land polygons (skipped if already present)...")
    download_land_polygons()

    print("2. Building land union (cached after first build)...")
    land_union = build_land_union()

    print(f"3. Generating ocean cells over bbox {BBOX} at resolution {RESOLUTION}...")
    ocean_cells = generate_ocean_cells_in_bbox(land_union=land_union, resolution=RESOLUTION, **BBOX)
    print(f"   {len(ocean_cells)} ocean cells")

    print("4. Building ocean graph (land-bridge pruning included)...")
    graph = build_ocean_graph(ocean_cells, land_union=land_union, verbose=True)

    print("5. Converting to routable directed graph...")
    digraph = to_routable_digraph(graph)
    print(f"   {digraph.number_of_nodes()} nodes, {digraph.number_of_edges()} directed edges")

    print("6. Snapping ports to ocean cells...")
    all_ports = load_ports()
    corridor_ports = {k: all_ports[k] for k in PORT_KEYS}
    anchors = snap_ports_to_ocean_cells(corridor_ports, ocean_cells, resolution=RESOLUTION)
    for key, cell in anchors.items():
        print(f"   {key}: {corridor_ports[key].name} -> {cell}")
    if len(set(anchors.values())) < len(anchors):
        print("   WARNING: two or more ports snapped to the SAME cell — resolution may be too "
              "coarse to distinguish their local geography (this is what happened to Rotterdam/"
              "Antwerp at resolution 3).")

    print("6b. Checking corridor connectivity (weakly connected components)...")
    components = sorted(nx.weakly_connected_components(digraph), key=len, reverse=True)
    print(f"   {len(components)} component(s); sizes (top 5): {[len(c) for c in components[:5]]}")
    comp_of_node = {node: i for i, comp in enumerate(components) for node in comp}
    for key, cell in anchors.items():
        idx = comp_of_node[cell]
        print(f"   {key} is in component #{idx} (size {len(components[idx])})")
    port_components = {comp_of_node[cell] for cell in anchors.values()}
    if len(port_components) > 1:
        print(f"   WARNING: the {len(PORT_KEYS)} ports are split across {len(port_components)} "
              f"disconnected component(s) — no route can exist between ports in different "
              f"components. This is the actual explanation if step 8 reports no path, not a bug "
              f"in the routing code itself; the fix is a finer RESOLUTION (see comment near the "
              f"top of this script) or a bbox adjustment, not a code change.")

        # Geographic footprint (lon/lat bounding box) of each port-containing component. A single
        # "closest pair" number is misleading on its own — it's crow-flight distance and can cut
        # straight across land (e.g. mainland Spain) between two components that are each large
        # and irregularly shaped, without saying anything about where either one's actual
        # coastal frontier sits. The bounding box says directly how far north/south/east/west
        # each connected chunk of ocean actually reaches.
        port_comp_indices = sorted(port_components, key=lambda i: -len(components[i]))
        for idx in port_comp_indices:
            comp = components[idx]
            lons, lats = [], []
            for cell in comp:
                lon, lat = cell_centroid(cell)
                lons.append(lon)
                lats.append(lat)
            print(f"   Component #{idx} (size {len(comp)}) footprint: "
                  f"lon [{min(lons):.2f}, {max(lons):.2f}], lat [{min(lats):.2f}, {max(lats):.2f}]")

        # Closest cross-component cell pair, kept as a secondary signal — interpret alongside the
        # bounding boxes above, not alone, since it's straight-line distance and may cross land.
        comp_a = components[port_comp_indices[0]]
        comp_b = components[port_comp_indices[1]]
        best = None
        for cell_a in comp_a:
            lon_a, lat_a = cell_centroid(cell_a)
            for cell_b in comp_b:
                lon_b, lat_b = cell_centroid(cell_b)
                d = haversine_nm(lat_a, lon_a, lat_b, lon_b)
                if best is None or d < best[0]:
                    best = (d, cell_a, lon_a, lat_a, cell_b, lon_b, lat_b)
        d, cell_a, lon_a, lat_a, cell_b, lon_b, lat_b = best
        print(f"   Closest gap (straight-line, may cross land) between component "
              f"#{port_comp_indices[0]} and #{port_comp_indices[1]}: {d:.1f} nm, between cell "
              f"{cell_a} (lon={lon_a:.2f}, lat={lat_a:.2f}) and cell {cell_b} "
              f"(lon={lon_b:.2f}, lat={lat_b:.2f}).")

    print("6c. Re-snapping any port outside the graph's dominant component...")
    # snap_ports_to_ocean_cells() only checks "is this H3 cell's centroid classified as ocean" —
    # it has no visibility into build_ocean_graph()'s land-bridge pruning, so a cell can pass
    # that test yet end up with zero usable edges (a real pattern for ports up narrow estuaries,
    # e.g. Antwerp via the Scheldt — see findings.md). A port anchor is only actually useful for
    # routing if it's reachable from the rest of the graph, so re-snap anything that isn't in the
    # largest component to the nearest cell that is, rather than silently leaving it unroutable.
    dominant_idx = max(range(len(components)), key=lambda i: len(components[i]))
    dominant_component = components[dominant_idx]
    RESNAP_WARN_THRESHOLD_NM = 50.0
    for key in list(anchors.keys()):
        if comp_of_node[anchors[key]] == dominant_idx:
            continue
        port = corridor_ports[key]
        best_cell, best_dist_nm = None, float("inf")
        for cell in dominant_component:
            lon, lat = cell_centroid(cell)
            d = haversine_nm(port.lat, port.lon, lat, lon)
            if d < best_dist_nm:
                best_dist_nm, best_cell = d, cell
        old_cell = anchors[key]
        anchors[key] = best_cell
        comp_of_node[best_cell] = dominant_idx
        flag = " ** LARGE RE-SNAP, VERIFY THIS IS SENSIBLE **" if best_dist_nm > RESNAP_WARN_THRESHOLD_NM else ""
        print(f"   {key}: {old_cell} (isolated) -> {best_cell} (dominant component), "
              f"{best_dist_nm:.1f} nm away{flag}")

    print("7. Caching results to data/processed/...")
    save_graph(digraph, path=DIGRAPH_PATH)
    save_anchors(anchors, path=ANCHORS_PATH)
    print(f"   graph: {DIGRAPH_PATH}")
    print(f"   anchors: {ANCHORS_PATH}")

    elapsed = time.perf_counter() - t0
    print(f"\nBuild complete in {elapsed:.1f}s.\n")

    print("8. Smoke test: Rotterdam -> Ceyhan, k=3 corridors, speed-optimized per corridor.")
    print("   enforce_diversity=True: a first real run at k=3 came back with 3 corridors sharing "
          "identical stats (Yen's kept re-finding near-duplicate tracks through adjacent hexes "
          "with negligible cost difference) — this is exactly the case that feature exists for.")
    origin_cell = anchors["rotterdam"]
    destination_cell = anchors["ceyhan"]

    yen = YenKShortestPaths(
        digraph,
        calm_water_cost_evaluator(digraph),
        v_max_knots=V_MAX_KNOTS,
        enforce_diversity=True,
    )
    corridors = yen.find_k_paths(origin_cell, destination_cell, k=3, start_time_hours=0.0)

    if not corridors:
        print("   NO PATH FOUND between Rotterdam and Ceyhan anchors — investigate before "
              "trusting anything else about this build (likely bbox too tight, or a genuine "
              "land-bridge pruning issue somewhere on this corridor).")
        return
    if len(corridors) < 3:
        print(f"   Only {len(corridors)} sufficiently diverse corridor(s) found (asked for 3) — "
              f"graceful degradation, not a bug: means the graph doesn't offer 3 genuinely "
              f"distinct tracks between these two ports at this resolution/diversity threshold.")

    if len(corridors) > 1:
        # Proof the corridors are actually distinct node sequences, not just "different enough
        # that node counts happen to differ" — same Jaccard check find_k_paths() itself used to
        # gate promotion (threshold 0.80: below is accepted as diverse). Total cost can still be
        # close even for genuinely distinct paths on an open-water crossing like this one, where
        # nothing but a strait or coastline forces a large cost gap between alternatives — this
        # is what actually distinguishes "diverse but similarly efficient" from "near-duplicate".
        print("\n   Pairwise Jaccard similarity between returned corridors (lower = more "
              "distinct; find_k_paths() only accepts pairs below 0.80):")
        for i in range(len(corridors)):
            for j in range(i + 1, len(corridors)):
                set_i, set_j = set(corridors[i]["path"]), set(corridors[j]["path"])
                jaccard = len(set_i & set_j) / len(set_i | set_j)
                print(f"     corridor {i + 1} vs corridor {j + 1}: {jaccard:.3f}")

    vessel = load_vessel_profile("mr2_product_tanker")
    for i, corridor in enumerate(corridors, start=1):
        print(f"\n   Corridor {i}: {len(corridor['path'])} nodes, "
              f"calm-water time {corridor['total_cost']:.1f}h")
        result = optimize_speed_profile(
            corridor["path"], digraph, vessel, "laden", weather_lookup=calm_weather_lookup
        )
        print(f"     fuel-optimized: {result.total_fuel_tonnes:.1f}t fuel, "
              f"{result.total_transit_hours:.1f}h transit, success={result.success}")


if __name__ == "__main__":
    main()
