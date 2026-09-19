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

from typing import Callable, Dict, List, Optional, Set, Tuple

import networkx as nx

from navi_opt.routing.a_star import CalmWaterAStar


class YenKShortestPaths:
    """
    Implements Yen's algorithm over a NetworkX directed sea graph using
    CalmWaterAStar to produce topologically distinct candidate maritime
    corridors (Phase 1). Feed the resulting k paths to Phase 2's
    MILP/NLP solver for dynamic weather-fuel speed optimization — do not
    swap in a dynamic fuel cost_evaluator here (see module docstring).

    DIVERSITY — `enforce_diversity=False` by default (plain Yen's, exactly
    the behavior this class has always had). On an open, locally symmetric
    grid (e.g. wide-open Atlantic/Mediterranean water at a fixed H3
    resolution), plain Yen's tends to degenerate: the k-th path is often
    just a 1-2 hex sideways wiggle of the (k-1)-th, not a genuinely
    different navigational choice, because nothing in vanilla Yen's biases
    the search away from the geometric neighborhood of an already-found
    route — only the exact edges/nodes shared with prior paths' identical
    prefixes get removed, which is a much narrower exclusion than "don't
    route through this same general area again." Set
    `enforce_diversity=True` to fix that with two complementary
    mechanisms:

    1. Corridor penalty: once a path is accepted into the result set,
       every node within `diversity_buffer_hops` graph-hops of it (not an
       H3-specific `k_ring` — using the graph's own hop-neighborhood keeps
       this class H3-agnostic, so it still works against the synthetic
       letter-node test graphs this module is tested with) gets its
       outgoing/incoming edge costs multiplied by
       `(1 + diversity_penalty_factor)` for all subsequent spur searches.
       This is a SEARCH-TIME bias only — it steers later searches away
       from the buffered corridor without forbidding it outright (a
       genuinely necessary shared chokepoint, e.g. the only lane through a
       strait, can still be re-used if there's truly no alternative).
    2. Jaccard acceptance gate: a candidate is only promoted from the
       pool into the accepted set if its node-set Jaccard similarity
       against EVERY already-accepted path is below
       `diversity_jaccard_threshold`. Candidates that fail this are
       dropped, not retried later, and if no sufficiently diverse
       candidate remains, `find_k_paths` returns fewer than `k` paths
       rather than accepting a near-duplicate — the same graceful
       degradation already used when the graph simply doesn't contain k
       distinct simple paths at all.

    Critically, the penalty affects ONLY which path the search finds —
    every path's reported `total_cost`/`arrival_time_hours` is always
    recomputed via the TRUE (unpenalized) `cost_evaluator` before being
    stored, never the inflated search-time cost. Comparing/ranking
    corridors on an artificially inflated number would misrepresent their
    real cost to anything downstream (a person, or Phase 2's solver).
    """

    def __init__(
        self,
        graph: nx.DiGraph,
        cost_evaluator: Callable[[str, str, float], Tuple[float, float]],
        v_max_knots: float = 16.0,
        enforce_diversity: bool = False,
        diversity_penalty_factor: float = 0.20,
        diversity_buffer_hops: int = 1,
        diversity_jaccard_threshold: float = 0.80,
    ):
        self.graph = graph
        self.cost_evaluator = cost_evaluator
        self.v_max = v_max_knots
        self.enforce_diversity = enforce_diversity
        self.diversity_penalty_factor = diversity_penalty_factor
        self.diversity_buffer_hops = diversity_buffer_hops
        self.diversity_jaccard_threshold = diversity_jaccard_threshold
        self._penalized_nodes: Set[str] = set()

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

    def _search_cost_evaluator(self, u: str, v: str, current_t: float) -> Tuple[float, float]:
        """Wraps `cost_evaluator` with the diversity penalty for SEARCH
        purposes only — never used for a reported total_cost. Duration
        (`dt`) is left untouched; only cost is penalized, since inflating
        duration would corrupt arrival-time bookkeeping for no reason."""
        cost, dt = self.cost_evaluator(u, v, current_t)
        if u in self._penalized_nodes or v in self._penalized_nodes:
            cost *= 1.0 + self.diversity_penalty_factor
        return cost, dt

    def _grow_penalty_buffer(self, path: List[str]) -> None:
        """Adds every node within `diversity_buffer_hops` graph-hops of
        `path` to the penalized set, so future spur searches are biased
        away from this corridor's general vicinity, not just its exact
        edges. Uses the graph's own topology (nx.ego_graph), not an
        H3-specific ring — keeps this class working against any graph,
        including the synthetic letter-node graphs it's tested with."""
        for node in path:
            if node not in self.graph:
                continue
            buffer = nx.ego_graph(
                self.graph, node, radius=self.diversity_buffer_hops, undirected=True
            )
            self._penalized_nodes.update(buffer.nodes())

    @staticmethod
    def _jaccard_similarity(path_a: List[str], path_b: List[str]) -> float:
        set_a, set_b = set(path_a), set(path_b)
        union = set_a | set_b
        if not union:
            return 0.0
        return len(set_a & set_b) / len(union)

    def _is_diverse_enough(self, candidate_path: List[str], accepted: List[Dict]) -> bool:
        return all(
            self._jaccard_similarity(candidate_path, acc["path"]) < self.diversity_jaccard_threshold
            for acc in accepted
        )

    def find_k_paths(
        self,
        origin: str,
        destination: str,
        k: int = 3,
        start_time_hours: float = 0.0,
    ) -> List[Dict]:
        search_evaluator = self._search_cost_evaluator if self.enforce_diversity else self.cost_evaluator
        finder = CalmWaterAStar(self.graph, search_evaluator, self.v_max)
        initial_route = finder.solve(origin, destination, start_time_hours)
        if not initial_route:
            return []

        # Always report TRUE cost, even though the search above may have used a
        # penalized evaluator — see class docstring. On the very first route the
        # penalty set is empty anyway, so this is a no-op when enforce_diversity
        # is False or this is the first path found; recomputing unconditionally
        # keeps the logic uniform rather than special-casing "first path".
        true_cost, true_arrival = self._compute_path_cost_and_time(
            initial_route["path"], start_time_hours
        )
        initial_route = {
            "path": initial_route["path"],
            "total_cost": round(true_cost, 2),
            "arrival_time_hours": round(true_arrival, 2),
            "legs": initial_route["legs"],
        }
        if self.enforce_diversity:
            self._grow_penalty_buffer(initial_route["path"])

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

                    # spur_res["total_cost"] may reflect the penalized search
                    # evaluator (see class docstring) — recompute the spur
                    # segment's TRUE cost/time via the real cost_evaluator so
                    # what gets reported and ranked is never the inflated
                    # search-time number. Recomputed as root_cost (already
                    # true — computed above, before root_path's nodes were
                    # temporarily removed) + a fresh true recomputation of
                    # JUST the spur segment, not the whole candidate_path:
                    # root_path's nodes are still removed from self.graph at
                    # this point (restored further below), so a
                    # cost_evaluator that reads graph node/edge data would
                    # KeyError on them if asked to replay the full path here.
                    spur_cost, arrival_time = self._compute_path_cost_and_time(
                        spur_res["path"], root_time
                    )
                    total_cost = root_cost + spur_cost

                    candidate_dict = {
                        "path": candidate_path,
                        "total_cost": round(total_cost, 2),
                        "arrival_time_hours": round(arrival_time, 2),
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

            # Sort candidate pool by TRUE cost, cheapest first.
            B.sort(key=lambda x: x["total_cost"])

            if not self.enforce_diversity:
                A.append(B.pop(0))
                continue

            # Diversity-gated promotion: take the cheapest candidate that is
            # NOT too similar (by node-set Jaccard) to anything already
            # accepted. Candidates that fail this are dropped permanently
            # (not retried later) — they're too similar to something we
            # already have, so keeping them around wouldn't help a future
            # iteration either. If nothing in B clears the bar, stop early
            # and return fewer than k, same graceful degradation as running
            # out of candidates entirely.
            promoted = None
            remaining: List[Dict] = []
            for cand in B:
                if promoted is None and self._is_diverse_enough(cand["path"], A):
                    promoted = cand
                else:
                    remaining.append(cand)
            B = remaining

            if promoted is None:
                break

            A.append(promoted)
            self._grow_penalty_buffer(promoted["path"])

        return A
