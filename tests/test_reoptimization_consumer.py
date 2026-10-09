from datetime import datetime, timedelta

import networkx as nx
import pytest

from navi_opt.optimization.speed_profile import optimize_speed_profile
from navi_opt.streaming.reoptimization_consumer import (
    handle_voyage_departed,
    handle_weather_update,
)
from navi_opt.streaming.serialization import speed_profile_result_to_dict
from navi_opt.streaming.voyage_registry import DEMO_TIME_SCALE, Voyage, VoyageRegistry
from navi_opt.weather.vessel_profiles import load_vessel_profile

VESSEL = load_vessel_profile("mr2_product_tanker")
HI = VESSEL.speed_bounds_knots[1]
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


def _now_at_elapsed_hours(hours: float) -> datetime:
    real_seconds = hours / DEMO_TIME_SCALE * 3600.0
    return DEPARTED_AT + timedelta(seconds=real_seconds)


def _departed_payload(graph, deadline_hours, voyage_id="v1") -> dict:
    initial = optimize_speed_profile(
        ["A", "B", "C", "D"], graph, VESSEL, "laden",
        weather_lookup=_calm, start_time_hours=0.0, max_transit_hours=deadline_hours,
    )
    return {
        "voyage_id": voyage_id,
        "vessel_key": "mr2_product_tanker",
        "cargo_condition": "laden",
        "voyage_start_time": VOYAGE_START.isoformat(),
        "deadline_hours": deadline_hours,
        "demo_departed_at": DEPARTED_AT.isoformat(),
        "committed_profile": speed_profile_result_to_dict(initial),
    }


# --- handle_voyage_departed ---


def test_handle_voyage_departed_registers_and_returns_the_voyage():
    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    payload = _departed_payload(g, deadline_hours=floor_leg_hours * 3 * 1.5)
    reg = VoyageRegistry()

    voyage = handle_voyage_departed(reg, payload)

    assert isinstance(voyage, Voyage)
    assert voyage.voyage_id == "v1"
    assert reg.get("v1") is voyage
    assert voyage.committed_profile.path == ["A", "B", "C", "D"]


def test_handle_voyage_departed_raises_key_error_on_missing_field():
    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    payload = _departed_payload(g, deadline_hours=floor_leg_hours * 3 * 1.5)
    del payload["deadline_hours"]
    reg = VoyageRegistry()

    with pytest.raises(KeyError):
        handle_voyage_departed(reg, payload)


# --- handle_weather_update ---


def test_handle_weather_update_reoptimizes_an_affected_voyage():
    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    deadline = floor_leg_hours * 3 * 1.5
    payload = _departed_payload(g, deadline_hours=deadline)
    reg = VoyageRegistry()
    voyage = handle_voyage_departed(reg, payload)

    # Advance just past leg A->B's actual solved duration, so B->C is ahead.
    leg_ab = voyage.committed_profile.legs[0]
    now = _now_at_elapsed_hours(leg_ab.departure_time_hours + leg_ab.transit_hours + 0.01)

    event = {"u": "B", "v": "C"}
    results = handle_weather_update(reg, g, _calm, event, now)

    assert len(results) == 1
    result = results[0]
    assert result.voyage_id == "v1"
    assert (result.trigger_u, result.trigger_v) == ("B", "C")
    # The registry's committed profile should now reflect the reoptimized result.
    assert reg.get("v1").committed_profile.path == ["B", "C", "D"]
    assert reg.get("v1").reoptimization_count == 1


def test_handle_weather_update_ignores_a_voyage_whose_remaining_path_lacks_the_edge():
    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    deadline = floor_leg_hours * 3 * 1.5
    payload = _departed_payload(g, deadline_hours=deadline)
    reg = VoyageRegistry()
    voyage = handle_voyage_departed(reg, payload)

    # Advance past leg A->B: only B->C and C->D remain, so A->B no longer applies.
    leg_ab = voyage.committed_profile.legs[0]
    now = _now_at_elapsed_hours(leg_ab.departure_time_hours + leg_ab.transit_hours + 0.01)

    event = {"u": "A", "v": "B"}
    results = handle_weather_update(reg, g, _calm, event, now)

    assert results == []
    assert reg.get("v1").reoptimization_count == 0


def test_handle_weather_update_skips_a_voyage_that_has_already_arrived():
    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    deadline = floor_leg_hours * 3 * 1.5
    payload = _departed_payload(g, deadline_hours=deadline)
    reg = VoyageRegistry()
    handle_voyage_departed(reg, payload)

    # Way past arrival — affected_voyage_ids() itself should already exclude
    # this voyage (nothing remains ahead), so no reoptimization/raise happens.
    now = _now_at_elapsed_hours(floor_leg_hours * 3 * 5.0)

    event = {"u": "B", "v": "C"}
    results = handle_weather_update(reg, g, _calm, event, now)

    assert results == []


def test_handle_weather_update_reflects_fuel_and_transit_deltas():
    g = _three_leg_graph(120.0)
    floor_leg_hours = 120.0 / HI
    deadline = floor_leg_hours * 3 * 1.8
    payload = _departed_payload(g, deadline_hours=deadline)
    reg = VoyageRegistry()
    voyage = handle_voyage_departed(reg, payload)

    leg_ab = voyage.committed_profile.legs[0]
    now = _now_at_elapsed_hours(leg_ab.departure_time_hours + leg_ab.transit_hours + 0.01)
    old_fuel = voyage.committed_profile.total_fuel_tonnes
    old_transit = voyage.committed_profile.total_transit_hours

    event = {"u": "B", "v": "C"}
    results = handle_weather_update(reg, g, _calm, event, now)

    assert len(results) == 1
    result = results[0]
    assert result.old_fuel_tonnes == pytest.approx(old_fuel)
    assert result.old_transit_hours == pytest.approx(old_transit)
    assert result.new_fuel_tonnes == pytest.approx(reg.get("v1").committed_profile.total_fuel_tonnes)
    assert result.new_transit_hours == pytest.approx(reg.get("v1").committed_profile.total_transit_hours)
    assert result.committed_profile["path"] == ["B", "C", "D"]


def test_old_remaining_fields_compare_like_for_like_not_against_the_whole_voyage():
    # Regression for the first real run: "369.7 -> 235.5t" compared a whole
    # voyage against only its remaining legs and read like a 36% saving.
    g = _three_leg_graph(120.0)
    floor_leg_hours = 120.0 / HI
    deadline = floor_leg_hours * 3 * 1.8
    reg = VoyageRegistry()
    voyage = handle_voyage_departed(reg, _departed_payload(g, deadline_hours=deadline))
    old_profile = voyage.committed_profile

    leg_ab = old_profile.legs[0]
    now = _now_at_elapsed_hours(leg_ab.departure_time_hours + leg_ab.transit_hours + 0.01)

    (result,) = handle_weather_update(reg, g, _calm, {"u": "B", "v": "C"}, now)

    old_tail = old_profile.legs[1:]  # A->B is already sailed; B->C, C->D remain
    assert result.old_remaining_fuel_tonnes == pytest.approx(sum(l.fuel_tonnes for l in old_tail))
    assert result.old_remaining_transit_hours == pytest.approx(sum(l.transit_hours for l in old_tail))
    # The remaining-legs baseline must be strictly smaller than the whole
    # profile (a leg was sailed) — the exact gap that made the naive
    # old-vs-new comparison misleading.
    assert result.old_remaining_fuel_tonnes < result.old_fuel_tonnes
    assert result.old_remaining_transit_hours < result.old_transit_hours
    # With unchanged calm weather the re-solve of the same legs should land
    # close to the old plan for those legs — not 30%+ "cheaper".
    assert result.new_fuel_tonnes == pytest.approx(result.old_remaining_fuel_tonnes, rel=0.05)


def test_handle_weather_update_reoptimizes_multiple_affected_voyages():
    g = _three_leg_graph(100.0)
    floor_leg_hours = 100.0 / HI
    deadline = floor_leg_hours * 3 * 1.5
    reg = VoyageRegistry()

    v1 = handle_voyage_departed(reg, _departed_payload(g, deadline_hours=deadline, voyage_id="v1"))
    v2 = handle_voyage_departed(reg, _departed_payload(g, deadline_hours=deadline, voyage_id="v2"))

    leg_ab = v1.committed_profile.legs[0]
    now = _now_at_elapsed_hours(leg_ab.departure_time_hours + leg_ab.transit_hours + 0.01)

    event = {"u": "B", "v": "C"}
    results = handle_weather_update(reg, g, _calm, event, now)

    assert {r.voyage_id for r in results} == {"v1", "v2"}
    assert reg.get("v1").reoptimization_count == 1
    assert reg.get("v2").reoptimization_count == 1
