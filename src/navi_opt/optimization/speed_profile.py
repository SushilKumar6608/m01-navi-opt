"""
Phase 2 speed-profile optimizer.

Given ONE fixed corridor (a `path` as returned by `YenKShortestPaths` /
`CalmWaterAStar`), choose a per-leg vessel speed that minimizes total fuel
burn under weather, subject to the vessel's speed bounds and an optional
total-transit-time cap. This is deliberately NOT a new graph search:
Phase 1 (Yen's over `CalmWaterAStar`, `navi_opt.routing`) already produced
a small, fixed set of geometrically diverse corridors using a static
calm-water cost — see findings.md's "Phase 2 — Routing" entry for why
that split exists. This module evaluates and optimizes ONE such
corridor's speed profile against the real, dynamic fuel model
(`resistance_model.fuel_tonnes_for_leg`), which is the other half of that
same design decision.

Why an NLP, not a MILP: `fuel_rate_tonnes_per_day` is cubic and strictly
increasing in speed for any speed > 0 (all fuel_curve coefficients are
positive — see vessel_profiles.yaml), and legs are coupled only through
cumulative arrival time (which determines which weather snapshot applies
to the next leg). That's a smooth, sequential nonlinear program with
simple bound + one linear-ish inequality constraint — solving it well
doesn't need speed or time discretized into bins, which a MILP
formulation would require and which would throw away resolution the
underlying model doesn't need. Solved with `scipy.optimize.minimize`
(SLSQP), using numerical (finite-difference) gradients — fine at this
problem's scale (tens of legs per corridor, not thousands).

Weather is injected via a `weather_lookup` callable
(`(u, v, arrival_time_hours) -> (wave_height_m, headwind_knots)`),
mirroring the `(u, v, current_t)` convention already used by
`cost_evaluator` in `navi_opt.routing`. This module has no CMEMS
dependency of its own — Phase 1's `fetch_wave_field()`/`fetch_wind_field()`
pull is still pending (see findings.md), so real weather gets wired in by
providing a `weather_lookup` backed by that data once it exists.
`calm_weather_lookup` below is a placeholder that returns exactly zero
for every leg — a stand-in for "no weather field wired in yet", not a
synthetic forecast, and it must never be mistaken for one.
"""
from __future__ import annotations

from typing import Callable, List, NamedTuple, Optional, Tuple

import networkx as nx
from scipy.optimize import minimize

from navi_opt.weather.resistance_model import fuel_tonnes_for_leg
from navi_opt.weather.vessel_profiles import (
    CargoCondition,
    VesselProfile,
    load_hourly_conversion_factor,
)

WeatherLookup = Callable[[str, str, float], Tuple[float, float]]


def calm_weather_lookup(u: str, v: str, arrival_time_hours: float) -> Tuple[float, float]:
    """Placeholder weather_lookup: always (wave_height_m=0.0, headwind_knots=0.0).

    Use this only where no real weather field is wired in yet (e.g. before
    Phase 1's CMEMS fetch is implemented, or in tests that care about the
    speed/time optimization itself rather than weather sensitivity). It
    represents "no weather data available", not "calm conditions
    forecast" — callers surfacing results to a person should say so.
    """
    return 0.0, 0.0


class LegResult(NamedTuple):
    u: str
    v: str
    speed_knots: float
    departure_time_hours: float
    wave_height_m: float
    headwind_knots: float
    transit_hours: float
    fuel_tonnes: float


class SpeedProfileResult(NamedTuple):
    path: List[str]
    legs: List[LegResult]
    total_fuel_tonnes: float
    total_transit_hours: float
    arrival_time_hours: float
    success: bool
    message: str


def _leg_edges(path: List[str]) -> List[Tuple[str, str]]:
    return list(zip(path, path[1:]))


def _leg_distance_nm(graph: nx.DiGraph, u: str, v: str) -> float:
    if not graph.has_edge(u, v):
        raise ValueError(f"No edge ({u!r} -> {v!r}) in graph — path is not a valid walk")
    try:
        return graph[u][v]["dist_nm"]
    except KeyError as exc:
        raise KeyError(
            f"Edge ({u!r} -> {v!r}) has no 'dist_nm' attribute — expected an edge from "
            f"navi_opt.grid.graph_builder.build_ocean_graph(), which always sets it"
        ) from exc


def _min_possible_transit_hours(
    graph: nx.DiGraph, edges: List[Tuple[str, str]], v_max_knots: float
) -> float:
    """Lower bound on total transit time: every leg run at the vessel's max
    speed, ignoring weather entirely (weather can only slow a vessel down
    relative to calm water in this model — see resistance_model.py's
    tailwind-clipped-to-zero note — so this is a true lower bound, not an
    approximation)."""
    return sum(_leg_distance_nm(graph, u, v) / v_max_knots for u, v in edges)


def optimize_speed_profile(
    path: List[str],
    graph: nx.DiGraph,
    vessel: VesselProfile,
    cargo_condition: CargoCondition,
    weather_lookup: WeatherLookup = calm_weather_lookup,
    start_time_hours: float = 0.0,
    max_transit_hours: Optional[float] = None,
    hourly_conversion_factor: Optional[float] = None,
) -> SpeedProfileResult:
    """Choose a per-leg speed along `path` minimizing total fuel burn.

    Parameters
    ----------
    path : the node sequence of ONE fixed corridor (e.g. one entry from
        YenKShortestPaths.find_k_paths()'s "path" field). Must have at
        least 2 nodes and every consecutive pair must be a real graph edge
        carrying a 'dist_nm' attribute (as build_ocean_graph() produces).
    max_transit_hours : optional total-duration cap (hours), e.g. from a
        laycan window. Raises ValueError up front if the vessel cannot
        possibly make it even at max speed in calm water — rather than
        handing an infeasible problem to the optimizer and returning a
        constraint-violating "solution" silently.

    Returns
    -------
    SpeedProfileResult. `success=False` means the underlying SLSQP solve
    did not report convergence — the returned profile may still be a
    reasonable (if unverified) answer; check `message` and treat it with
    appropriate skepticism rather than as a *raised* failure, since SLSQP
    can report non-convergence for reasons (e.g. hitting max iterations
    near an already-good point) that don't necessarily invalidate the
    result.
    """
    if len(path) < 2:
        raise ValueError(f"path must have at least 2 nodes (1 leg), got {path}")

    edges = _leg_edges(path)
    distances_nm = [_leg_distance_nm(graph, u, v) for u, v in edges]

    lo, hi = vessel.speed_bounds_knots
    if hourly_conversion_factor is None:
        hourly_conversion_factor = load_hourly_conversion_factor()

    if max_transit_hours is not None:
        min_possible = _min_possible_transit_hours(graph, edges, v_max_knots=hi)
        if min_possible > max_transit_hours:
            raise ValueError(
                f"max_transit_hours={max_transit_hours} is infeasible: even running every "
                f"leg at the vessel's max speed ({hi} kn) in calm water takes "
                f"{min_possible:.2f} hours"
            )

    def _evaluate(speeds: List[float]) -> Tuple[float, List[LegResult]]:
        """Walks the corridor once at the given per-leg speeds, sampling
        weather at each leg's departure time (a simplification — the
        alternative, sampling at the leg's midpoint or re-solving on
        arrival, adds complexity this prototype doesn't need given legs
        are short relative to how fast weather fields change; documented
        here rather than silently assumed)."""
        t = start_time_hours
        total_fuel = 0.0
        legs: List[LegResult] = []
        for (u, v), dist_nm in zip(edges, distances_nm):
            wave_h, headwind = weather_lookup(u, v, t)
            fuel, transit_hours = fuel_tonnes_for_leg(
                dist_nm,
                speeds[len(legs)],
                wave_h,
                headwind,
                vessel,
                cargo_condition,
                hourly_conversion_factor=hourly_conversion_factor,
            )
            legs.append(
                LegResult(
                    u=u,
                    v=v,
                    speed_knots=speeds[len(legs)],
                    departure_time_hours=t,
                    wave_height_m=wave_h,
                    headwind_knots=headwind,
                    transit_hours=transit_hours,
                    fuel_tonnes=fuel,
                )
            )
            total_fuel += fuel
            t += transit_hours
        return total_fuel, legs

    def _objective(speeds) -> float:
        total_fuel, _ = _evaluate(list(speeds))
        return total_fuel

    def _total_transit_hours(speeds) -> float:
        return sum(dist / speed for dist, speed in zip(distances_nm, speeds))

    n_legs = len(edges)
    x0 = [min(max(vessel.design_speed_knots, lo), hi)] * n_legs
    bounds = [(lo, hi)] * n_legs

    constraints = []
    if max_transit_hours is not None:
        constraints.append(
            {
                "type": "ineq",
                "fun": lambda speeds: max_transit_hours - _total_transit_hours(speeds),
            }
        )

    result = minimize(
        _objective,
        x0=x0,
        method="SLSQP",
        bounds=bounds,
        constraints=constraints,
    )

    total_fuel, legs = _evaluate(list(result.x))
    arrival_time_hours = legs[-1].departure_time_hours + legs[-1].transit_hours
    total_transit_hours = arrival_time_hours - start_time_hours

    return SpeedProfileResult(
        path=path,
        legs=legs,
        total_fuel_tonnes=total_fuel,
        total_transit_hours=total_transit_hours,
        arrival_time_hours=arrival_time_hours,
        success=bool(result.success),
        message=str(result.message),
    )
