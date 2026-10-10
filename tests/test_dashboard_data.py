import copy
from datetime import timedelta

import pandas as pd
import pytest

from navi_opt.dashboard.data import (
    build_voyage_views,
    compare_remaining,
    demo_clock_note,
    load_frontier,
    path_coordinates,
    profile_leg_frame,
    summarize_frontier,
)
from navi_opt.streaming.serialization import speed_profile_result_from_dict
from tests.dashboard_fixtures import make_scenario


@pytest.fixture(scope="module")
def scenario():
    return make_scenario()


# --- load_frontier ---

NEW_HEADER = ("corridor_index,epsilon_hours,transit_hours,fuel_tonnes,weather_risk_m_hours,"
              "severe_weather_hours_ge_4.5m,success,on_frontier\n")
OLD_HEADER = "corridor_index,epsilon_hours,transit_hours,fuel_tonnes,weather_risk_m_hours,success,on_frontier\n"


def test_load_frontier_new_format_parses_severe_column_and_threshold(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text(NEW_HEADER + "0,260.0,259.1,580.4,812.0,3.5,True,True\n1,300.0,298.0,560.2,790.0,0.0,False,False\n")
    df = load_frontier(p)
    assert list(df["corridor"]) == [1, 2]  # 1-based for display
    assert df.attrs["severe_threshold_m"] == pytest.approx(4.5)
    assert df["severe_weather_hours"].tolist() == [3.5, 0.0]
    assert df["on_frontier"].tolist() == [True, False]
    assert df["success"].tolist() == [True, False]


def test_load_frontier_old_format_has_nan_severe_never_zero(tmp_path):
    # An older sweep file predates the severe-weather column. NaN, not 0.0:
    # zero would claim "no severe weather" for data that never measured it.
    p = tmp_path / "old.csv"
    p.write_text(OLD_HEADER + "0,260.0,259.1,580.4,812.0,True,True\n")
    df = load_frontier(p)
    assert df["severe_weather_hours"].isna().all()
    assert df.attrs["severe_threshold_m"] is None


def test_load_frontier_missing_file_raises_with_helpful_message(tmp_path):
    with pytest.raises(FileNotFoundError, match="run_pareto_sweep"):
        load_frontier(tmp_path / "nope.csv")


def test_load_frontier_missing_required_column_names_it(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("corridor_index,fuel_tonnes\n0,1.0\n")
    with pytest.raises(ValueError, match="transit_hours"):
        load_frontier(p)


def test_summarize_frontier_counts_and_ranges(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text(NEW_HEADER
                 + "0,260,259,580,800,1.0,True,True\n"
                 + "0,300,298,560,790,0.0,False,True\n"
                 + "1,320,310,600,900,5.0,True,False\n")
    s = summarize_frontier(load_frontier(p))
    assert (s.n_points, s.n_frontier, s.n_unproven, s.n_corridors) == (3, 2, 1, 2)
    assert s.fuel_range_t == (560.0, 580.0)  # frontier points only
    assert s.duration_range_h == (259.0, 298.0)


# --- profiles / geometry ---


def test_profile_leg_frame_has_one_row_per_leg_with_absolute_times(scenario):
    profile = speed_profile_result_from_dict(scenario["departures"][0]["committed_profile"])
    df = profile_leg_frame(profile)
    assert len(df) == len(profile.legs)
    assert df["start_hours"].iloc[0] == pytest.approx(0.0)
    # Legs are contiguous: each starts when the previous one ends.
    assert (df["start_hours"].iloc[1:].values == pytest.approx(df["end_hours"].iloc[:-1].values))
    assert df["fuel_tonnes"].sum() == pytest.approx(profile.total_fuel_tonnes)


def test_path_coordinates_returns_lat_lon_per_node(scenario):
    g, path = scenario["graph"], scenario["path"]
    lats, lons = path_coordinates(g, path)
    assert len(lats) == len(lons) == len(path)
    assert lats[0] == pytest.approx(g.nodes[path[0]]["lat"])
    assert lons[-1] == pytest.approx(g.nodes[path[-1]]["lon"])


def test_path_coordinates_names_the_missing_node(scenario):
    with pytest.raises(KeyError, match="ghost"):
        path_coordinates(scenario["graph"], ["ghost"])


# --- compare_remaining: the like-for-like guard ---


def test_compare_remaining_uses_the_matching_tail_not_the_whole_profile(scenario):
    original = speed_profile_result_from_dict(scenario["departures"][0]["committed_profile"])
    new = speed_profile_result_from_dict(scenario["results"][0]["committed_profile"])
    cmp = compare_remaining(original, new)

    n = len(new.legs)
    assert cmp.n_legs == n
    tail = original.legs[-n:]
    assert cmp.old_fuel_tonnes == pytest.approx(sum(l.fuel_tonnes for l in tail))
    # The bug this guards against: the whole-profile total is strictly larger
    # than the tail (some legs were already sailed), so a naive whole-vs-new
    # comparison looks like a big saving that isn't one.
    assert cmp.old_fuel_tonnes < original.total_fuel_tonnes
    assert cmp.fuel_delta_tonnes == pytest.approx(cmp.new_fuel_tonnes - cmp.old_fuel_tonnes)
    assert cmp.transit_delta_hours == pytest.approx(cmp.new_transit_hours - cmp.old_transit_hours)


def test_compare_remaining_refuses_a_non_suffix(scenario):
    original = speed_profile_result_from_dict(scenario["departures"][0]["committed_profile"])
    new = speed_profile_result_from_dict(scenario["results"][0]["committed_profile"])
    broken = new._replace(legs=[new.legs[0]._replace(u="zzz")] + list(new.legs[1:]))
    with pytest.raises(ValueError, match="suffix"):
        compare_remaining(original, broken)


def test_compare_remaining_refuses_more_new_legs_than_old(scenario):
    original = speed_profile_result_from_dict(scenario["departures"][0]["committed_profile"])
    with pytest.raises(ValueError):
        compare_remaining(original._replace(legs=original.legs[:2]), original)


# --- build_voyage_views ---


def test_view_without_results_is_sailing_with_original_as_current(scenario):
    (view,) = build_voyage_views(scenario["departures"], [], scenario["now"])
    assert view.status == "sailing"
    assert view.steps == []
    assert view.current_profile == view.original_profile
    assert 0.0 < view.progress < 1.0
    assert view.legs_remaining + round(view.progress * view.total_legs) == view.total_legs
    assert view.origin == scenario["path"][0] and view.destination == scenario["path"][-1]


def test_view_with_result_makes_the_reoptimized_profile_current(scenario):
    (view,) = build_voyage_views(
        scenario["departures"], scenario["results"], scenario["now"], scenario["weather_events"],
    )
    assert len(view.steps) == 1
    step = view.steps[0]
    assert view.current_profile == step.profile
    assert step.comparison is not None
    # Derived from the profiles, so it matches regardless of what the event's
    # own old_remaining_* fields say.
    original_tail = view.original_profile.legs[-len(step.profile.legs):]
    assert step.comparison.old_fuel_tonnes == pytest.approx(sum(l.fuel_tonnes for l in original_tail))
    assert step.injected is True  # the weather event on that edge was a labelled injection


def test_view_still_correct_for_results_published_before_old_remaining_fields_existed(scenario):
    old_style = copy.deepcopy(scenario["results"])
    for r in old_style:
        r.pop("old_remaining_fuel_tonnes", None)
        r.pop("old_remaining_transit_hours", None)
    (view,) = build_voyage_views(scenario["departures"], old_style, scenario["now"])
    assert view.steps[0].comparison is not None


def test_replayed_duplicate_events_are_collapsed(scenario):
    deps = scenario["departures"] * 2
    results = scenario["results"] * 3
    views = build_voyage_views(deps, results, scenario["now"])
    assert len(views) == 1
    assert len(views[0].steps) == 1


def test_result_for_an_unknown_voyage_is_ignored(scenario):
    stray = copy.deepcopy(scenario["results"][0])
    stray["voyage_id"] = "never-departed"
    (view,) = build_voyage_views(scenario["departures"], [stray], scenario["now"])
    assert view.steps == []


def test_a_voyage_long_past_arrival_is_marked_arrived(scenario):
    late = scenario["now"] + timedelta(hours=10)
    (view,) = build_voyage_views(scenario["departures"], [], late)
    assert view.status == "arrived"
    assert view.legs_remaining == 0
    assert view.progress == pytest.approx(1.0)


def test_newest_departure_comes_first(scenario):
    second = copy.deepcopy(scenario["departures"][0])
    second["voyage_id"] = "demo-test0002"
    second["demo_departed_at"] = "2026-10-09T22:00:00"
    views = build_voyage_views(scenario["departures"] + [second], [], scenario["now"])
    assert [v.voyage_id for v in views] == ["demo-test0002", "demo-test0001"]


def test_injected_flag_defaults_to_false_for_real_events(scenario):
    real_event = dict(scenario["weather_events"][0], injected=False)
    (view,) = build_voyage_views(scenario["departures"], scenario["results"], scenario["now"], [real_event])
    assert view.steps[0].injected is False


def test_demo_clock_note_states_the_scale_and_that_positions_are_simulated():
    note = demo_clock_note()
    assert "10 simulated voyage hours" in note
    assert "not real vessel tracking" in note
