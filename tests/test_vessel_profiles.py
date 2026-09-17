import pytest

from navi_opt.weather.vessel_profiles import (
    load_eca_price_premium,
    load_hourly_conversion_factor,
    load_vessel_profile,
    load_vessel_profiles,
)


def test_load_vessel_profiles_has_expected_vessels():
    profiles = load_vessel_profiles()
    assert "mr2_product_tanker" in profiles
    assert "aframax_tanker" in profiles
    assert "vlcc_tanker" in profiles


def test_hourly_conversion_factor_is_one_twenty_fourth():
    factor = load_hourly_conversion_factor()
    assert factor == pytest.approx(1 / 24, rel=1e-6)


def test_vessel_profile_fields_are_sane():
    vessel = load_vessel_profile("mr2_product_tanker")
    assert vessel.dwt_tonnes > 0
    assert vessel.design_speed_knots > 0
    lo, hi = vessel.speed_bounds_knots
    assert lo < hi
    assert lo <= vessel.design_speed_knots <= hi
    assert 0 < vessel.cargo_condition_factor["ballast"] < vessel.cargo_condition_factor["laden"]


def test_unknown_vessel_key_raises():
    with pytest.raises(KeyError):
        load_vessel_profile("nonexistent_vessel_class")


def test_eca_price_premium_is_positive():
    assert load_eca_price_premium() > 0
