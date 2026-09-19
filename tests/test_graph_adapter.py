import networkx as nx
import pytest
from pyproj import Geod
from shapely.geometry import Polygon

from navi_opt.grid.graph_builder import build_ocean_graph
from navi_opt.grid.h3_ocean_grid import cell_centroid, generate_ocean_cells_in_bbox
from navi_opt.routing.a_star import CalmWaterAStar
from navi_opt.routing.graph_adapter import to_routable_digraph
from navi_opt.routing.k_shortest import YenKShortestPaths

GEOD = Geod(ellps="WGS84")
BBOX = dict(west=8, south=48, east=13, north=53)
SQUARE_LAND = Polygon([(10, 50), (11, 50), (11, 51), (10, 51)])


def _real_undirected_ocean_graph() -> nx.Graph:
    ocean_cells = generate_ocean_cells_in_bbox(land_union=SQUARE_LAND, resolution=4, **BBOX)
    return build_ocean_graph(ocean_cells, land_union=SQUARE_LAND)


def test_result_is_a_digraph():
    g = _real_undirected_ocean_graph()
    d = to_routable_digraph(g)
    assert isinstance(d, nx.DiGraph)


def test_every_node_has_lat_lon_matching_cell_centroid():
    g = _real_undirected_ocean_graph()
    d = to_routable_digraph(g)
    for node in d.nodes():
        expected_lon, expected_lat = cell_centroid(node)
        assert d.nodes[node]["lon"] == pytest.approx(expected_lon)
        assert d.nodes[node]["lat"] == pytest.approx(expected_lat)


def test_every_undirected_edge_becomes_two_directed_edges():
    g = _real_undirected_ocean_graph()
    d = to_routable_digraph(g)
    assert d.number_of_edges() == 2 * g.number_of_edges()
    for u, v in g.edges():
        assert d.has_edge(u, v)
        assert d.has_edge(v, u)


def test_dist_nm_is_symmetric_and_matches_source_graph():
    g = _real_undirected_ocean_graph()
    d = to_routable_digraph(g)
    for u, v, data in g.edges(data=True):
        assert d[u][v]["dist_nm"] == pytest.approx(data["dist_nm"])
        assert d[v][u]["dist_nm"] == pytest.approx(data["dist_nm"])


def test_bearing_deg_matches_independent_geod_computation_per_direction():
    # The actual point of this adapter: each directed edge's bearing must
    # be the real bearing FOR THAT DIRECTION, not whatever
    # build_ocean_graph()'s cell_to_nbr/nbr_to_cell fields happened to
    # store based on undocumented iteration order.
    g = _real_undirected_ocean_graph()
    d = to_routable_digraph(g)
    for u, v in d.edges():
        u_lon, u_lat = d.nodes[u]["lon"], d.nodes[u]["lat"]
        v_lon, v_lat = d.nodes[v]["lon"], d.nodes[v]["lat"]
        expected_az, _, _ = GEOD.inv(u_lon, u_lat, v_lon, v_lat)
        assert d[u][v]["bearing_deg"] == pytest.approx(expected_az % 360.0, abs=1e-6)


def test_reverse_bearings_are_roughly_opposite_not_equal():
    g = _real_undirected_ocean_graph()
    d = to_routable_digraph(g)
    for u, v in g.edges():
        fwd = d[u][v]["bearing_deg"]
        back = d[v][u]["bearing_deg"]
        diff = abs(fwd - back)
        diff = min(diff, 360 - diff)
        assert 170 <= diff <= 190


def test_adapted_graph_is_directly_usable_by_calm_water_a_star():
    # The real point: no KeyError, no AttributeError — a live, non-trivial
    # route actually gets found end-to-end.
    g = _real_undirected_ocean_graph()
    d = to_routable_digraph(g)

    origin, destination = None, None
    for node in d.nodes():
        for other in d.nodes():
            if other != node and nx.has_path(d, node, other):
                # prefer a pair with some hops between them, not adjacent
                try:
                    hops = nx.shortest_path_length(d, node, other)
                except nx.NetworkXNoPath:
                    continue
                if hops >= 3:
                    origin, destination = node, other
                    break
        if origin:
            break

    assert origin is not None and destination is not None, "test graph too small/disconnected"

    def cost_evaluator(u, v, t):
        hours = d[u][v]["dist_nm"] / 16.0
        return hours, hours

    result = CalmWaterAStar(d, cost_evaluator, v_max_knots=16.0).solve(origin, destination)
    assert result is not None
    assert result["path"][0] == origin
    assert result["path"][-1] == destination


def test_adapted_graph_is_directly_usable_by_yen_k_shortest():
    g = _real_undirected_ocean_graph()
    d = to_routable_digraph(g)

    # pick any two connected, non-adjacent nodes
    origin, destination = None, None
    for node in d.nodes():
        for other in d.nodes():
            if other != node and nx.has_path(d, node, other):
                if nx.shortest_path_length(d, node, other) >= 3:
                    origin, destination = node, other
                    break
        if origin:
            break
    assert origin is not None

    def cost_evaluator(u, v, t):
        hours = d[u][v]["dist_nm"] / 16.0
        return hours, hours

    results = YenKShortestPaths(d, cost_evaluator, v_max_knots=16.0).find_k_paths(
        origin, destination, k=2
    )
    assert len(results) >= 1
    for r in results:
        assert r["path"][0] == origin
        assert r["path"][-1] == destination
