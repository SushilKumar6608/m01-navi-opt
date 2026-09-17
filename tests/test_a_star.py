import math

import networkx as nx
import pytest

from navi_opt.routing.a_star import EARTH_RADIUS_NM, CalmWaterAStar, haversine_nm

V_MAX = 14.0  # knots, arbitrary for these tests


def calm_water_cost_evaluator(graph: nx.DiGraph):
    """Returns a cost_evaluator matching CalmWaterAStar's documented contract:
    cost == duration == great-circle transit time in hours. This is the only
    contract CalmWaterAStar is correct under (see a_star.py's module
    docstring) — anything that returns fuel or another unit here would be
    testing a usage the class explicitly doesn't support."""

    def _evaluator(u: str, v: str, current_t: float):
        lat1, lon1 = graph.nodes[u]["lat"], graph.nodes[u]["lon"]
        lat2, lon2 = graph.nodes[v]["lat"], graph.nodes[v]["lon"]
        hours = haversine_nm(lat1, lon1, lat2, lon2) / V_MAX
        return hours, hours

    return _evaluator


# --- haversine_nm ---


def test_haversine_nm_same_point_is_zero():
    assert haversine_nm(51.5, -0.1, 51.5, -0.1) == pytest.approx(0.0, abs=1e-9)


def test_haversine_nm_is_symmetric():
    a = haversine_nm(10.0, 20.0, 30.0, 40.0)
    b = haversine_nm(30.0, 40.0, 10.0, 20.0)
    assert a == pytest.approx(b)


def test_haversine_nm_quarter_great_circle():
    # Two points 90 degrees apart along the equator are a quarter of the
    # great circle apart: pi/2 * R — a known closed-form value, not just an
    # internally-consistent check against the function's own logic.
    result = haversine_nm(0.0, 0.0, 0.0, 90.0)
    expected = (math.pi / 2.0) * EARTH_RADIUS_NM
    assert result == pytest.approx(expected, rel=1e-6)


# --- CalmWaterAStar.solve() ---


def _diamond_graph() -> nx.DiGraph:
    """A -> B -> D and A -> C -> D, with B on a shorter great-circle detour
    than C, plus a third, longer A -> E -> D route — enough topological
    variety to distinguish "fewest hops" from "lowest cost"."""
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=0.1, lon=0.5)
    g.add_node("C", lat=2.0, lon=0.5)  # further off the direct line than B
    g.add_node("D", lat=0.0, lon=1.0)
    g.add_node("E", lat=5.0, lon=0.5)  # a much longer detour
    g.add_edge("A", "B")
    g.add_edge("B", "D")
    g.add_edge("A", "C")
    g.add_edge("C", "D")
    g.add_edge("A", "E")
    g.add_edge("E", "D")
    return g


def test_solve_finds_a_direct_route():
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=0.0, lon=1.0)
    g.add_edge("A", "B")

    solver = CalmWaterAStar(g, calm_water_cost_evaluator(g), v_max_knots=V_MAX)
    result = solver.solve("A", "B")

    assert result is not None
    assert result["path"] == ["A", "B"]
    expected_hours = haversine_nm(0.0, 0.0, 0.0, 1.0) / V_MAX
    assert result["total_cost"] == pytest.approx(expected_hours, abs=0.01)
    assert result["arrival_time_hours"] == pytest.approx(expected_hours, abs=0.01)


def test_solve_prefers_lower_cost_route_not_fewest_hops():
    # Both A-B-D and A-E-D are 2-hop routes, but A-E-D detours much further
    # off the direct line than A-B-D. A hop-count-only search would treat
    # them as tied; a cost-driven search must not.
    g = _diamond_graph()
    solver = CalmWaterAStar(g, calm_water_cost_evaluator(g), v_max_knots=V_MAX)
    result = solver.solve("A", "D")

    assert result["path"] == ["A", "B", "D"]


def test_solve_returns_none_for_unreachable_destination():
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=1.0, lon=1.0)
    # no edge between them
    solver = CalmWaterAStar(g, calm_water_cost_evaluator(g), v_max_knots=V_MAX)
    assert solver.solve("A", "B") is None


def test_solve_returns_none_when_origin_or_destination_missing():
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    solver = CalmWaterAStar(g, calm_water_cost_evaluator(g), v_max_knots=V_MAX)
    assert solver.solve("A", "nonexistent") is None
    assert solver.solve("nonexistent", "A") is None


def test_solve_respects_start_time_offset():
    # A non-zero start_time_hours should be carried straight through into
    # arrival_time_hours (cost_evaluator here doesn't vary with current_t,
    # but the bookkeeping itself must still thread it through correctly).
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=0.0, lon=1.0)
    g.add_edge("A", "B")

    solver = CalmWaterAStar(g, calm_water_cost_evaluator(g), v_max_knots=V_MAX)
    result = solver.solve("A", "B", start_time_hours=100.0)

    leg_hours = haversine_nm(0.0, 0.0, 0.0, 1.0) / V_MAX
    assert result["arrival_time_hours"] == pytest.approx(100.0 + leg_hours, abs=0.01)


def test_total_cost_matches_sum_of_leg_costs_along_returned_path():
    g = _diamond_graph()
    evaluator = calm_water_cost_evaluator(g)
    solver = CalmWaterAStar(g, evaluator, v_max_knots=V_MAX)
    result = solver.solve("A", "D")

    manual_cost = 0.0
    t = 0.0
    for u, v in zip(result["path"], result["path"][1:]):
        c, dt = evaluator(u, v, t)
        manual_cost += c
        t += dt

    assert result["total_cost"] == pytest.approx(manual_cost, abs=0.01)
    assert result["arrival_time_hours"] == pytest.approx(t, abs=0.01)
