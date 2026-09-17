from shapely.geometry import Polygon
from shapely.prepared import prep

from navi_opt.grid.graph_builder import _segment_crosses_land, build_ocean_graph
from navi_opt.grid.h3_ocean_grid import cell_centroid, generate_ocean_cells_in_bbox

SQUARE_LAND = Polygon([(10, 50), (11, 50), (11, 51), (10, 51)])
BBOX = dict(west=8, south=48, east=13, north=53)


def test_segment_crosses_land_detects_a_straight_line_through_land():
    prepared = prep(SQUARE_LAND)
    # (9, 50.5) is west of the land square, (12, 50.5) is east of it — the
    # straight line between them cuts directly through the square.
    assert _segment_crosses_land(9.0, 50.5, 12.0, 50.5, prepared) is True


def test_segment_crosses_land_is_false_for_a_clear_ocean_path():
    prepared = prep(SQUARE_LAND)
    # Both points well south of the land square — a clear path.
    assert _segment_crosses_land(9.0, 45.0, 12.0, 45.0, prepared) is False


def test_land_bridge_between_two_ocean_cells_is_pruned():
    # This is the actual bug class this module exists to catch: two cells
    # that are BOTH individually classified as ocean (their centroids are
    # not on land), and are H3 grid-neighbors of each other, but a thin
    # landmass sits directly on the straight line between their centroids
    # (a headland/peninsula pinch point) — so the naive "neighbor + both
    # ocean" test alone would wrongly add this edge.
    #
    # Construction: first find a real pair of H3-adjacent ocean cells with
    # no land at all, then build a thin land strip that crosses the
    # straight line between their centroids without touching either
    # centroid itself — verified directly below, not assumed.
    u, v = "851f1e33fffffff", "851f1e07fffffff"
    u_lon, u_lat = cell_centroid(u)
    v_lon, v_lat = cell_centroid(v)
    assert u_lon > 11.76 and v_lon < 11.70  # both centroids sit outside the strip below

    land_strip = Polygon([(11.70, 48), (11.76, 48), (11.76, 55), (11.70, 55)])

    ocean_cells = generate_ocean_cells_in_bbox(land_union=land_strip, resolution=5, **BBOX)
    assert u in ocean_cells and v in ocean_cells  # both still classified as ocean

    graph = build_ocean_graph(ocean_cells, land_union=land_strip)

    assert not graph.has_edge(u, v), (
        "edge between two ocean-classified cells should have been pruned — "
        "the straight line between their centroids crosses the land strip"
    )
    assert graph.graph["build_stats"]["edges_pruned_land_bridge"] > 0


def test_build_ocean_graph_basic_properties():
    ocean_cells = generate_ocean_cells_in_bbox(land_union=SQUARE_LAND, resolution=4, **BBOX)
    graph = build_ocean_graph(ocean_cells, land_union=SQUARE_LAND)

    assert set(graph.nodes()) == ocean_cells
    assert graph.number_of_edges() > 0

    for _, _, data in graph.edges(data=True):
        assert data["dist_nm"] > 0
        assert 0.0 <= data["bearing_cell_to_nbr_deg"] < 360.0
        assert 0.0 <= data["bearing_nbr_to_cell_deg"] < 360.0
        # the two directions' bearings should be roughly opposite (~180°
        # apart), not equal or arbitrary — sanity check on the az_fwd/
        # az_back distinction, not just that some number got stored
        diff = abs(data["bearing_cell_to_nbr_deg"] - data["bearing_nbr_to_cell_deg"])
        diff = min(diff, 360 - diff)
        assert 170 <= diff <= 190

    stats = graph.graph["build_stats"]
    assert stats["edges_added"] == graph.number_of_edges()
