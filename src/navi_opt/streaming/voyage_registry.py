"""
Phase 6: in-memory voyage state — the piece that didn't exist anywhere in
this project before, and that "re-optimize the REMAINING legs of an
in-progress voyage" cannot mean anything without.

In-memory in the consumer process, deliberately (see Phase 6's findings.md
entry for the full reasoning): the simplest real implementation, lost on
restart, single-process only — an honest, working v1 rather than premature
durability/schema design (a Postgres-backed registry is a real option
later, once the core reoptimization logic is proven, not before).

The one real design problem this module solves: a `Voyage`'s weather
sampling needs an ABSOLUTE calendar time (`voyage_start_time` — the same
reference every `optimize_speed_profile()` call in this project has always
used, since CMEMS data is a fixed historical/forecast window, not a live
stream), but "how far along the voyage is right now" needs some notion of
elapsed time to progress at all in a session that runs for minutes, not
the ~230-370 REAL hours findings.md's Phase 4 entry measured for the
actual Rotterdam<->Ceyhan corridor. Conflating those two clocks would be
wrong (voyage_start_time is fixed at whatever historical CMEMS window was
fetched, e.g. 2024-06-01 — real "now" in 2026 is nowhere near it). So this
module tracks a THIRD, explicit clock: `demo_departed_at`, the REAL
wall-clock time a voyage was registered in a running demo, scaled by
`DEMO_TIME_SCALE` into voyage-timeline hours. This is openly a demo
acceleration mechanic, not a claim about real voyage tracking — a real
deployment would replace `elapsed_hours()`'s wall-clock-based computation
with real AIS/position-report data and drop the demo clock entirely; that
replacement is the only thing `DEMO_TIME_SCALE` exists to make legible as
a placeholder for, not a design flaw to quietly ship as if it were real
tracking.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

from navi_opt.optimization.speed_profile import SpeedProfileResult
from navi_opt.weather.vessel_profiles import CargoCondition

#: How many voyage-timeline hours pass per REAL second of wall-clock time
#: in a running demo. 600.0 means one real minute = 10 simulated voyage
#: hours, so the ~230-370h real corridor (findings.md's Phase 4 numbers)
#: completes in roughly 25-40 real minutes — long enough to plausibly
#: watch a reoptimization actually happen, short enough to fit in one
#: sitting. Not derived from anything physical; a documented default, same
#: as `DEFAULT_SEVERE_WAVE_THRESHOLD_M` elsewhere in this project, and a
#: caller who wants a different pace overrides it per-`VoyageRegistry` or
#: per-call, not by editing this constant.
DEMO_TIME_SCALE = 600.0


@dataclass
class Voyage:
    """One in-progress voyage's full state. Mutable (via
    `VoyageRegistry.commit()`), because a voyage's `committed_profile` is
    exactly the thing Phase 6's reoptimization loop replaces over time —
    an immutable/frozen dataclass would just mean rebuilding a new Voyage
    object on every commit for no benefit, since the registry already owns
    the only reference to it."""

    voyage_id: str
    vessel_key: str
    cargo_condition: CargoCondition
    voyage_start_time: datetime  # absolute calendar time, fixed — the weather_lookup's own reference time
    deadline_hours: float        # must arrive by voyage_start_time + deadline_hours, voyage-timeline hours
    demo_departed_at: datetime   # REAL wall-clock time this voyage was registered — see module docstring
    committed_profile: SpeedProfileResult  # profile for path[current_node:] as of the last commit
    reoptimization_count: int = 0


def elapsed_hours(voyage: Voyage, now: datetime) -> float:
    """Voyage-timeline hours since `voyage.demo_departed_at`, scaled by
    `DEMO_TIME_SCALE`. `now` is a parameter, not `datetime.now()` read
    internally, so every caller — including every test in
    `tests/test_voyage_registry.py` — controls it explicitly rather than
    this module's behavior depending on when a test happens to run."""
    real_seconds = (now - voyage.demo_departed_at).total_seconds()
    return max(0.0, real_seconds) / 3600.0 * DEMO_TIME_SCALE


def current_node_and_remaining_path(voyage: Voyage, now: datetime) -> Tuple[str, List[str]]:
    """Where `voyage` currently is (by node, not sub-leg position — this
    project's whole routing model is node-to-node legs, so pretending
    sub-leg precision exists here would be a false precision this
    project's H3-cell-grained graph doesn't actually have) and the path
    remaining from there to the destination, both computed from
    `elapsed_hours(voyage, now)` against `voyage.committed_profile.legs`'
    own (departure_time_hours, transit_hours) — which are absolute,
    voyage-timeline hours, unchanged by how many times this voyage has
    been reoptimized (see module docstring).

    A voyage is considered to have arrived at a node the moment that
    node's inbound leg's `departure_time_hours + transit_hours` is at or
    before the current elapsed hours — i.e., "at" a node between legs, not
    mid-leg. If elapsed hours have passed the whole committed profile's
    arrival, the voyage has completed: returns the destination node with a
    single-element remaining path (nothing left to reoptimize).
    """
    elapsed = elapsed_hours(voyage, now)
    legs = voyage.committed_profile.legs
    path = voyage.committed_profile.path

    node_index = 0
    for i, leg in enumerate(legs):
        arrival = leg.departure_time_hours + leg.transit_hours
        if elapsed < arrival:
            break
        node_index = i + 1
    else:
        node_index = len(legs)  # completed every leg

    return path[node_index], path[node_index:]


class VoyageRegistry:
    """The in-memory store itself — a dict keyed by voyage_id, plus the
    lookups Phase 6's consumer actually needs: which voyages still have a
    given edge ahead of them (to react to a weather event), and committing
    a freshly reoptimized profile."""

    def __init__(self) -> None:
        self._voyages: Dict[str, Voyage] = {}

    def register(self, voyage: Voyage) -> None:
        self._voyages[voyage.voyage_id] = voyage

    def get(self, voyage_id: str) -> Voyage:
        return self._voyages[voyage_id]

    def remove(self, voyage_id: str) -> None:
        del self._voyages[voyage_id]

    def all_voyage_ids(self) -> List[str]:
        return list(self._voyages.keys())

    def commit(self, voyage_id: str, new_profile: SpeedProfileResult) -> None:
        """Replaces `voyage_id`'s `committed_profile` with `new_profile`
        (the result of a reoptimization) and bumps its counter. Does NOT
        touch `demo_departed_at` — the demo clock keeps running against
        the ORIGINAL departure regardless of how many times a voyage has
        been reoptimized, which is exactly what makes
        `current_node_and_remaining_path()` correct across repeated
        reoptimizations without this registry needing to track separately
        "how much simulated time had elapsed as of the last commit" (see
        module docstring for why that's derivable instead of stored)."""
        voyage = self._voyages[voyage_id]
        voyage.committed_profile = new_profile
        voyage.reoptimization_count += 1

    def current_node_and_remaining_path(self, voyage_id: str, now: datetime) -> Tuple[str, List[str]]:
        return current_node_and_remaining_path(self._voyages[voyage_id], now)

    def elapsed_hours(self, voyage_id: str, now: datetime) -> float:
        return elapsed_hours(self._voyages[voyage_id], now)

    def affected_voyage_ids(self, u: str, v: str, now: datetime) -> List[str]:
        """Which registered voyages still have edge (u, v) somewhere ahead
        of them — the lookup Phase 6's consumer runs on every weather
        event to decide who to reoptimize. O(voyages x remaining-path
        length); fine at this project's demo scale (a handful of voyages
        at once), same "simple over asymptotically clever" standard
        `pareto_frontier()`'s own docstring already applies to a smaller
        problem."""
        affected = []
        for voyage_id, voyage in self._voyages.items():
            _, remaining_path = current_node_and_remaining_path(voyage, now)
            remaining_edges = zip(remaining_path, remaining_path[1:])
            if (u, v) in remaining_edges:
                affected.append(voyage_id)
        return affected
