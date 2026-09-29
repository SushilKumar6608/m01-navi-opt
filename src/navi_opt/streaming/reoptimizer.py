"""
Phase 6: re-solves the REMAINING legs of one in-progress voyage against
updated weather — the actual reoptimization, as opposed to
`voyage_registry.py` (what a voyage IS) or the consumer (when this gets
called). Kept separate from both on purpose: this is the one piece with
real mathematical content (another call into
`navi_opt.optimization.speed_profile.optimize_speed_profile()`), so it's
independently unit-testable against a real solve on a tiny fake graph —
exactly the pattern `tests/test_pareto.py` and
`tests/test_pareto_sweep_task.py` already use, not mocked away like the
Kafka-facing pieces have to be.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import networkx as nx

from navi_opt.optimization.speed_profile import (
    SpeedProfileResult,
    WeatherLookup,
    optimize_speed_profile,
)
from navi_opt.streaming.voyage_registry import Voyage, current_node_and_remaining_path, elapsed_hours
from navi_opt.weather.vessel_profiles import VesselProfile

#: Forwarded to `optimize_speed_profile()`'s own `maxiter`/`max_seconds` —
#: same bounded-solve reasoning as `pareto.py`'s `DEFAULT_SWEEP_MAXITER`/
#: `DEFAULT_SWEEP_MAX_SECONDS` (see those docstrings), reused here rather
#: than re-derived: a reoptimization triggered by a live weather event is
#: if anything MORE time-sensitive than a batch sweep point, not less, so
#: the same "always bounded, timeout still returns a real usable result"
#: guarantee applies at least as strongly here.
DEFAULT_REOPTIMIZE_MAXITER = 60
DEFAULT_REOPTIMIZE_MAX_SECONDS = 180.0


class VoyageAlreadyArrived(Exception):
    """Raised by `reoptimize_voyage()` when `voyage` has fewer than 2 nodes
    left in its remaining path at `now` — i.e., it has already completed,
    or is one node from completing, and there is nothing left to
    reoptimize. A caller (the consumer) should treat this as "stop
    tracking this voyage," not as a failure."""


def reoptimize_voyage(
    voyage: Voyage,
    digraph: nx.DiGraph,
    vessel: VesselProfile,
    weather_lookup: WeatherLookup,
    now: datetime,
    maxiter: Optional[int] = DEFAULT_REOPTIMIZE_MAXITER,
    max_seconds: Optional[float] = DEFAULT_REOPTIMIZE_MAX_SECONDS,
) -> SpeedProfileResult:
    """Re-solves `optimize_speed_profile()` for exactly the portion of
    `voyage` still ahead of it at `now`, against `weather_lookup` (the
    caller's job to have fetched fresh — see
    `reoptimization_consumer.py`'s docstring for why this function itself
    doesn't fetch weather), keeping the voyage's ORIGINAL absolute
    deadline (`voyage.deadline_hours`, measured from `voyage.
    voyage_start_time`) rather than resetting it — a re-solve that forgot
    how much of the original schedule was already used up wouldn't be a
    reoptimization of this voyage, it would be a fresh one that happens to
    share a start node.

    Warm-starts from the CURRENT committed profile's remaining leg speeds
    when the remaining path is unchanged in length (same reasoning as
    `sweep_epsilon_constraint()`'s warm-starting: the previous solution is
    typically close to the next one, cutting the solver's real dominant
    cost — see `pareto.py`'s `DEFAULT_SWEEP_MAXITER` docstring for the
    measurement that motivated always preferring a warm start when a valid
    one exists). Falls back to a cold start otherwise.

    Raises `VoyageAlreadyArrived` if fewer than 2 nodes remain — nothing
    to reoptimize. Raises `ValueError` (propagated straight from
    `optimize_speed_profile()`) if the remaining deadline is already
    infeasible even at the vessel's max speed — deliberately NOT caught
    here, for the same reason `pareto_sweep_task.py` lets its own
    exceptions propagate: a live reoptimization failing needs to surface
    as a real failure a person or an alerting system sees, not be
    swallowed.
    """
    node, remaining_path = current_node_and_remaining_path(voyage, now)
    if len(remaining_path) < 2:
        raise VoyageAlreadyArrived(
            f"Voyage {voyage.voyage_id!r} has {len(remaining_path)} node(s) left at {now!r} "
            f"— nothing to reoptimize."
        )

    elapsed = elapsed_hours(voyage, now)
    remaining_deadline_hours = voyage.deadline_hours - elapsed
    # A negative remaining deadline means the voyage is already running
    # late against its original schedule — a real, meaningful situation
    # this function does NOT silently paper over by clamping to 0 (which
    # would misrepresent "immediate arrival required" as a legitimate
    # constraint) or by raising (a late voyage still needs a best-effort
    # speed profile, arguably more urgently than an on-time one). Instead:
    # drop the deadline constraint entirely for this solve, so the
    # optimizer returns its unconstrained-fuel-minimizing profile — the
    # caller is expected to treat `remaining_deadline_hours <= 0` in the
    # returned context as "already late" and handle that as its own
    # signal (an alert, a flagged event), not something this function
    # decides how to react to.
    max_transit_hours = remaining_deadline_hours if remaining_deadline_hours > 0 else None

    # `remaining_path` is always a suffix of `committed_profile.path` by
    # construction (current_node_and_remaining_path() derives it that
    # way), so its legs align 1:1, in order, with the tail of
    # `committed_profile.legs` — the warm start is always exactly the
    # right length, never a partial/mismatched guess.
    committed_legs = voyage.committed_profile.legs
    already_used_legs = len(committed_legs) - (len(remaining_path) - 1)
    warm_start = [leg.speed_knots for leg in committed_legs[already_used_legs:]]

    return optimize_speed_profile(
        remaining_path,
        digraph,
        vessel,
        voyage.cargo_condition,
        weather_lookup=weather_lookup,
        start_time_hours=elapsed,
        max_transit_hours=max_transit_hours,
        initial_speeds_knots=warm_start,
        maxiter=maxiter,
        max_seconds=max_seconds,
    )
