import networkx as nx
import pytest

from navi_opt.optimization.pareto import (
    DEFAULT_SWEEP_MAXITER,
    ParetoPoint,
    default_epsilon_sweep,
    pareto_frontier,
    sweep_epsilon_constraint,
    weather_risk_m_hours,
)
from navi_opt.optimization.speed_profile import LegResult, SpeedProfileResult
from navi_opt.weather.vessel_profiles import load_vessel_profile

VESSEL = load_vessel_profile("mr2_product_tanker")
LO, HI = VESSEL.speed_bounds_knots


def _two_leg_graph(dist_nm_each: float) -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=0.0, lon=2.0)
    g.add_node("C", lat=0.0, lon=4.0)
    g.add_edge("A", "B", dist_nm=dist_nm_each)
    g.add_edge("B", "C", dist_nm=dist_nm_each)
    return g


def _fake_leg(wave_height_m: float, transit_hours: float) -> LegResult:
    return LegResult(
        u="A",
        v="B",
        speed_knots=12.0,
        departure_time_hours=0.0,
        wave_height_m=wave_height_m,
        headwind_knots=0.0,
        transit_hours=transit_hours,
        fuel_tonnes=1.0,
    )


def _fake_result(legs) -> SpeedProfileResult:
    return SpeedProfileResult(
        path=["A", "B"],
        legs=legs,
        total_fuel_tonnes=sum(leg.fuel_tonnes for leg in legs),
        total_transit_hours=sum(leg.transit_hours for leg in legs),
        arrival_time_hours=sum(leg.transit_hours for leg in legs),
        success=True,
        message="test fixture",
    )


def _point(fuel, duration, risk, corridor_index=0, epsilon=0.0) -> ParetoPoint:
    """Build a ParetoPoint directly for pure dominance-logic tests — the
    `result` field's internal consistency doesn't matter there, only the
    three objective fields `_dominates`/`pareto_frontier` actually read."""
    fake = _fake_result([_fake_leg(0.0, duration)])
    return ParetoPoint(
        corridor_index=corridor_index,
        epsilon_hours=epsilon,
        fuel_tonnes=fuel,
        transit_hours=duration,
        weather_risk_m_hours=risk,
        success=True,
        result=fake,
    )


# --- weather_risk_m_hours: closed-form check against hand-built legs ---


def test_weather_risk_is_sum_of_wave_height_times_transit_hours():
    legs = [_fake_leg(2.0, 10.0), _fake_leg(0.5, 20.0), _fake_leg(0.0, 5.0)]
    result = _fake_result(legs)
    # 2.0*10 + 0.5*20 + 0.0*5 = 20 + 10 + 0 = 30
    assert weather_risk_m_hours(result) == pytest.approx(30.0)


def test_weather_risk_is_zero_for_calm_water():
    legs = [_fake_leg(0.0, 10.0), _fake_leg(0.0, 5.0)]
    assert weather_risk_m_hours(_fake_result(legs)) == pytest.approx(0.0)


# --- default_epsilon_sweep: closed-form floor and spacing ---


def test_default_epsilon_sweep_starts_at_the_flat_out_floor():
    g = _two_leg_graph(100.0)
    sweep = default_epsilon_sweep(["A", "B", "C"], g, v_max_knots=HI, n_points=5, max_margin=1.5)
    floor = 200.0 / HI
    assert sweep[0] == pytest.approx(floor)
    assert sweep[-1] == pytest.approx(floor * 1.5)
    assert len(sweep) == 5
    assert sweep == sorted(sweep)  # monotonically increasing


def test_default_epsilon_sweep_rejects_too_few_points():
    g = _two_leg_graph(100.0)
    with pytest.raises(ValueError):
        default_epsilon_sweep(["A", "B", "C"], g, v_max_knots=HI, n_points=1)


# --- sweep_epsilon_constraint: real solves, closed-form fuel/duration checks ---


def test_sweep_skips_infeasible_epsilons_without_raising():
    g = _two_leg_graph(120.0)
    floor = 240.0 / HI

    def calm(u, v, t):
        return 0.0, 0.0

    epsilons = [floor * 0.5, floor * 1.1, floor * 1.5]  # first is infeasible
    points = sweep_epsilon_constraint(0, ["A", "B", "C"], g, VESSEL, "laden", calm, epsilons)
    assert len(points) == 2  # the infeasible one was skipped, not raised
    assert all(p.epsilon_hours in (floor * 1.1, floor * 1.5) for p in points)


def test_sweep_fuel_decreases_as_epsilon_relaxes_on_calm_water():
    # A looser deadline can never require more fuel than a tighter one on
    # the same corridor under the same weather — the optimizer always has
    # at least the option of the tighter solution's speeds available.
    g = _two_leg_graph(150.0)

    def calm(u, v, t):
        return 0.0, 0.0

    epsilons = default_epsilon_sweep(["A", "B", "C"], g, v_max_knots=HI, n_points=6, max_margin=1.8)
    points = sweep_epsilon_constraint(0, ["A", "B", "C"], g, VESSEL, "laden", calm, epsilons)
    fuels = [p.fuel_tonnes for p in points]
    assert fuels == sorted(fuels, reverse=True)  # non-increasing as epsilon grows


def test_sweep_points_carry_the_correct_corridor_index():
    g = _two_leg_graph(120.0)

    def calm(u, v, t):
        return 0.0, 0.0

    epsilons = default_epsilon_sweep(["A", "B", "C"], g, v_max_knots=HI, n_points=3, max_margin=1.5)
    points = sweep_epsilon_constraint(7, ["A", "B", "C"], g, VESSEL, "laden", calm, epsilons)
    assert all(p.corridor_index == 7 for p in points)


def test_sweep_defaults_to_bounded_maxiter_not_unbounded():
    # Direct check that the sweep's default isn't scipy's own unbounded
    # default (None) — a caller relying on the module-level default must
    # actually get a bounded worst case, not silently fall through to
    # unbounded per-solve iteration counts.
    assert DEFAULT_SWEEP_MAXITER is not None
    assert DEFAULT_SWEEP_MAXITER > 0


def test_sweep_calls_on_progress_once_per_epsilon_in_order():
    g = _two_leg_graph(120.0)

    def calm(u, v, t):
        return 0.0, 0.0

    epsilons = default_epsilon_sweep(["A", "B", "C"], g, v_max_knots=HI, n_points=4, max_margin=1.5)
    seen = []

    def _capture(idx, eps, point, elapsed):
        seen.append((idx, eps, point, elapsed))

    points = sweep_epsilon_constraint(
        0, ["A", "B", "C"], g, VESSEL, "laden", calm, epsilons, on_progress=_capture
    )
    assert len(seen) == len(epsilons)  # called for every epsilon, feasible or not
    assert [idx for idx, *_ in seen] == list(range(len(epsilons)))  # in order
    assert [eps for _, eps, _, _ in seen] == epsilons
    # every callback's elapsed time is non-negative and its point (when
    # present) matches what actually landed in the returned list
    feasible_points_seen = [p for _, _, p, _ in seen if p is not None]
    assert feasible_points_seen == points
    assert all(elapsed >= 0.0 for *_, elapsed in seen)


def test_sweep_warm_starts_each_epsilon_from_the_previous_converged_result():
    # Direct check that warm-starting is actually happening: capture every
    # x0 optimize_speed_profile is called with (via monkeypatching) and
    # confirm epsilon i>0 is started from epsilon i-1's converged speeds,
    # not the cold design_speed_knots guess every time.
    import navi_opt.optimization.pareto as pareto_mod

    g = _two_leg_graph(120.0)

    def calm(u, v, t):
        return 0.0, 0.0

    epsilons = default_epsilon_sweep(["A", "B", "C"], g, v_max_knots=HI, n_points=4, max_margin=1.6)
    seen_x0 = []
    real_optimize = pareto_mod.optimize_speed_profile

    def _spy(*args, **kwargs):
        seen_x0.append(kwargs.get("initial_speeds_knots"))
        return real_optimize(*args, **kwargs)

    pareto_mod.optimize_speed_profile = _spy
    try:
        points = sweep_epsilon_constraint(0, ["A", "B", "C"], g, VESSEL, "laden", calm, epsilons)
    finally:
        pareto_mod.optimize_speed_profile = real_optimize

    assert seen_x0[0] is None  # first epsilon: no prior result, cold start
    for i in range(1, len(seen_x0)):
        prev_speeds = [leg.speed_knots for leg in points[i - 1].result.legs]
        assert seen_x0[i] == pytest.approx(prev_speeds)


def test_sweep_on_progress_reports_none_for_infeasible_epsilon():
    g = _two_leg_graph(120.0)
    floor = 240.0 / HI

    def calm(u, v, t):
        return 0.0, 0.0

    seen = []
    sweep_epsilon_constraint(
        0, ["A", "B", "C"], g, VESSEL, "laden", calm, [floor * 0.5],
        on_progress=lambda idx, eps, point, elapsed: seen.append(point),
    )
    assert seen == [None]


# --- pareto_frontier: hand-built dominance cases, not just "returns something" ---


def test_dominance_survives_sub_tolerance_floating_point_noise():
    # Regression for a real bug caught before delivery: two points whose
    # duration differs only by numerical noise far smaller than
    # DURATION_TOL_HOURS (e.g. two independent SLSQP solves landing on
    # the same flat-out speed floor) must still resolve as dominance when
    # one is strictly worse on fuel and risk at that same effective
    # duration — a raw `<=` comparison flips on noise like this and lets
    # a strictly-worse point survive the filter.
    a = _point(fuel=40.0, duration=12.500000074499013, risk=0.0)
    b = _point(fuel=45.0, duration=12.500000074499010, risk=62.5)  # duration noise-lower than a
    frontier = pareto_frontier([a, b])
    assert a in frontier
    assert b not in frontier


def test_dominated_point_is_removed():
    # B is strictly worse than A on every objective -> B must be dropped.
    a = _point(fuel=100.0, duration=200.0, risk=10.0)
    b = _point(fuel=110.0, duration=210.0, risk=15.0)
    frontier = pareto_frontier([a, b])
    assert a in frontier
    assert b not in frontier


def test_mutually_nondominated_points_both_survive():
    # A is cheaper on fuel, B is faster and lower-risk -> neither dominates.
    a = _point(fuel=100.0, duration=250.0, risk=20.0)
    b = _point(fuel=120.0, duration=200.0, risk=10.0)
    frontier = pareto_frontier([a, b])
    assert a in frontier
    assert b in frontier


def test_identical_points_on_all_three_objectives_both_survive():
    # Ties are not dominance in either direction (strictly-better check
    # fails both ways), so neither is removed.
    a = _point(fuel=100.0, duration=200.0, risk=10.0, epsilon=1.0)
    b = _point(fuel=100.0, duration=200.0, risk=10.0, epsilon=2.0)
    frontier = pareto_frontier([a, b])
    assert len(frontier) == 2


def test_frontier_drops_points_dominated_across_corridors_not_just_within_one():
    # Dominance must be checked across the whole pooled set, not per
    # corridor — a corridor-2 point strictly worse than a corridor-1 point
    # on all three objectives must still be dropped.
    good = _point(fuel=100.0, duration=200.0, risk=5.0, corridor_index=1)
    bad_other_corridor = _point(fuel=105.0, duration=205.0, risk=6.0, corridor_index=2)
    frontier = pareto_frontier([good, bad_other_corridor])
    assert good in frontier
    assert bad_other_corridor not in frontier


def test_frontier_of_three_points_keeps_only_the_two_nondominated():
    # A dominates C outright; B is a genuine trade-off against both.
    a = _point(fuel=100.0, duration=250.0, risk=20.0)
    b = _point(fuel=130.0, duration=180.0, risk=8.0)
    c = _point(fuel=110.0, duration=260.0, risk=25.0)  # dominated by A
    frontier = pareto_frontier([a, b, c])
    assert len(frontier) == 2
    assert a in frontier and b in frontier and c not in frontier


# --- end-to-end: sweep + frontier compose against a real weather-varying corridor ---


def test_sweep_and_frontier_compose_on_a_weather_varying_corridor():
    g = _two_leg_graph(150.0)

    # Leg B->C is rough, leg A->B is calm — same asymmetry used to validate
    # the tight-laycan feature, reused here so this test is checking
    # composition, not re-deriving the resistance model's behavior.
    def rough_second_leg(u, v, t):
        if (u, v) == ("B", "C"):
            return 4.0, 20.0
        return 0.0, 0.0

    epsilons = default_epsilon_sweep(["A", "B", "C"], g, v_max_knots=HI, n_points=8, max_margin=1.7)
    points = sweep_epsilon_constraint(
        0, ["A", "B", "C"], g, VESSEL, "laden", rough_second_leg, epsilons
    )
    assert len(points) >= 2  # at least some epsilons were feasible

    frontier = pareto_frontier(points)
    # The frontier must be non-empty and every point on it must actually
    # come from the swept set (frontier is a filter, never invents points).
    assert 0 < len(frontier) <= len(points)
    assert all(p in points for p in frontier)

    # Every retained point's weather risk should be attributable to the
    # rough leg alone (the calm leg always samples 0.0 wave height), a
    # direct closed-form check that weather_risk_m_hours is reading the
    # right leg's data, not just "some positive number came back".
    for p in points:
        rough_leg = next(leg for leg in p.result.legs if (leg.u, leg.v) == ("B", "C"))
        assert p.weather_risk_m_hours == pytest.approx(rough_leg.wave_height_m * rough_leg.transit_hours)
