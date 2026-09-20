import networkx as nx
import pytest

from navi_opt.optimization.speed_profile import (
    calm_weather_lookup,
    optimize_speed_profile,
)
from navi_opt.weather.resistance_model import fuel_tonnes_for_leg
from navi_opt.weather.vessel_profiles import load_hourly_conversion_factor, load_vessel_profile

VESSEL = load_vessel_profile("mr2_product_tanker")
LO, HI = VESSEL.speed_bounds_knots


def _single_leg_graph(dist_nm: float) -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=0.0, lon=1.0)
    g.add_edge("A", "B", dist_nm=dist_nm)
    return g


def _two_leg_graph(dist_nm_each: float) -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=0.0, lon=2.0)
    g.add_node("C", lat=0.0, lon=4.0)
    g.add_edge("A", "B", dist_nm=dist_nm_each)
    g.add_edge("B", "C", dist_nm=dist_nm_each)
    return g


# --- unconstrained: fuel is monotonically increasing in speed, so the
# closed-form optimum with no deadline is exactly the vessel's minimum
# speed bound — not just "some low-ish number the solver landed on" ---


def test_no_deadline_picks_minimum_speed_bound_exactly():
    g = _single_leg_graph(120.0)
    result = optimize_speed_profile(
        ["A", "B"], g, VESSEL, "laden", weather_lookup=calm_weather_lookup
    )
    assert result.success
    assert result.legs[0].speed_knots == pytest.approx(LO, abs=0.05)


def test_no_deadline_minimum_speed_holds_even_under_headwind():
    # Weather increases the fuel rate at every speed but doesn't change the
    # sign of dF/dv (still positive for v>0), so the unconstrained optimum
    # stays at the same minimum speed bound — a real behavioral check, not
    # just "does it run under weather".
    g = _single_leg_graph(120.0)

    def stormy(u, v, t):
        return 4.0, 20.0

    result = optimize_speed_profile(["A", "B"], g, VESSEL, "laden", weather_lookup=stormy)
    assert result.legs[0].speed_knots == pytest.approx(LO, abs=0.05)


# --- deadline constraint: a binding deadline should force exactly the
# speed needed to make it, computable in closed form for a single leg ---


def test_binding_deadline_forces_exact_required_speed_single_leg():
    dist_nm = 150.0
    deadline_hours = 12.0  # requires exactly 12.5 kn to make it in dist/deadline
    required_speed = dist_nm / deadline_hours
    assert LO < required_speed < HI  # sanity: this deadline is meaningfully binding

    g = _single_leg_graph(dist_nm)
    result = optimize_speed_profile(
        ["A", "B"],
        g,
        VESSEL,
        "laden",
        weather_lookup=calm_weather_lookup,
        max_transit_hours=deadline_hours,
    )
    assert result.success
    assert result.legs[0].speed_knots == pytest.approx(required_speed, abs=0.05)
    assert result.total_transit_hours == pytest.approx(deadline_hours, abs=0.05)


def test_binding_deadline_on_symmetric_two_leg_corridor_splits_evenly():
    # Two legs of equal distance, equal (zero) weather: by convexity and
    # symmetry the optimal split under a binding deadline is equal speed on
    # both legs, not an arbitrary distribution that merely sums to the
    # deadline.
    g = _two_leg_graph(120.0)
    deadline_hours = 16.0
    result = optimize_speed_profile(
        ["A", "B", "C"],
        g,
        VESSEL,
        "laden",
        weather_lookup=calm_weather_lookup,
        max_transit_hours=deadline_hours,
    )
    assert result.success
    speeds = [leg.speed_knots for leg in result.legs]
    assert speeds[0] == pytest.approx(speeds[1], abs=0.05)
    assert result.total_transit_hours == pytest.approx(deadline_hours, abs=0.05)


def test_infeasible_deadline_raises_value_error_before_optimizing():
    g = _single_leg_graph(300.0)
    min_possible = 300.0 / HI
    with pytest.raises(ValueError, match="infeasible"):
        optimize_speed_profile(
            ["A", "B"],
            g,
            VESSEL,
            "laden",
            max_transit_hours=min_possible - 1.0,
        )


# --- weather wiring ---


def test_weather_affects_only_the_leg_it_applies_to():
    g = _two_leg_graph(120.0)

    def weather(u, v, t):
        if (u, v) == ("B", "C"):
            return 3.0, 15.0
        return 0.0, 0.0

    result = optimize_speed_profile(
        ["A", "B", "C"], g, VESSEL, "laden", weather_lookup=weather
    )
    assert result.legs[0].wave_height_m == 0.0
    assert result.legs[0].headwind_knots == 0.0
    assert result.legs[1].wave_height_m == 3.0
    assert result.legs[1].headwind_knots == 15.0
    assert result.legs[1].fuel_tonnes > result.legs[0].fuel_tonnes


def test_calm_weather_lookup_is_always_zero():
    assert calm_weather_lookup("A", "B", 123.4) == (0.0, 0.0)


# --- bookkeeping / consistency ---


def test_total_fuel_matches_sum_of_leg_fuel():
    g = _two_leg_graph(120.0)
    result = optimize_speed_profile(["A", "B", "C"], g, VESSEL, "laden")
    assert result.total_fuel_tonnes == pytest.approx(
        sum(leg.fuel_tonnes for leg in result.legs)
    )


def test_arrival_time_accounts_for_start_time_offset():
    g = _single_leg_graph(140.0)
    result = optimize_speed_profile(
        ["A", "B"], g, VESSEL, "laden", start_time_hours=50.0
    )
    assert result.legs[0].departure_time_hours == pytest.approx(50.0)
    assert result.arrival_time_hours == pytest.approx(50.0 + result.total_transit_hours)


def test_each_leg_fuel_matches_resistance_model_directly():
    # Regression-style check that speed_profile.py isn't reimplementing the
    # fuel math independently — it must reuse fuel_tonnes_for_leg exactly,
    # so recomputing with the same inputs must match bit-for-bit (modulo
    # the solver's own float tolerance).
    g = _single_leg_graph(180.0)
    factor = load_hourly_conversion_factor()
    result = optimize_speed_profile(["A", "B"], g, VESSEL, "ballast")
    leg = result.legs[0]

    expected_fuel, expected_hours = fuel_tonnes_for_leg(
        180.0, leg.speed_knots, leg.wave_height_m, leg.headwind_knots,
        VESSEL, "ballast", hourly_conversion_factor=factor,
    )
    assert leg.fuel_tonnes == pytest.approx(expected_fuel)
    assert leg.transit_hours == pytest.approx(expected_hours)


def test_path_with_fewer_than_two_nodes_raises():
    g = _single_leg_graph(100.0)
    with pytest.raises(ValueError):
        optimize_speed_profile(["A"], g, VESSEL, "laden")


def test_missing_edge_raises():
    g = _single_leg_graph(100.0)
    g.add_node("Z", lat=5.0, lon=5.0)
    with pytest.raises(ValueError):
        optimize_speed_profile(["A", "Z"], g, VESSEL, "laden")


def test_missing_dist_nm_attribute_raises():
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=0.0, lon=1.0)
    g.add_edge("A", "B")  # no dist_nm
    with pytest.raises(KeyError):
        optimize_speed_profile(["A", "B"], g, VESSEL, "laden")


# --- maxiter: opt-in cap for callers running many solves back-to-back
# (added for pareto.py's epsilon sweep — see its DEFAULT_SWEEP_MAXITER
# docstring and findings.md's Phase 4 entry for why) ---


def test_maxiter_defaults_to_scipy_default_unbounded():
    # Omitting maxiter must reproduce the exact pre-existing closed-form
    # result — this is the regression check that adding the parameter
    # didn't change behavior for every caller that doesn't pass it.
    g = _single_leg_graph(120.0)
    result = optimize_speed_profile(
        ["A", "B"], g, VESSEL, "laden", weather_lookup=calm_weather_lookup
    )
    assert result.success
    assert result.legs[0].speed_knots == pytest.approx(LO, abs=0.05)


# --- initial_speeds_knots: warm-start opt-in (added for pareto.py's
# epsilon sweep — see findings.md's Phase 4 entry) ---


def test_initial_speeds_knots_defaults_to_unchanged_behavior():
    # Omitting it must reproduce the exact pre-existing closed-form result.
    g = _single_leg_graph(120.0)
    result = optimize_speed_profile(
        ["A", "B"], g, VESSEL, "laden", weather_lookup=calm_weather_lookup
    )
    assert result.success
    assert result.legs[0].speed_knots == pytest.approx(LO, abs=0.05)


def test_initial_speeds_knots_wrong_length_raises():
    g = _two_leg_graph(100.0)
    with pytest.raises(ValueError):
        optimize_speed_profile(
            ["A", "B", "C"], g, VESSEL, "laden",
            weather_lookup=calm_weather_lookup, initial_speeds_knots=[12.0],  # needs 2
        )


def test_initial_speeds_knots_still_converges_to_the_same_closed_form_optimum():
    # A warm start (even a deliberately bad/off one) must not change WHERE
    # the solver ends up on an unconstrained problem with a known closed-
    # form optimum — only how fast it gets there.
    g = _single_leg_graph(120.0)
    result = optimize_speed_profile(
        ["A", "B"], g, VESSEL, "laden",
        weather_lookup=calm_weather_lookup, initial_speeds_knots=[HI],  # start at the wrong end
    )
    assert result.success
    assert result.legs[0].speed_knots == pytest.approx(LO, abs=0.05)


def test_initial_speeds_knots_out_of_bounds_is_clamped_not_rejected():
    # A previous solve's speed is only ever a starting GUESS — passing one
    # outside this call's own bounds must not raise, just get clamped.
    g = _single_leg_graph(150.0)
    result = optimize_speed_profile(
        ["A", "B"], g, VESSEL, "laden",
        weather_lookup=calm_weather_lookup, initial_speeds_knots=[HI + 100.0],
    )
    assert result.success


# --- max_seconds: hard wall-clock budget per solve (added for pareto.py's
# epsilon sweep after a real run showed maxiter alone doesn't bound
# wall-clock time — see findings.md's Phase 4 entry) ---


def test_max_seconds_defaults_to_unbounded():
    # Omitting it must reproduce the exact pre-existing closed-form result.
    g = _single_leg_graph(120.0)
    result = optimize_speed_profile(
        ["A", "B"], g, VESSEL, "laden", weather_lookup=calm_weather_lookup
    )
    assert result.success
    assert result.legs[0].speed_knots == pytest.approx(LO, abs=0.05)


def test_max_seconds_bounds_wall_clock_time_even_with_many_slow_legs():
    # Direct regression for a real bug caught mid-project: an EARLIER
    # implementation checked the time budget once per SLSQP iteration (via
    # a `callback`), which measurably failed here — one iteration's
    # finite-difference gradient can call the objective dozens of times
    # before a callback ever fires, so a slow per-leg lookup blew the
    # budget by 18x (37.2s actual vs. a 2.0s budget) in the scenario that
    # exposed it. Fixed by checking before every LEG inside the evaluation
    # loop itself, not once per call or once per iteration. This test
    # reproduces that exact scenario (many legs, an artificially slow
    # weather_lookup, a real deadline forcing real SLSQP iterations) and
    # asserts the fix actually holds, with a generous but real margin.
    import time as _time

    N = 60
    g = nx.DiGraph()
    for i in range(N):
        g.add_node(str(i), lat=0.0, lon=float(i))
    for i in range(N - 1):
        g.add_edge(str(i), str(i + 1), dist_nm=20.0)
    path = [str(i) for i in range(N)]

    def slow_weather(u, v, t):
        _time.sleep(0.01)
        return 2.0, 10.0

    dist_total = 20.0 * (N - 1)
    deadline_hours = dist_total / HI * 1.3
    budget_seconds = 2.0

    t0 = _time.time()
    result = optimize_speed_profile(
        path, g, VESSEL, "laden", weather_lookup=slow_weather,
        max_transit_hours=deadline_hours, max_seconds=budget_seconds,
    )
    elapsed = _time.time() - t0

    assert not result.success
    assert "max_seconds" in result.message
    # Real margin, not the tight ~0.6s observed once locally: this must
    # stay far below the ~37s the old per-iteration check actually took on
    # this exact scenario, not just squeak under some arbitrary number.
    assert elapsed < budget_seconds + 5.0
    # A timed-out solve still returns a real, usable profile (the legs
    # walked at the last-attempted speeds), not an empty/broken result.
    assert len(result.legs) == N - 1


def test_maxiter_is_actually_forwarded_to_the_solver():
    # A single-iteration cap on a corridor with a binding deadline (far
    # from the unconstrained optimum, so it needs real iterations to
    # converge) should be unable to fully converge — proves maxiter is
    # reaching scipy's minimize(), not silently dropped.
    dist_nm = 150.0
    deadline_hours = 12.0
    g = _single_leg_graph(dist_nm)
    capped = optimize_speed_profile(
        ["A", "B"], g, VESSEL, "laden",
        weather_lookup=calm_weather_lookup, max_transit_hours=deadline_hours, maxiter=1,
    )
    uncapped = optimize_speed_profile(
        ["A", "B"], g, VESSEL, "laden",
        weather_lookup=calm_weather_lookup, max_transit_hours=deadline_hours,
    )
    assert uncapped.success
    # A 1-iteration solve either fails to converge or lands measurably
    # farther from the true optimum than the uncapped solve — either way,
    # capping must visibly change the outcome, not be a no-op.
    same_speed = capped.legs[0].speed_knots == pytest.approx(
        uncapped.legs[0].speed_knots, abs=1e-6
    )
    assert not capped.success or not same_speed
