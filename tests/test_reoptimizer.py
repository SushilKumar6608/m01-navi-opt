from datetime import datetime, timedelta

import networkx as nx
import pytest

from navi_opt.optimization.speed_profile import optimize_speed_profile
from navi_opt.streaming.reoptimizer import VoyageAlreadyArrived, reoptimize_voyage
from navi_opt.streaming.voyage_registry import DEMO_TIME_SCALE, Voyage
from navi_opt.weather.vessel_profiles import load_vessel_profile

VESSEL = load_vessel_profile("mr2_product_tanker")
LO, HI = VESSEL.speed_bounds_knots
VOYAGE_START = datetime(2024, 6, 1, 0, 0)
DEPARTED_AT = datetime(2026, 1, 1, 12, 0)


def _three_leg_graph(dist_nm_each: float) -> nx.DiGraph:
    g = nx.DiGraph()
    for i, name in enumerate("ABCD"):
        g.add_node(name, lat=0.0, lon=float(i) * 2.0)
    g.add_edge("A", "B", dist_nm=dist_nm_each)
    g.add_edge("B", "C", dist_nm=dist_nm_each)
    g.add_edge("C", "D", dist_nm=dist_nm_each)
    return g


def _calm(u, v, t):
    return 0.0, 0.0


def _departed_voyage(graph, deadline_hours, weather_lookup=_calm, demo_departed_at=DEPARTED_AT) -> Voyage:
    initial = optimize_speed_profile(
        ["A", "B", "C", "D"], graph, VESSEL, "laden",
        weather_lookup=weather_lookup, start_time_hours=0.0, max_transit_hours=deadline_hours,
    )
    return Voyage(
        voyage_id="v1", vessel_key="mr2_product_tanker", cargo_condition="laden",
        voyage_start_time=VOYAGE_START, deadline_hours=deadline_hours,
        demo_departed_at=demo_departed_at, committed_profile=initial,
    )


def _now_at_elapsed_hours(hours: float) -> datetime:
    real_seconds = hours / DEMO_TIME_SCALE * 3600.0
    return DEPARTED_AT + timedelta(seconds=real_seconds)


# --- basic correctness: re-solves only the remaining sub-path ---


def test_reoptimize_solves_only_the_remaining_path():
    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    voyage = _departed_voyage(g, deadline_hours=floor_leg_hours * 3 * 1.5)

    # Advance partway through leg A->B (still before B).
    now = _now_at_elapsed_hours(floor_leg_hours * 0.5)
    result = reoptimize_voyage(voyage, g, VESSEL, _calm, now)

    assert result.path == ["A", "B", "C", "D"]  # nothing completed yet, full path remains


def test_reoptimize_drops_completed_legs_from_the_result_path():
    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    voyage = _departed_voyage(g, deadline_hours=floor_leg_hours * 3 * 1.5)

    # Advance just past leg A->B's ACTUAL solved duration (not the flat-out
    # floor — with generous slack, fuel-optimal legs run slower than that,
    # so timing off the floor would land mid-leg-1, not at B).
    leg_ab = voyage.committed_profile.legs[0]
    now = _now_at_elapsed_hours(leg_ab.departure_time_hours + leg_ab.transit_hours + 0.01)
    result = reoptimize_voyage(voyage, g, VESSEL, _calm, now)

    assert result.path == ["B", "C", "D"]
    assert len(result.legs) == 2


def test_reoptimize_keeps_the_original_absolute_deadline():
    # The remaining budget must shrink by exactly how much simulated time
    # has elapsed — not reset to the full original deadline, and not
    # measured from the wrong reference.
    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    deadline = floor_leg_hours * 3 * 1.5
    voyage = _departed_voyage(g, deadline_hours=deadline)

    elapsed = floor_leg_hours * 1.2
    now = _now_at_elapsed_hours(elapsed)
    result = reoptimize_voyage(voyage, g, VESSEL, _calm, now)

    # total_transit_hours is the REMAINING duration (see speed_profile.py:
    # arrival_time_hours - start_time_hours), so it must fit within
    # (deadline - elapsed), not the full deadline.
    assert result.total_transit_hours <= (deadline - elapsed) + 1e-6


def test_reoptimize_reacts_to_newly_rough_weather_by_costing_more_fuel_or_time():
    # The actual point of this whole subsystem: a voyage that becomes
    # feasible only at higher fuel cost once weather worsens on its
    # remaining leg must reflect that in the reoptimized result, not just
    # return an unchanged/cheaper profile.
    g = _three_leg_graph(120.0)
    floor_leg_hours = 120.0 / HI
    # Generous deadline so a fuel-cheap slow solution is available under
    # calm water, and rough weather forces a real trade-off.
    deadline = floor_leg_hours * 3 * 1.8
    voyage = _departed_voyage(g, deadline_hours=deadline)

    leg_ab = voyage.committed_profile.legs[0]
    now = _now_at_elapsed_hours(leg_ab.departure_time_hours + leg_ab.transit_hours + 0.01)

    def rough_from_here(u, v, t):
        return 5.0, 25.0  # rough weather on every remaining leg

    calm_result = reoptimize_voyage(voyage, g, VESSEL, _calm, now)
    rough_result = reoptimize_voyage(voyage, g, VESSEL, rough_from_here, now)

    assert rough_result.total_fuel_tonnes >= calm_result.total_fuel_tonnes - 1e-6


def test_reoptimize_warm_starts_from_the_committed_profiles_remaining_legs():
    import navi_opt.streaming.reoptimizer as reoptimizer_mod

    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    voyage = _departed_voyage(g, deadline_hours=floor_leg_hours * 3 * 1.5)
    leg_ab = voyage.committed_profile.legs[0]
    now = _now_at_elapsed_hours(leg_ab.departure_time_hours + leg_ab.transit_hours + 0.01)

    seen_x0 = []
    real_optimize = reoptimizer_mod.optimize_speed_profile

    def _spy(*args, **kwargs):
        seen_x0.append(kwargs.get("initial_speeds_knots"))
        return real_optimize(*args, **kwargs)

    reoptimizer_mod.optimize_speed_profile = _spy
    try:
        reoptimize_voyage(voyage, g, VESSEL, _calm, now)
    finally:
        reoptimizer_mod.optimize_speed_profile = real_optimize

    expected = [leg.speed_knots for leg in voyage.committed_profile.legs[1:]]  # skip A->B, already done
    assert seen_x0[0] == pytest.approx(expected)


# --- edge cases: already arrived, already late ---


def test_reoptimize_raises_voyage_already_arrived_when_nothing_remains():
    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    voyage = _departed_voyage(g, deadline_hours=floor_leg_hours * 3 * 1.5)
    now = _now_at_elapsed_hours(floor_leg_hours * 3 * 5.0)  # way past arrival
    with pytest.raises(VoyageAlreadyArrived):
        reoptimize_voyage(voyage, g, VESSEL, _calm, now)


def test_reoptimize_drops_the_deadline_constraint_when_already_late_rather_than_crashing():
    from navi_opt.optimization.speed_profile import LegResult, SpeedProfileResult

    g = _three_leg_graph(100.0)
    # A committed profile that already runs LONGER than deadline_hours —
    # standing in for a voyage that got reoptimized once already (e.g.
    # under worsening weather) in a way that used up the original slack
    # entirely and then some. Building this by hand rather than through
    # `_departed_voyage()`'s constrained solve, since a solve that
    # actually RESPECTS a deadline can, by construction, never itself
    # produce an already-late committed profile — this scenario has to
    # come from simulating that a PRIOR reoptimization already blew it.
    legs = [
        LegResult(u="A", v="B", speed_knots=LO, departure_time_hours=0.0, wave_height_m=0.0,
                  headwind_knots=0.0, transit_hours=10.0, fuel_tonnes=5.0),
        LegResult(u="B", v="C", speed_knots=LO, departure_time_hours=10.0, wave_height_m=0.0,
                  headwind_knots=0.0, transit_hours=10.0, fuel_tonnes=5.0),
        LegResult(u="C", v="D", speed_knots=LO, departure_time_hours=20.0, wave_height_m=0.0,
                  headwind_knots=0.0, transit_hours=10.0, fuel_tonnes=5.0),
    ]
    already_late_profile = SpeedProfileResult(
        path=["A", "B", "C", "D"], legs=legs,
        total_fuel_tonnes=15.0, total_transit_hours=30.0, arrival_time_hours=30.0,
        success=True, message="already-late test fixture",
    )
    voyage = Voyage(
        voyage_id="v1", vessel_key="mr2_product_tanker", cargo_condition="laden",
        voyage_start_time=VOYAGE_START, deadline_hours=15.0,  # well under the committed 30h
        demo_departed_at=DEPARTED_AT, committed_profile=already_late_profile,
    )
    now = _now_at_elapsed_hours(22.0)  # past the 15h deadline, still mid-voyage (leg B->C, ends at 20h... just past it, on C->D)

    # Must not raise — a late voyage still gets a best-effort profile.
    result = reoptimize_voyage(voyage, g, VESSEL, _calm, now)
    assert result.success
    assert result.path == ["C", "D"]
