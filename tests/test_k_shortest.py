import copy

import networkx as nx
import pytest

from navi_opt.routing.a_star import CalmWaterAStar, haversine_nm
from navi_opt.routing.k_shortest import YenKShortestPaths

V_MAX = 14.0  # knots, arbitrary for these tests


def calm_water_cost_evaluator(graph: nx.DiGraph):
    """Same calm-water contract as test_a_star.py: cost == duration ==
    great-circle transit time in hours. YenKShortestPaths must only ever be
    driven by an evaluator like this one (see k_shortest.py's module
    docstring) — a dynamic fuel evaluator is explicitly out of scope."""

    def _evaluator(u: str, v: str, current_t: float):
        lat1, lon1 = graph.nodes[u]["lat"], graph.nodes[u]["lon"]
        lat2, lon2 = graph.nodes[v]["lat"], graph.nodes[v]["lon"]
        hours = haversine_nm(lat1, lon1, lat2, lon2) / V_MAX
        return hours, hours

    return _evaluator


def _branching_graph() -> nx.DiGraph:
    """Exactly three simple A -> D paths, deliberately constructed so one
    pair shares a multi-hop prefix (A-B-C) and diverges only at C — this is
    what actually exercises a spur search at spur_idx > 0, i.e. a root_path
    with nonzero cost, which is precisely the case the original
    root_path_cost bug (using spur_res["total_cost"] alone, ignoring the
    accumulated prefix cost) would get wrong:

      A -> B -> C -> D          (short, direct)
      A -> B -> C -> E -> D     (shares A-B-C, detours via E after C)
      A -> F -> D               (unrelated, larger detour)
    """
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=0.0, lon=0.3)
    g.add_node("C", lat=0.0, lon=0.6)
    g.add_node("D", lat=0.0, lon=1.0)
    g.add_node("E", lat=0.3, lon=0.8)
    g.add_node("F", lat=3.0, lon=0.5)
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    g.add_edge("C", "D")
    g.add_edge("C", "E")
    g.add_edge("E", "D")
    g.add_edge("A", "F")
    g.add_edge("F", "D")
    return g


def _independent_full_path_cost(graph: nx.DiGraph, path, evaluator, start_time_hours=0.0):
    """Recomputes a path's total cost from scratch via the evaluator,
    independent of anything internal to YenKShortestPaths — the ground
    truth a candidate's reported total_cost must match."""
    total = 0.0
    t = start_time_hours
    for u, v in zip(path, path[1:]):
        c, dt = evaluator(u, v, t)
        total += c
        t += dt
    return total, t


# --- find_k_paths ---


def test_find_k_paths_with_k_1_matches_plain_a_star():
    g = _branching_graph()
    evaluator = calm_water_cost_evaluator(g)

    direct = CalmWaterAStar(g, evaluator, v_max_knots=V_MAX).solve("A", "D")
    yen_result = YenKShortestPaths(g, evaluator, v_max_knots=V_MAX).find_k_paths(
        "A", "D", k=1
    )

    assert len(yen_result) == 1
    assert yen_result[0]["path"] == direct["path"]
    assert yen_result[0]["total_cost"] == pytest.approx(direct["total_cost"], abs=0.01)


def test_find_k_paths_returns_exactly_the_three_available_simple_paths():
    g = _branching_graph()
    evaluator = calm_water_cost_evaluator(g)
    results = YenKShortestPaths(g, evaluator, v_max_knots=V_MAX).find_k_paths(
        "A", "D", k=3
    )

    expected_paths = {
        ("A", "B", "C", "D"),
        ("A", "B", "C", "E", "D"),
        ("A", "F", "D"),
    }
    returned_paths = {tuple(r["path"]) for r in results}
    assert returned_paths == expected_paths


def test_find_k_paths_is_sorted_ascending_by_total_cost():
    g = _branching_graph()
    evaluator = calm_water_cost_evaluator(g)
    results = YenKShortestPaths(g, evaluator, v_max_knots=V_MAX).find_k_paths(
        "A", "D", k=3
    )
    costs = [r["total_cost"] for r in results]
    assert costs == sorted(costs)


def test_find_k_paths_stops_early_when_fewer_than_k_paths_exist():
    # Only 3 simple A->D paths exist in this graph; asking for k=10 must not
    # error, it should just return what's actually findable.
    g = _branching_graph()
    evaluator = calm_water_cost_evaluator(g)
    results = YenKShortestPaths(g, evaluator, v_max_knots=V_MAX).find_k_paths(
        "A", "D", k=10
    )
    assert len(results) == 3


def test_find_k_paths_returns_empty_list_for_unreachable_destination():
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("D", lat=0.0, lon=1.0)
    # no path at all
    evaluator = calm_water_cost_evaluator(g)
    results = YenKShortestPaths(g, evaluator, v_max_knots=V_MAX).find_k_paths(
        "A", "D", k=3
    )
    assert results == []


def test_every_candidate_total_cost_matches_independent_recomputation():
    # Direct regression test for the root_path_cost bug: an earlier draft
    # used spur_res["total_cost"] alone as a candidate's total cost, which
    # silently drops the accumulated cost of the path prefix for any
    # candidate whose spur node isn't the origin itself (spur_idx > 0).
    # The A-B-C-E-D candidate here spurs off C (spur_idx=2, nonzero-cost
    # root_path=[A,B,C]) and is exactly the case that bug would get wrong.
    g = _branching_graph()
    evaluator = calm_water_cost_evaluator(g)
    results = YenKShortestPaths(g, evaluator, v_max_knots=V_MAX).find_k_paths(
        "A", "D", k=3, start_time_hours=5.0
    )

    assert len(results) == 3
    for r in results:
        expected_cost, expected_arrival = _independent_full_path_cost(
            g, r["path"], evaluator, start_time_hours=5.0
        )
        assert r["total_cost"] == pytest.approx(expected_cost, abs=0.01)
        assert r["arrival_time_hours"] == pytest.approx(expected_arrival, abs=0.01)


def test_graph_is_left_unmodified_after_find_k_paths():
    # Regression test for the node-attribute-loss bug: Yen's temporarily
    # removes root-path nodes and restores them afterward. An earlier draft
    # only replayed the removed node's edges on restore, not its own
    # lat/lon attribute dict, so a restored node came back bare — silently
    # corrupting the graph for any caller that reuses it afterward.
    g = _branching_graph()
    nodes_before = copy.deepcopy(dict(g.nodes(data=True)))
    edges_before = sorted(g.edges())

    evaluator = calm_water_cost_evaluator(g)
    YenKShortestPaths(g, evaluator, v_max_knots=V_MAX).find_k_paths("A", "D", k=3)

    assert sorted(g.edges()) == edges_before
    assert dict(g.nodes(data=True)) == nodes_before


def test_find_k_paths_is_safely_reusable_on_the_same_graph():
    # If node attributes leaked away during the first call, this second
    # call would KeyError inside CalmWaterAStar._heuristic() the moment it
    # tried to read a stripped node's lat/lon.
    g = _branching_graph()
    evaluator = calm_water_cost_evaluator(g)
    solver = YenKShortestPaths(g, evaluator, v_max_knots=V_MAX)

    first = solver.find_k_paths("A", "D", k=3)
    second = solver.find_k_paths("A", "D", k=3)

    assert {tuple(r["path"]) for r in first} == {tuple(r["path"]) for r in second}
