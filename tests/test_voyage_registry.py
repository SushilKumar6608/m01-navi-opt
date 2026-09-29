from datetime import datetime, timedelta

import pytest

from navi_opt.optimization.speed_profile import LegResult, SpeedProfileResult
from navi_opt.streaming.voyage_registry import (
    DEMO_TIME_SCALE,
    Voyage,
    VoyageRegistry,
    current_node_and_remaining_path,
    elapsed_hours,
)

VOYAGE_START = datetime(2024, 6, 1, 0, 0)
DEPARTED_AT = datetime(2026, 1, 1, 12, 0)  # arbitrary real wall-clock "registration" time


def _leg(u, v, departure_time_hours, transit_hours, speed_knots=12.0) -> LegResult:
    return LegResult(
        u=u, v=v, speed_knots=speed_knots, departure_time_hours=departure_time_hours,
        wave_height_m=0.0, headwind_knots=0.0, transit_hours=transit_hours, fuel_tonnes=1.0,
    )


def _profile(path, leg_hours) -> SpeedProfileResult:
    """`leg_hours`: list of (departure_time_hours, transit_hours) per leg,
    same length as len(path) - 1."""
    legs = [_leg(u, v, dep, dur) for (u, v), (dep, dur) in zip(zip(path, path[1:]), leg_hours)]
    return SpeedProfileResult(
        path=path, legs=legs,
        total_fuel_tonnes=sum(l.fuel_tonnes for l in legs),
        total_transit_hours=sum(l.transit_hours for l in legs),
        arrival_time_hours=legs[-1].departure_time_hours + legs[-1].transit_hours if legs else 0.0,
        success=True, message="test fixture",
    )


def _voyage(path=("A", "B", "C", "D"), leg_hours=((0.0, 10.0), (10.0, 10.0), (20.0, 10.0)),
            deadline_hours=40.0, demo_departed_at=DEPARTED_AT) -> Voyage:
    return Voyage(
        voyage_id="v1", vessel_key="mr2_product_tanker", cargo_condition="laden",
        voyage_start_time=VOYAGE_START, deadline_hours=deadline_hours,
        demo_departed_at=demo_departed_at, committed_profile=_profile(list(path), leg_hours),
    )


# --- elapsed_hours: pure clock-scaling math ---


def test_elapsed_hours_is_zero_at_departure():
    v = _voyage()
    assert elapsed_hours(v, DEPARTED_AT) == pytest.approx(0.0)


def test_elapsed_hours_scales_real_seconds_by_demo_time_scale():
    v = _voyage()
    now = DEPARTED_AT + timedelta(seconds=60)
    expected = 60.0 / 3600.0 * DEMO_TIME_SCALE
    assert elapsed_hours(v, now) == pytest.approx(expected)


def test_elapsed_hours_never_negative_even_if_now_precedes_departure():
    v = _voyage()
    now = DEPARTED_AT - timedelta(hours=1)
    assert elapsed_hours(v, now) == pytest.approx(0.0)


# --- current_node_and_remaining_path: position from elapsed hours ---


def test_position_at_departure_is_the_first_node():
    v = _voyage()
    node, remaining = current_node_and_remaining_path(v, DEPARTED_AT)
    assert node == "A"
    assert remaining == ["A", "B", "C", "D"]


def test_position_mid_second_leg_is_at_node_b():
    v = _voyage()
    # 15 simulated hours in: leg A->B (0-10h) is done, leg B->C (10-20h) is underway.
    real_seconds = 15.0 / DEMO_TIME_SCALE * 3600.0
    now = DEPARTED_AT + timedelta(seconds=real_seconds)
    node, remaining = current_node_and_remaining_path(v, now)
    assert node == "B"
    assert remaining == ["B", "C", "D"]


def test_position_exactly_at_a_leg_boundary_is_the_arrived_node():
    v = _voyage()
    real_seconds = 20.0 / DEMO_TIME_SCALE * 3600.0  # exactly when leg B->C completes
    now = DEPARTED_AT + timedelta(seconds=real_seconds)
    node, remaining = current_node_and_remaining_path(v, now)
    assert node == "C"
    assert remaining == ["C", "D"]


def test_position_after_full_transit_is_destination_with_nothing_remaining():
    v = _voyage()
    real_seconds = 999.0 / DEMO_TIME_SCALE * 3600.0  # way past the 30h total transit
    now = DEPARTED_AT + timedelta(seconds=real_seconds)
    node, remaining = current_node_and_remaining_path(v, now)
    assert node == "D"
    assert remaining == ["D"]


# --- VoyageRegistry: register/commit/lookup ---


def test_registry_round_trips_a_registered_voyage():
    reg = VoyageRegistry()
    v = _voyage()
    reg.register(v)
    assert reg.get("v1") is v
    assert reg.all_voyage_ids() == ["v1"]


def test_registry_commit_replaces_profile_and_bumps_counter():
    reg = VoyageRegistry()
    v = _voyage()
    reg.register(v)
    new_profile = _profile(["B", "C", "D"], [(10.0, 8.0), (18.0, 8.0)])
    reg.commit("v1", new_profile)
    assert reg.get("v1").committed_profile is new_profile
    assert reg.get("v1").reoptimization_count == 1


def test_registry_commit_does_not_reset_the_demo_clock():
    # Confirms the module docstring's claim directly: reoptimizing must
    # NOT touch demo_departed_at, or current_node_and_remaining_path()
    # would silently "rewind" a voyage's simulated progress every time it
    # gets reoptimized.
    reg = VoyageRegistry()
    v = _voyage()
    reg.register(v)
    before = reg.get("v1").demo_departed_at
    reg.commit("v1", _profile(["B", "C", "D"], [(10.0, 8.0), (18.0, 8.0)]))
    assert reg.get("v1").demo_departed_at == before


def test_registry_remove_drops_the_voyage():
    reg = VoyageRegistry()
    reg.register(_voyage())
    reg.remove("v1")
    assert reg.all_voyage_ids() == []
    with pytest.raises(KeyError):
        reg.get("v1")


def test_affected_voyage_ids_finds_an_edge_still_ahead():
    reg = VoyageRegistry()
    reg.register(_voyage())  # remaining path at departure: A-B-C-D
    assert reg.affected_voyage_ids("B", "C", DEPARTED_AT) == ["v1"]


def test_affected_voyage_ids_excludes_an_edge_already_passed():
    reg = VoyageRegistry()
    reg.register(_voyage())
    real_seconds = 15.0 / DEMO_TIME_SCALE * 3600.0  # past leg A->B
    now = DEPARTED_AT + timedelta(seconds=real_seconds)
    assert reg.affected_voyage_ids("A", "B", now) == []


def test_affected_voyage_ids_excludes_unrelated_voyages():
    reg = VoyageRegistry()
    reg.register(_voyage())
    other = _voyage()
    other.voyage_id = "v2"
    other.committed_profile = _profile(["X", "Y"], [(0.0, 5.0)])
    reg.register(other)
    assert reg.affected_voyage_ids("B", "C", DEPARTED_AT) == ["v1"]
