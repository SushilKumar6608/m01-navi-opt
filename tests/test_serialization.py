import json

from navi_opt.optimization.speed_profile import LegResult, SpeedProfileResult
from navi_opt.streaming.serialization import (
    leg_result_from_dict,
    leg_result_to_dict,
    speed_profile_result_from_dict,
    speed_profile_result_to_dict,
)


def _leg(u="A", v="B") -> LegResult:
    return LegResult(
        u=u, v=v, speed_knots=12.5, departure_time_hours=3.0, wave_height_m=1.5,
        headwind_knots=4.0, transit_hours=8.0, fuel_tonnes=6.25,
    )


def _result() -> SpeedProfileResult:
    legs = [_leg("A", "B"), _leg("B", "C")]
    return SpeedProfileResult(
        path=["A", "B", "C"], legs=legs, total_fuel_tonnes=12.5, total_transit_hours=16.0,
        arrival_time_hours=16.0, success=True, message="ok",
    )


def test_leg_result_round_trips():
    leg = _leg()
    assert leg_result_from_dict(leg_result_to_dict(leg)) == leg


def test_leg_result_to_dict_is_actually_json_serializable():
    d = leg_result_to_dict(_leg())
    json.dumps(d)  # raises if not serializable


def test_speed_profile_result_round_trips():
    result = _result()
    round_tripped = speed_profile_result_from_dict(speed_profile_result_to_dict(result))
    assert round_tripped == result


def test_speed_profile_result_to_dict_is_actually_json_serializable_end_to_end():
    result = _result()
    payload = json.dumps(speed_profile_result_to_dict(result))
    round_tripped = speed_profile_result_from_dict(json.loads(payload))
    assert round_tripped == result


def test_speed_profile_result_to_dict_does_not_mutate_the_original():
    result = _result()
    original_legs = list(result.legs)
    speed_profile_result_to_dict(result)
    assert result.legs == original_legs
