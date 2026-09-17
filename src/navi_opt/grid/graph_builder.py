"""
Builds the NetworkX sea-lane graph over an ocean-cell set: nodes are H3
ocean cells, edges connect H3-adjacent ocean cells whose straight-line
segment does NOT cross land — the guard against illegal "land bridges"
across narrow straits/peninsulas (Dover Strait, Skagerrak/Kattegat,
Bosporus, Gibraltar) that centroid-in-ocean masking alone cannot catch,
since a cell's centroid can be perfectly valid open water while the
straight line to its neighbor's centroid still cuts across a headland.

Edge weights here are geodesic distance only (`dist_nm`), not fuel/time
cost — resistance_model.py (Phase 2, next) layers vessel- and
weather-dependent cost on top of this topology. This module's only job is:
which cell-to-cell transitions are physically navigable, and how long is
each one.
"""
from __future__ import annotations

import pickle
import time
from pathlib import Path

import networkx as nx
from pyproj import Geod
from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry

from navi_opt.grid.h3_ocean_grid import cell_centroid, neighbors
from navi_opt.grid.land_mask import build_land_union, prepared_land

GEOD = Geod(ellps="WGS84")
METERS_PER_NM = 1852.0

CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "processed" / "ocean_graph.pkl"


def _segment_crosses_land(
    u_lon: float, u_lat: float, v_lon: float, v_lat: float, prepared_land_union
) -> bool:
    """True if the straight LineString between two points intersects land.

    Factored out from build_ocean_graph() so it can be unit-tested directly
    against hand-picked coordinates, independent of H3 grid adjacency —
    this is the actual novel logic in this module; H3 neighbor iteration is
    already covered by h3_ocean_grid.py's own tests.
    """
    segment = LineString([(u_lon, u_lat), (v_lon, v_lat)])
    return prepared_land_union.intersects(segment)


def build_ocean_graph(
    ocean_cells: set[str],
    land_union: BaseGeometry | None = None,
    verbose: bool = False,
) -> nx.Graph:
    """Build an undirected NetworkX graph over `ocean_cells`.

    An edge (u, v) is added only if:
      1. v is an H3 grid-neighbor of u, AND
      2. v is itself in `ocean_cells` (not land, not outside the region), AND
      3. the straight-line segment between u's and v's centroids does NOT
         intersect land (the land-bridge guard — see module docstring).

    Edge weight is geodesic distance in nautical miles (`dist_nm`), stored
    as an edge attribute so any downstream cost model (Phase 2's
    resistance_model.py, Phase 3's speed-profile solver) builds on it
    without recomputing geometry.

    Uses land_mask.prepared_land() (a GEOS prepared geometry, already
    spatially indexed internally) for the intersects() checks rather than
    a raw shapely geometry or a hand-built STRtree — see findings.md for
    why a separate STRtree adds nothing here, since land_union is a single
    unified polygon, not a collection of many polygons to filter.
    """
    if land_union is None:
        land_union = build_land_union()
    prepared = prepared_land(land_union)

    graph = nx.Graph()
    graph.add_nodes_from(ocean_cells)

    start = time.perf_counter()
    edges_checked = 0
    edges_added = 0
    edges_pruned_land_bridge = 0
    seen_pairs: set[frozenset[str]] = set()

    for cell in ocean_cells:
        u_lon, u_lat = cell_centroid(cell)
        for nbr in neighbors(cell):
            if nbr not in ocean_cells:
                continue

            pair = frozenset((cell, nbr))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            edges_checked += 1

            v_lon, v_lat = cell_centroid(nbr)

            if _segment_crosses_land(u_lon, u_lat, v_lon, v_lat, prepared):
                edges_pruned_land_bridge += 1
                continue

            az_fwd, az_back, dist_m = GEOD.inv(u_lon, u_lat, v_lon, v_lat)
            graph.add_edge(
                cell,
                nbr,
                dist_nm=dist_m / METERS_PER_NM,
                # Bearing is direction-dependent but the graph is undirected,
                # so both travel directions' bearings are stored explicitly
                # rather than picking one arbitrarily. az_fwd/az_back come
                # free from the same GEOD.inv() call above (confirmed:
                # az_back is precisely the reverse-direction bearing, not
                # just az_fwd + 180). Consumers (resistance_model.py) pick
                # whichever matches the direction they're actually
                # traversing. Normalized to [0, 360) to match
                # wind_uv_to_speed_direction()'s convention.
                bearing_cell_to_nbr_deg=az_fwd % 360.0,
                bearing_nbr_to_cell_deg=az_back % 360.0,
            )
            edges_added += 1

    elapsed = time.perf_counter() - start

    graph.graph["build_stats"] = {
        "nodes": len(ocean_cells),
        "edges_checked": edges_checked,
        "edges_added": edges_added,
        "edges_pruned_land_bridge": edges_pruned_land_bridge,
        "build_seconds": elapsed,
    }

    if verbose:
        stats = graph.graph["build_stats"]
        print(
            f"build_ocean_graph: {stats['nodes']} nodes, {stats['edges_checked']} candidate "
            f"edges checked, {stats['edges_added']} added, "
            f"{stats['edges_pruned_land_bridge']} pruned as land-bridges, "
            f"{stats['build_seconds']:.2f}s"
        )

    return graph


def save_graph(graph: nx.Graph, path: Path = CACHE_PATH) -> None:
    """Cache the built graph with plain pickle. NOT nx.write_gpickle() —
    that function (and read_gpickle) was removed in NetworkX 3.x."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(graph, f)


def load_graph(path: Path = CACHE_PATH) -> nx.Graph:
    with open(path, "rb") as f:
        return pickle.load(f)
