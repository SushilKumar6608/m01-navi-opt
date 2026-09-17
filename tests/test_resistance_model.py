import math

import pytest

from navi_opt.weather.resistance_model import (
    fuel_rate_tonnes_per_day,
    fuel_tonnes_for_leg,
    relative_headwind_component,
    wind_uv_to_speed_direction,
)
from navi_opt.weather.vessel_profiles import load_vessel_profile

VESSEL = load_vessel_profile("mr2_product_tanker")


# --- wind_uv_to_speed_direction ---


def test_wind_uv_pure_eastward_gives_speed_and_90_degrees():
    speed, direction = wind_uv_to_speed_direction(eastward=10.0, northward=0.0)
    assert speed == pytest.approx(10.0)
    assert direction == pytest.approx(90.0)


def test_wind_uv_pure_northward_gives_0_degrees():
    speed, direction = wind_uv_to_speed_direction(eastward=0.0, northward=10.0)
    assert speed == pytest.approx(10.0)
    assert direction == pytest.approx(0.0)


# --- relative_headwind_component ---


def test_headwind_directly_opposing_travel_is_full_speed():
    # Vessel heading due north (0deg). Wind blowing toward the south (180deg)
    # is a dead headwind — full wind speed should register as headwind.
    result = relative_headwind_component(
        vessel_heading_deg=0.0, wind_blowing_toward_deg=180.0, wind_speed=20.0
    )
    assert result == pytest.approx(20.0)


def test_tailwind_is_clipped_to_zero_not_negative():
    # Vessel heading due north, wind ALSO blowing toward north (a tailwind)
    # should clip to zero, not go negative.
    result = relative_headwind_component(
        vessel_heading_deg=0.0, wind_blowing_toward_deg=0.0, wind_speed=20.0
    )
    assert result == 0.0


def test_pure_crosswind_gives_zero_headwind_component():
    # Vessel heading due north, wind blowing due east (90deg, pure
    # crosswind) should contribute no headwind component.
    result = relative_headwind_component(
        vessel_heading_deg=0.0, wind_blowing_toward_deg=90.0, wind_speed=20.0
    )
    assert result == pytest.approx(0.0, abs=1e-9)


def test_45_degree_headwind_gives_cos45_component():
    # Wind blowing toward 135deg while vessel heads 0deg (north): the wind
    # is partially opposing (45deg off dead-ahead-opposing), so the headwind
    # component should be wind_speed * cos(45deg).
    result = relative_headwind_component(
        vessel_heading_deg=0.0, wind_blowing_toward_deg=135.0, wind_speed=10.0
    )
    assert result == pytest.approx(10.0 * math.cos(math.radians(45)), rel=1e-6)


# --- fuel_rate_tonnes_per_day ---


def test_fuel_rate_increases_with_speed():
    calm_low = fuel_rate_tonnes_per_day(10.0, 0.0, 0.0, VESSEL, "laden")
    calm_high = fuel_rate_tonnes_per_day(15.0, 0.0, 0.0, VESSEL, "laden")
    assert calm_high > calm_low


def test_fuel_rate_cubic_dominance_at_high_speed():
    # Going from 12 to 14 knots should cost noticeably more than a linear
    # scaling would predict, since the a*v^3 term dominates.
    rate_12 = fuel_rate_tonnes_per_day(12.0, 0.0, 0.0, VESSEL, "laden")
    rate_14 = fuel_rate_tonnes_per_day(14.0, 0.0, 0.0, VESSEL, "laden")
    linear_prediction = rate_12 * (14.0 / 12.0)
    assert rate_14 > linear_prediction


def test_wave_height_increases_fuel_rate():
    calm = fuel_rate_tonnes_per_day(12.0, 0.0, 0.0, VESSEL, "laden")
    rough = fuel_rate_tonnes_per_day(12.0, 4.0, 0.0, VESSEL, "laden")
    assert rough > calm


def test_headwind_increases_fuel_rate():
    no_wind = fuel_rate_tonnes_per_day(12.0, 0.0, 0.0, VESSEL, "laden")
    headwind = fuel_rate_tonnes_per_day(12.0, 0.0, 20.0, VESSEL, "laden")
    assert headwind > no_wind


def test_ballast_burns_less_fuel_than_laden_at_same_conditions():
    laden = fuel_rate_tonnes_per_day(12.0, 2.0, 5.0, VESSEL, "laden")
    ballast = fuel_rate_tonnes_per_day(12.0, 2.0, 5.0, VESSEL, "ballast")
    assert ballast < laden
    # and specifically by the configured factor
    assert ballast == pytest.approx(laden * VESSEL.cargo_condition_factor["ballast"])


def test_negative_wave_height_raises():
    with pytest.raises(ValueError):
        fuel_rate_tonnes_per_day(12.0, -1.0, 0.0, VESSEL, "laden")


def test_negative_headwind_raises():
    with pytest.raises(ValueError):
        fuel_rate_tonnes_per_day(12.0, 0.0, -1.0, VESSEL, "laden")


def test_speed_outside_bounds_raises():
    lo, hi = VESSEL.speed_bounds_knots
    with pytest.raises(ValueError):
        fuel_rate_tonnes_per_day(hi + 5.0, 0.0, 0.0, VESSEL, "laden")


def test_unknown_cargo_condition_raises():
    with pytest.raises(KeyError):
        fuel_rate_tonnes_per_day(12.0, 0.0, 0.0, VESSEL, "empty_hold")  # type: ignore[arg-type]


# --- fuel_tonnes_for_leg ---


def test_fuel_tonnes_for_leg_matches_manual_calculation():
    distance_nm = 120.0
    speed_knots = 12.0
    fuel, transit_hours = fuel_tonnes_for_leg(
        distance_nm, speed_knots, 1.0, 5.0, VESSEL, "laden", hourly_conversion_factor=1 / 24
    )

    assert transit_hours == pytest.approx(distance_nm / speed_knots)

    expected_rate_per_day = fuel_rate_tonnes_per_day(speed_knots, 1.0, 5.0, VESSEL, "laden")
    expected_fuel = expected_rate_per_day * (1 / 24) * transit_hours
    assert fuel == pytest.approx(expected_fuel)


def test_fuel_tonnes_for_leg_reads_conversion_factor_from_yaml_by_default():
    # Not passing hourly_conversion_factor should give the same answer as
    # passing the real value from vessel_profiles.yaml explicitly — i.e.
    # the default path actually reads the YAML, not a hardcoded guess.
    from navi_opt.weather.vessel_profiles import load_hourly_conversion_factor

    real_factor = load_hourly_conversion_factor()
    fuel_default, _ = fuel_tonnes_for_leg(100.0, 12.0, 1.0, 0.0, VESSEL, "laden")
    fuel_explicit, _ = fuel_tonnes_for_leg(
        100.0, 12.0, 1.0, 0.0, VESSEL, "laden", hourly_conversion_factor=real_factor
    )
    assert fuel_default == pytest.approx(fuel_explicit)
