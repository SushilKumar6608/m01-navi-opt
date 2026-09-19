"""
Bridges `graph_builder.build_ocean_graph()`'s output to the schema
`CalmWaterAStar` / `YenKShortestPaths` expect. These were built and tested
independently (see findings.md's Phase 2 entries) and their interfaces
don't match on three points:

1. `build_ocean_graph()` returns an undirected `nx.Graph`; the routing
   classes call `graph.successors()` / `graph.in_edges()` / `graph.out_edges()`,
   which only exist on `nx.DiGraph`.
2. `build_ocean_graph()`'s nodes are bare H3 cell-ID strings with no
   `lat`/`lon` attributes (only edges carry geodesic data) — but
   `CalmWaterAStar._heuristic()` reads `graph.nodes[node]["lat"]`.
3. A naive `nx.Graph.to_directed()` would copy each undirected edge's
   attribute dict verbatim onto BOTH resulting directed edges — including
   `bearing_cell_to_nbr_deg`/`bearing_nbr_to_cell_deg`, whose meaning
   depends on which endpoint happened to be the outer-loop `cell` when
   `build_ocean_graph()` first added that edge (iteration-order-dependent,
   not part of that function's documented contract). Trusting that mapping
   from outside `build_ocean_graph()` would be fragile; this module
   recomputes bearing directly per direction instead, which is just as
   cheap and has no such ambiguity.
"""
from __future__ import annotations

import networkx as nx
from pyproj import Geod

from navi_opt.grid.h3_ocean_grid import cell_centroid

GEOD = Geod(ellps="WGS84")


def to_routable_digraph(graph: nx.Graph) -> nx.DiGraph:
    """Convert build_ocean_graph()'s undirected sea-lane graph into a
    directed graph usable by CalmWaterAStar / YenKShortestPaths:
    every node gets 'lat'/'lon' (from cell_centroid), and every undirected
    edge becomes two directed edges, each carrying:
      - dist_nm: copied as-is (distance is direction-independent)
      - bearing_deg: the travel bearing FOR THAT DIRECTION, recomputed
        fresh via pyproj.Geod rather than read from the undirected edge's
        cell_to_nbr/nbr_to_cell fields (see module docstring).
    """
    digraph = nx.DiGraph()

    for node in graph.nodes():
        lon, lat = cell_centroid(node)
        digraph.add_node(node, lat=lat, lon=lon)

    for u, v, data in graph.edges(data=True):
        u_lon, u_lat = digraph.nodes[u]["lon"], digraph.nodes[u]["lat"]
        v_lon, v_lat = digraph.nodes[v]["lon"], digraph.nodes[v]["lat"]
        dist_nm = data["dist_nm"]

        az_u_to_v, az_v_to_u, _ = GEOD.inv(u_lon, u_lat, v_lon, v_lat)
        digraph.add_edge(u, v, dist_nm=dist_nm, bearing_deg=az_u_to_v % 360.0)
        digraph.add_edge(v, u, dist_nm=dist_nm, bearing_deg=az_v_to_u % 360.0)

    return digraph
