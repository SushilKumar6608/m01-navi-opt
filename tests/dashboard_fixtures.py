"""
Shared synthetic scenario for the Phase 7 dashboard tests (and the
Streamlit smoke test). NOT real data and not meant to look like it: a small
made-up chain of nodes loosely shaped like a Rotterdam -> Ceyhan route, so
map/profile code has plausible geometry to draw. Everything that matters is
produced by the REAL project code — the speed profile by
`optimize_speed_profile()`, the reoptimization by `handle_weather_update()` —
so the event dicts have exactly the shapes the real Kafka topics carry.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import Any, Dict, List

import networkx as nx

from navi_opt.optimization.speed_profile import optimize_speed_profile
from navi_opt.routing.a_star import haversine_nm
from navi_opt.streaming.reoptimization_consumer import handle_voyage_departed, handle_weather_update
from navi_opt.streaming.serialization import speed_profile_result_to_dict
from navi_opt.streaming.voyage_registry import DEMO_TIME_SCALE, VoyageRegistry
from navi_opt.weather.vessel_profiles import load_vessel_profile

VESSEL_KEY = "mr2_product_tanker"
VOYAGE_START = datetime(2024, 6, 1)
DEPARTED_AT = datetime(2026, 10, 9, 21, 28)

# Rough waypoints (lat, lon): Rotterdam, Channel, Biscay, Iberia, Gibraltar, Med, Ceyhan.
_WAYPOINTS = [
    (51.95, 4.05), (50.9, 1.4), (49.6, -3.0), (48.4, -5.6), (43.2, -9.5),
    (37.0, -9.0), (36.0, -5.5), (36.6, 0.0), (37.4, 10.0), (35.6, 18.0),
    (34.6, 26.0), (35.6, 32.4), (36.8, 35.9),
]


def make_graph(points_per_segment: int = 2) -> nx.DiGraph:
    g = nx.DiGraph()
    nodes: List[str] = []
    for (lat0, lon0), (lat1, lon1) in zip(_WAYPOINTS, _WAYPOINTS[1:]):
        for k in range(points_per_segment):
            f = k / points_per_segment
            name = f"n{len(nodes):02d}"
            g.add_node(name, lat=lat0 + (lat1 - lat0) * f, lon=lon0 + (lon1 - lon0) * f)
            nodes.append(name)
    last = f"n{len(nodes):02d}"
    g.add_node(last, lat=_WAYPOINTS[-1][0], lon=_WAYPOINTS[-1][1])
    nodes.append(last)
    for a, b in zip(nodes, nodes[1:]):
        d = haversine_nm(g.nodes[a]["lat"], g.nodes[a]["lon"], g.nodes[b]["lat"], g.nodes[b]["lon"])
        g.add_edge(a, b, dist_nm=d)
    g.graph["path"] = nodes
    return g


def make_scenario(fraction_sailed: float = 0.35, with_reoptimization: bool = True) -> Dict[str, Any]:
    """Returns {graph, path, departures, results, weather_events, now}."""
    g = make_graph()
    path: List[str] = g.graph["path"]
    vessel = load_vessel_profile(VESSEL_KEY)
    hi = vessel.speed_bounds_knots[1]
    floor_hours = sum(g[u][v]["dist_nm"] for u, v in zip(path, path[1:])) / hi
    deadline = floor_hours * 1.4

    def calm(u, v, t):
        return 0.0, 0.0

    initial = optimize_speed_profile(
        path, g, vessel, "laden", weather_lookup=calm, start_time_hours=0.0,
        max_transit_hours=deadline, maxiter=40, max_seconds=30.0,
    )
    payload = {
        "voyage_id": "demo-test0001", "vessel_key": VESSEL_KEY, "cargo_condition": "laden",
        "voyage_start_time": VOYAGE_START.isoformat(), "deadline_hours": deadline,
        "demo_departed_at": DEPARTED_AT.isoformat(),
        "committed_profile": speed_profile_result_to_dict(initial),
    }

    cut = max(1, int(len(initial.legs) * fraction_sailed))
    hours_sailed = initial.legs[cut - 1].departure_time_hours + initial.legs[cut - 1].transit_hours + 0.01
    now = DEPARTED_AT + timedelta(seconds=hours_sailed / DEMO_TIME_SCALE * 3600.0)

    results: List[Dict[str, Any]] = []
    weather: List[Dict[str, Any]] = []
    if with_reoptimization:
        rough_edges = set(zip(path[cut + 2:], path[cut + 3:]))

        def rough_ahead(u, v, t):
            return (4.8, 22.0) if (u, v) in rough_edges else (0.0, 0.0)

        registry = VoyageRegistry()
        handle_voyage_departed(registry, payload)
        trigger = (path[cut + 2], path[cut + 3])
        event = {"u": trigger[0], "v": trigger[1], "previous_wave_height_m": 2.0,
                 "new_wave_height_m": 3.0, "sampled_at": now.isoformat(), "injected": True}
        weather.append(event)
        for r in handle_weather_update(registry, g, rough_ahead, event, now):
            results.append(dataclasses.asdict(r))

    return {
        "graph": g, "path": path, "departures": [payload], "results": results,
        "weather_events": weather, "now": now,
    }
