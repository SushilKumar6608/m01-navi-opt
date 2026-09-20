"""
Phase 4: multi-objective trade-off analysis (fuel vs. duration vs. weather
risk) via the epsilon-constraint method, built on top of Phase 2's speed
optimizer and Phase 2's corridor diversity feature rather than as a new
solver.

Why epsilon-constraint, and why it needed almost no new solver code:
`optimize_speed_profile()` already minimizes fuel subject to
`max_transit_hours` — that IS the epsilon-constraint method applied to the
duration objective (minimize one objective, cap the other at a swept
threshold epsilon). Sweeping `max_transit_hours` over a range and
collecting (fuel, duration) pairs traces out the fuel-vs-duration frontier
for free; nothing here re-implements optimization that Phase 2 already
does. This module adds: (1) a weather-risk objective computed from the
optimizer's own per-leg results (no new sampling), (2) the sweep loop
across epsilon values and across the k diverse corridors from Phase 2's
Yen's-with-diversity, and (3) non-dominated filtering across all three
objectives at once (not just the two the solver directly trades off),
since a corridor/epsilon combination optimal on fuel-vs-duration can still
be weather-riskier than another combination at a similar fuel/duration
cost.

Weather-risk objective definition (deliberate choice, documented rather
than assumed): significant-wave-height exposure, metre-hours
(`sum(leg.wave_height_m * leg.transit_hours)`). This is a time-weighted
integral of rough-water exposure along the route, not a per-leg maximum —
chosen because it's directly derivable from `SpeedProfileResult.legs`
(the optimizer already samples and stores `wave_height_m` per leg for
every solve, so this needs no new weather sampling), and because a route
that spends more TIME in rough water is a more actionable "risk" signal
for voyage planning than a route that merely touches a high wave height
briefly. A per-leg-maximum ("worst single leg") metric is a legitimate
alternative for a different question ("what's the single worst moment"),
not implemented here — documented so this choice doesn't get
rediscovered as an oversight later.
"""
from __future__ import annotations

import time
from typing import Callable, List, NamedTuple, Optional

import networkx as nx

from navi_opt.optimization.speed_profile import (
    SpeedProfileResult,
    WeatherLookup,
    optimize_speed_profile,
)
from navi_opt.weather.vessel_profiles import CargoCondition, VesselProfile


def weather_risk_m_hours(result: SpeedProfileResult) -> float:
    """Significant-wave-height exposure integral (metre-hours) for one
    already-solved speed profile. See module docstring for why this
    metric, not a per-leg maximum."""
    return sum(leg.wave_height_m * leg.transit_hours for leg in result.legs)


class ParetoPoint(NamedTuple):
    corridor_index: int
    epsilon_hours: float  # the max_transit_hours cap used to produce this point
    fuel_tonnes: float
    transit_hours: float
    weather_risk_m_hours: float
    success: bool
    result: SpeedProfileResult


#: Default SLSQP iteration cap passed to `optimize_speed_profile()` by
#: `sweep_epsilon_constraint()` below. Kept as a secondary safeguard
#: alongside `DEFAULT_SWEEP_MAX_SECONDS` (the real bound — see its
#: docstring): a local synthetic benchmark projected ~2.4s/solve at this
#: cap, but a REAL run on the actual Rotterdam<->Ceyhan corridor measured
#: two epsilons at an identical maxiter=60 differing ~9x in wall-clock
#: time (217.7s vs. 1941.1s, the second not even converging) — proof that
#: iteration count alone does not bound wall-clock time on real weather
#: data, only `max_seconds` does. See findings.md's Phase 4 entry for the
#: full writeup, including why this was trusted before being measured and
#: turned out wrong.
DEFAULT_SWEEP_MAXITER = 60

#: Default hard wall-clock budget (seconds) per solve in a sweep — the
#: safeguard that actually bounds a sweep's worst case, added after
#: `DEFAULT_SWEEP_MAXITER` alone failed to on a real run (see its
#: docstring). 180s gives real solves room to converge (the real run's
#: first, converged epsilon took 217.7s — close to but over this, so this
#: value trades some genuine convergence for a bounded sweep; a caller that
#: wants a real solve to run longer should pass a larger `max_seconds`
#: explicitly) while capping the worst case at n_points x k_corridors x
#: this value, a number a caller can actually plan around before starting
#: a sweep — unlike an iteration cap, which measurably couldn't be
#: reasoned about the same way on real data.
DEFAULT_SWEEP_MAX_SECONDS = 180.0

ProgressCallback = Callable[[int, float, Optional["ParetoPoint"], float], None]


def sweep_epsilon_constraint(
    corridor_index: int,
    path: List[str],
    graph: nx.DiGraph,
    vessel: VesselProfile,
    cargo_condition: CargoCondition,
    weather_lookup: WeatherLookup,
    epsilons_hours: List[float],
    start_time_hours: float = 0.0,
    maxiter: Optional[int] = DEFAULT_SWEEP_MAXITER,
    max_seconds: Optional[float] = DEFAULT_SWEEP_MAX_SECONDS,
    on_progress: Optional[ProgressCallback] = None,
) -> List[ParetoPoint]:
    """Runs `optimize_speed_profile()` once per epsilon in `epsilons_hours`
    for ONE fixed corridor, each with `max_transit_hours=epsilon`. Skips
    (does not raise for) any epsilon `optimize_speed_profile()` itself
    reports infeasible (tighter than every leg at the vessel's max speed
    in calm water) — a sweep is expected to include some epsilons below
    the feasible floor when the floor isn't known precisely in advance;
    that's normal sweep behavior, not an error to propagate.

    `maxiter` and `max_seconds` are both forwarded to every solve in the
    sweep (see `DEFAULT_SWEEP_MAXITER`'s and `DEFAULT_SWEEP_MAX_SECONDS`'s
    docstrings for why a sweep needs both bounded, and why iteration count
    alone measurably wasn't enough on real weather data). Pass `None` for
    either to use scipy's own default / leave that solve unbounded.

    `on_progress`, if given, is called after EVERY epsilon attempt
    (feasible or not) as `(index_in_sweep, epsilon_hours, point_or_None,
    elapsed_seconds)` — added specifically so a caller running this against
    a real, possibly slow corridor can report per-epsilon progress rather
    than staying silent for the whole sweep's duration (a real gap caught
    after a live run looked indistinguishable from a hang — see
    findings.md). Optional and side-effect-only: omitting it changes
    nothing about this function's return value.

    Warm-starting: each epsilon after the first is solved starting from the
    PREVIOUS feasible epsilon's converged speeds (`initial_speeds_knots`),
    not a cold `design_speed_knots` guess every time. Consecutive epsilons
    in a sweep are close together (`default_epsilon_sweep`'s spacing is
    linear), so the previous solution is typically a good starting point
    for the next, which matters because a cold start's iteration count is
    where a real corridor's cost concentrates — see findings.md's Phase 4
    entry for the direct measurement behind this (a real ~390-leg corridor
    took 217.7s for one cold-start solve; the per-evaluation cost, not the
    iteration cap, is what dominates that, so warm-starting attacks the
    actual driver rather than just bounding the damage the way `maxiter`
    alone does). Falls back to a cold start again after any epsilon that
    was skipped as infeasible or that didn't converge, rather than warm-
    starting from a result that shouldn't be trusted as "close."

    Returns one ParetoPoint per feasible epsilon, in the same order as
    `epsilons_hours` (with infeasible ones simply absent) — not
    pre-filtered for dominance; call `pareto_frontier()` on the
    concatenation across corridors for that.
    """
    points: List[ParetoPoint] = []
    warm_start: Optional[List[float]] = None
    for i, eps in enumerate(epsilons_hours):
        t0 = time.monotonic()
        point: Optional[ParetoPoint] = None
        try:
            result = optimize_speed_profile(
                path,
                graph,
                vessel,
                cargo_condition,
                weather_lookup=weather_lookup,
                start_time_hours=start_time_hours,
                max_transit_hours=eps,
                maxiter=maxiter,
                initial_speeds_knots=warm_start,
                max_seconds=max_seconds,
            )
        except ValueError:
            warm_start = None  # infeasible epsilon for this corridor — expected, not an error
        else:
            point = ParetoPoint(
                corridor_index=corridor_index,
                epsilon_hours=eps,
                fuel_tonnes=result.total_fuel_tonnes,
                transit_hours=result.total_transit_hours,
                weather_risk_m_hours=weather_risk_m_hours(result),
                success=result.success,
                result=result,
            )
            points.append(point)
            # Only warm-start the NEXT epsilon from a solve that actually
            # converged — an unconverged result isn't "close" in any sense
            # worth trusting as a starting guess, so fall back to cold.
            warm_start = [leg.speed_knots for leg in result.legs] if result.success else None
        if on_progress is not None:
            on_progress(i, eps, point, time.monotonic() - t0)
    return points


# Dominance tolerance per objective. Needed because two independent SLSQP
# solves that both land on the same flat-out speed floor (e.g. two
# corridors' cheapest epsilon in a sweep) converge to transit times that
# are equal in principle but differ by solver noise on the order of 1e-9
# to 1e-6 — an exact `<=` comparison treats that noise as a genuine
# trade-off and lets a point that is actually strictly worse (same
# schedule, more fuel, more risk) survive on the frontier as if it
# represented a real alternative. Caught by a direct check before this
# module was ever delivered: two solves at the same epsilon differed by
# ~7.5e-11 hours, which flipped `<=` for no physical reason. These
# tolerances are set well below any difference this project's fuel model
# would ever call meaningful (single-digit tonnes, hours, metre-hours are
# the scales that actually matter here — see the Pareto findings.md
# entry), and well above the observed solver noise, so real trade-offs
# are never hidden by this.
FUEL_TOL_TONNES = 1e-3
DURATION_TOL_HOURS = 1e-3
RISK_TOL_M_HOURS = 1e-3


def _dominates(a: ParetoPoint, b: ParetoPoint) -> bool:
    """True if `a` dominates `b`: at least as good on every objective
    (fuel, duration, weather risk — all minimized), within
    `*_TOL_*` of numerical solver noise, and strictly better on at least
    one beyond that tolerance. Ties (within tolerance) on all three are
    NOT dominance either way (both stay, since neither is actually
    better)."""
    at_least_as_good = (
        a.fuel_tonnes <= b.fuel_tonnes + FUEL_TOL_TONNES
        and a.transit_hours <= b.transit_hours + DURATION_TOL_HOURS
        and a.weather_risk_m_hours <= b.weather_risk_m_hours + RISK_TOL_M_HOURS
    )
    strictly_better = (
        a.fuel_tonnes < b.fuel_tonnes - FUEL_TOL_TONNES
        or a.transit_hours < b.transit_hours - DURATION_TOL_HOURS
        or a.weather_risk_m_hours < b.weather_risk_m_hours - RISK_TOL_M_HOURS
    )
    return at_least_as_good and strictly_better


def pareto_frontier(points: List[ParetoPoint]) -> List[ParetoPoint]:
    """Filters `points` (pooled across corridors and epsilons) down to the
    non-dominated set across all three objectives at once. O(n^2) pairwise
    comparison — deliberately simple over a more elaborate non-dominated-
    sort algorithm (e.g. Kung's), since this project's sweep sizes (tens
    of epsilons x k=3 corridors, so low hundreds of points at most) don't
    come close to where the asymptotic difference would matter; same
    "don't add complexity a problem this size doesn't need" standard
    already applied to the STRtree/joblib question in Phase 2."""
    frontier = []
    for candidate in points:
        if not any(_dominates(other, candidate) for other in points if other is not candidate):
            frontier.append(candidate)
    return frontier


def default_epsilon_sweep(
    path: List[str],
    graph: nx.DiGraph,
    v_max_knots: float,
    n_points: int = 12,
    max_margin: float = 1.6,
) -> List[float]:
    """Convenience helper: builds an epsilon sweep from each corridor's own
    fastest-possible transit floor (`total_dist_nm / v_max_knots`) up to
    `max_margin` x that floor, evenly spaced. Not the only valid way to
    pick a sweep (a caller with a specific laycan window in mind should
    build epsilons directly around it instead) — this is a reasonable
    default for exploring a corridor's full fuel-duration trade space when
    no specific deadline is given."""
    edges = list(zip(path, path[1:]))
    total_dist_nm = sum(graph[u][v]["dist_nm"] for u, v in edges)
    floor_hours = total_dist_nm / v_max_knots
    if n_points < 2:
        raise ValueError(f"n_points must be >= 2, got {n_points}")
    step = (max_margin - 1.0) / (n_points - 1)
    return [floor_hours * (1.0 + i * step) for i in range(n_points)]
