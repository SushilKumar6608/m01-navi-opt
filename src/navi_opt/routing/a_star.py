"""
A* pathfinder over the ocean graph, scoped to STATIC (time-invariant) edge
costs — e.g. great-circle distance or calm-water transit time.

Renamed from an earlier `TimeDependentAStar` draft: the class accepted a
`current_time_hours` argument threaded through `cost_evaluator` and its
name implied it handled dynamic, time-varying cost correctly. It doesn't,
for two independent reasons, both load-bearing:

1. Node settling is single-label (`best_g_cost` keyed only by node). A
   label reaching a node at lower cost-so-far but later arrival time will
   always evict one that arrived earlier at higher cost — even when the
   later arrival means the rest of its route runs straight into weather
   the earlier-arriving label would have dodged. That's only safe when
   `cost_evaluator`'s output doesn't meaningfully depend on the time it's
   evaluated at (a true FIFO/monotonic cost), which calm-water distance
   or transit time trivially satisfies and a dynamic weather-fuel cost
   does not.
2. `_heuristic()` returns a lower bound on TRANSIT TIME (great-circle
   distance / v_max, in hours) and is added directly to `g` to rank the
   open set. That's only a valid (admissible) bound when `g` itself
   accumulates time — if `cost_evaluator` instead returns fuel tonnes as
   its cost, `g + h` mixes tonnes and hours, which isn't a lower bound on
   anything and voids A*'s optimality guarantee outright.

Conclusion (see findings.md): this class is correct and appropriate for
Phase 1 candidate-corridor generation (Yen's k-shortest over
`YenKShortestPaths`, driven by a calm-water distance/time cost_evaluator
that ignores `current_time_hours`). It must NOT be reused as-is with a
dynamic, weather-dependent fuel cost_evaluator — that needs a fuel-based
admissible heuristic (e.g. calm-water minimum fuel-per-nm at the vessel's
most efficient speed, using the ballast factor as the valid lower bound
across cargo conditions) and multi-label settling over (node,
arrival_time), which is deliberately out of scope here: Phase 2 evaluates
dynamic weather-fuel cost via MILP/NLP over the small, already-fixed set
of k corridors Phase 1 hands it, rather than searching a new graph under
time-dependent cost.
"""
from __future__ import annotations

import heapq
import math
from typing import Callable, Dict, List, Optional, Tuple

import networkx as nx

EARTH_RADIUS_NM = 3440.065  # International nautical miles radius


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes great-circle distance between two coordinates in nautical miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_NM * math.asin(math.sqrt(max(0.0, min(1.0, a))))


class CalmWaterAStar:
    """
    A* pathfinder over a sea-lane NetworkX graph, using an admissible
    great-circle lower-bound heuristic based on maximum speed.

    IMPORTANT — `cost_evaluator` must return a cost that is commensurate
    with the heuristic (hours) and does not meaningfully vary with the
    `current_time_hours` it's called with — e.g. great-circle distance or
    calm-water transit time. Passing a dynamic, weather-dependent fuel
    cost_evaluator here breaks both admissibility (unit mismatch between
    `g` and `h`) and correctness of node settling (single-label dominance
    is not FIFO-safe under time-varying cost). See the module docstring.
    """

    def __init__(
        self,
        graph: nx.DiGraph,
        cost_evaluator: Callable[[str, str, float], Tuple[float, float]],
        v_max_knots: float = 16.0,
    ):
        """
        Parameters
        ----------
        graph : nx.DiGraph
            Directed ocean graph where nodes have 'lat' and 'lon' attributes.
        cost_evaluator : Callable[[str, str, float], Tuple[float, float]]
            Function signature: (u, v, current_time_hours) -> (incremental_cost, leg_duration_hours).
            `incremental_cost` must be in the same units as the heuristic
            (hours) and must not meaningfully depend on `current_time_hours`
            — see class docstring.
        v_max_knots : float
            Maximum service speed in knots used to compute the admissible heuristic.
        """
        self.graph = graph
        self.cost_evaluator = cost_evaluator
        self.v_max = v_max_knots

    def _heuristic(self, node: str, target: str) -> float:
        """Admissible lower bound (in hours) on transit time to target."""
        node_data = self.graph.nodes[node]
        target_data = self.graph.nodes[target]
        dist_nm = haversine_nm(
            node_data["lat"], node_data["lon"], target_data["lat"], target_data["lon"]
        )
        return dist_nm / self.v_max

    def solve(
        self,
        origin: str,
        destination: str,
        start_time_hours: float = 0.0,
    ) -> Optional[Dict]:
        if origin not in self.graph or destination not in self.graph:
            return None

        # Priority Queue entries: (f_score, g_cost, current_time, current_node, path)
        open_set: List[Tuple[float, float, float, str, List[str]]] = []
        h0 = self._heuristic(origin, destination)
        heapq.heappush(open_set, (h0, 0.0, start_time_hours, origin, [origin]))

        best_g_cost: Dict[str, float] = {origin: 0.0}

        while open_set:
            f, g, current_t, u, path = heapq.heappop(open_set)

            if u == destination:
                return {
                    "path": path,
                    "total_cost": round(g, 2),
                    "arrival_time_hours": round(current_t, 2),
                    "legs": len(path) - 1,
                }

            if g > best_g_cost.get(u, float("inf")):
                continue

            for v in self.graph.successors(u):
                edge_cost, leg_hours = self.cost_evaluator(u, v, current_t)
                tentative_g = g + edge_cost
                arr_time = current_t + leg_hours

                if tentative_g < best_g_cost.get(v, float("inf")):
                    best_g_cost[v] = tentative_g
                    h_score = self._heuristic(v, destination)
                    heapq.heappush(
                        open_set,
                        (tentative_g + h_score, tentative_g, arr_time, v, path + [v]),
                    )

        return None
