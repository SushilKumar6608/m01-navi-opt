"""
Yen's k-shortest-loopless-paths algorithm over the ocean graph.

DESIGN NOTE — this is Phase 1's candidate-corridor generator, not a
dynamic-fuel router: it must be instantiated with a `cost_evaluator` that
returns calm-water distance or transit time (see CalmWaterAStar's
docstring in a_star.py for exactly why) — never a dynamic,
weather-dependent fuel cost. Passing a fuel evaluator here would break
both the heuristic's units (fuel tonnes vs. an hours-based bound) and
node-settling correctness (single-label dominance isn't FIFO-safe once
cost genuinely varies with arrival time). Dynamic weather-fuel
optimization belongs in Phase 2's MILP/NLP, evaluated along the small,
already-fixed set of k corridors this class hands it — a search space
small enough that FIFO-unsafe settling isn't a risk, because Phase 2
evaluates fixed paths rather than searching over new ones.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import networkx as nx

from navi_opt.routing.a_star import CalmWaterAStar


class YenKShortestPaths:
    """
    Implements Yen's algorithm over a NetworkX directed sea graph using
    CalmWaterAStar to produce topologically distinct candidate maritime
    corridors (Phase 1). Feed the resulting k paths to Phase 2's
    MILP/NLP solver for dynamic weather-fuel speed optimization — do not
    swap in a dynamic fuel cost_evaluator here (see module docstring).
    """

    def __init__(
        self,
        graph: nx.DiGraph,
        cost_evaluator: Callable[[str, str, float], Tuple[float, float]],
        v_max_knots: float = 16.0,
    ):
        self.graph = graph
        self.cost_evaluator = cost_evaluator
        self.v_max = v_max_knots

    def _compute_path_cost_and_time(
        self, path: List[str], start_time: float
    ) -> Tuple[float, float]:
        """Calculates exact cumulative edge cost and elapsed time across a
        path sequence, by replaying it through the same `cost_evaluator`
        used everywhere else — not a static edge-weight sum. This matters
        because cost per leg depends on the accumulated arrival time at
        each node (weather conditions at that ETA), so root-path cost has
        to carry that time state forward exactly as a live traversal
        would, or it won't be commensurate with the spur-path cost it gets
        added to.
        """
        total_cost = 0.0
        current_t = start_time
        for idx in range(len(path) - 1):
            c, dt = self.cost_evaluator(path[idx], path[idx + 1], current_t)
            total_cost += c
            current_t += dt
        return total_cost, current_t

    def find_k_paths(
        self,
        origin: str,
        destination: str,
        k: int = 3,
        start_time_hours: float = 0.0,
    ) -> List[Dict]:
        finder = CalmWaterAStar(self.graph, self.cost_evaluator, self.v_max)
        initial_route = finder.solve(origin, destination, start_time_hours)
        if not initial_route:
            return []

        # A holds the determined shortest paths in ascending cost order
        A: List[Dict] = [initial_route]
        # B holds potential candidate paths, persisted across outer
        # iterations (a candidate not promoted at iteration i can still
        # become the best available candidate at iteration i+1)
        B: List[Dict] = []

        for i in range(1, k):
            prev_path = A[i - 1]["path"]

            for spur_idx in range(len(prev_path) - 1):
                spur_node = prev_path[spur_idx]
                root_path = prev_path[: spur_idx + 1]

                # Cumulative cost and arrival timestamp up to the spur node —
                # computed via the real cost_evaluator + accumulated time,
                # not a stray reference to spur_res["total_cost"].
                root_cost, root_time = self._compute_path_cost_and_time(
                    root_path, start_time_hours
                )

                # Temporarily remove edges sharing the root path prefix in
                # previously found paths (including prev_path itself) so
                # the spur search can't just retrace an already-found route.
                removed_edges = []
                for p in A:
                    if (
                        len(p["path"]) > spur_idx + 1
                        and p["path"][: spur_idx + 1] == root_path
                    ):
                        u = p["path"][spur_idx]
                        v = p["path"][spur_idx + 1]
                        if self.graph.has_edge(u, v):
                            edge_attr = self.graph[u][v]
                            self.graph.remove_edge(u, v)
                            removed_edges.append((u, v, edge_attr))

                # Temporarily remove nodes in root_path (excluding the spur
                # node itself) so the spur path can't loop back into its
                # own root.
                removed_nodes = []
                for node in root_path[:-1]:
                    if node in self.graph:
                        node_data = dict(self.graph.nodes[node])
                        node_edges_in = list(self.graph.in_edges(node, data=True))
                        node_edges_out = list(self.graph.out_edges(node, data=True))
                        self.graph.remove_node(node)
                        removed_nodes.append((node, node_data, node_edges_in, node_edges_out))

                # Calculate the spur path from the spur node to the destination
                spur_res = finder.solve(spur_node, destination, start_time_hours=root_time)
                if spur_res:
                    candidate_path = root_path[:-1] + spur_res["path"]
                    total_cost = root_cost + spur_res["total_cost"]

                    candidate_dict = {
                        "path": candidate_path,
                        "total_cost": round(total_cost, 2),
                        "arrival_time_hours": round(spur_res["arrival_time_hours"], 2),
                        "legs": len(candidate_path) - 1,
                    }

                    # Add candidate if not already discovered
                    if not any(cand["path"] == candidate_path for cand in B) and not any(
                        cand["path"] == candidate_path for cand in A
                    ):
                        B.append(candidate_dict)

                # Restore removed edges and nodes
                for u, v, attr in removed_edges:
                    self.graph.add_edge(u, v, **attr)
                for node, node_data, in_edges, out_edges in removed_nodes:
                    self.graph.add_node(node, **node_data)
                    for u, v, d in in_edges:
                        self.graph.add_edge(u, v, **d)
                    for u, v, d in out_edges:
                        self.graph.add_edge(u, v, **d)

            if not B:
                break

            # Sort candidate pool and promote lowest-cost candidate to A
            B.sort(key=lambda x: x["total_cost"])
            A.append(B.pop(0))

        return A
