import pandas as pd
import pytest

from navi_opt.dashboard.charts import (
    MAX_CATEGORICAL,
    PALETTES,
    corridor_map,
    palette,
    pareto_scatter,
    speed_profile_chart,
)
from navi_opt.dashboard.data import profile_leg_frame
from navi_opt.streaming.serialization import speed_profile_result_from_dict
from tests.dashboard_fixtures import make_scenario


def _frontier_df(n_corridors=3, with_severe=True) -> pd.DataFrame:
    rows = []
    for c in range(1, n_corridors + 1):
        for i, (dur, fuel, risk) in enumerate([(260, 580, 800), (300, 560, 790), (340, 545, 760)]):
            rows.append({
                "corridor": c, "epsilon_hours": dur + 1.0, "transit_hours": dur + c, "fuel_tonnes": fuel + c,
                "weather_risk_m_hours": risk, "severe_weather_hours": 2.0 * i if with_severe else float("nan"),
                "success": i != 2, "on_frontier": i < 2,
            })
    return pd.DataFrame(rows)


def _names(fig):
    return [t.name for t in fig.data if t.showlegend is not False]


# --- palettes ---


def test_both_palettes_define_the_same_roles_and_three_series():
    assert set(PALETTES["light"]) == set(PALETTES["dark"])
    for mode in ("light", "dark"):
        assert len(PALETTES[mode]["series"]) == MAX_CATEGORICAL
        assert len(set(PALETTES[mode]["series"])) == MAX_CATEGORICAL


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        palette("sepia")


# --- pareto scatter ---


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_pareto_scatter_has_a_legend_entry_per_corridor_plus_a_shape_key(mode):
    fig = pareto_scatter(_frontier_df(3), "transit_hours", "fuel_tonnes", "Duration (h)", "Fuel (t)", mode)
    names = _names(fig)
    assert {"Corridor 1", "Corridor 2", "Corridor 3"} <= set(names)
    assert "Frontier (non-dominated)" in names and "Dominated" in names


def test_pareto_scatter_encodes_frontier_by_shape_not_colour_alone():
    fig = pareto_scatter(_frontier_df(1), "transit_hours", "fuel_tonnes", "d", "f")
    symbols = {t.marker.symbol for t in fig.data if t.x is not None and t.x[0] is not None}
    assert {"diamond", "circle"} <= symbols


def test_pareto_scatter_uses_the_validated_series_colours_in_fixed_order():
    fig = pareto_scatter(_frontier_df(3), "transit_hours", "fuel_tonnes", "d", "f", "dark")
    colour_by_name = {t.name: t.marker.color for t in fig.data if t.name.startswith("Corridor")}
    assert colour_by_name["Corridor 1"] == PALETTES["dark"]["series"][0]
    assert colour_by_name["Corridor 2"] == PALETTES["dark"]["series"][1]
    assert colour_by_name["Corridor 3"] == PALETTES["dark"]["series"][2]


def test_pareto_scatter_filtering_does_not_repaint_the_surviving_corridors():
    full = _frontier_df(3)
    only_frontier_of_corridor_3 = full[(full["corridor"] == 3) & full["on_frontier"]]
    order = sorted(full["corridor"].unique())
    fig = pareto_scatter(only_frontier_of_corridor_3, "transit_hours", "fuel_tonnes", "d", "f",
                         "light", corridor_order=order)
    colour = {t.name: t.marker.color for t in fig.data if t.name.startswith("Corridor")}
    # Corridor 3 keeps its own (third) colour even though it is now the only one drawn.
    assert colour["Corridor 3"] == PALETTES["light"]["series"][2]
    # Without the stable order it would have been repainted as "first":
    naive = pareto_scatter(only_frontier_of_corridor_3, "transit_hours", "fuel_tonnes", "d", "f", "light")
    assert {t.name: t.marker.color for t in naive.data if t.name.startswith("Corridor")}[
        "Corridor 3"] == PALETTES["light"]["series"][0]


def test_pareto_scatter_folds_a_fourth_corridor_into_gray_instead_of_a_new_hue():
    fig = pareto_scatter(_frontier_df(5), "transit_hours", "fuel_tonnes", "d", "f", "light")
    names = _names(fig)
    assert "Other corridors" in names
    assert "Corridor 4" not in names and "Corridor 5" not in names
    other = [t for t in fig.data if t.name == "Other corridors"]
    assert {t.marker.color for t in other} == {PALETTES["light"]["muted"]}


def test_pareto_scatter_hover_flags_unproven_solves_and_tolerates_missing_severe():
    fig = pareto_scatter(_frontier_df(1, with_severe=False), "transit_hours", "fuel_tonnes", "d", "f")
    hover = " ".join(" ".join(t.text) for t in fig.data if t.text is not None and len(t.text) and t.text[0])
    assert "unproven-optimal" in hover
    assert "Severe-weather time" not in hover  # NaN column is omitted, not printed as 0


def test_pareto_scatter_with_no_data_returns_a_figure_with_a_message():
    fig = pareto_scatter(_frontier_df(1).iloc[0:0], "transit_hours", "fuel_tonnes", "d", "f")
    assert len(fig.data) == 0
    assert "No sweep points" in fig.layout.annotations[0].text


# --- corridor map ---


def test_corridor_map_splits_sailed_and_remaining_at_the_current_index():
    lats, lons = [50.0, 51.0, 52.0, 53.0], [1.0, 2.0, 3.0, 4.0]
    fig = corridor_map(lats, lons, current_index=2, mode="light")
    by_name = {t.name: t for t in fig.data}
    assert list(by_name["Sailed"].lat) == [50.0, 51.0, 52.0]
    assert list(by_name["Remaining route"].lat) == [52.0, 53.0]
    assert list(by_name["Simulated position"].lat) == [52.0]


def test_corridor_map_arrived_voyage_has_no_remaining_trace():
    fig = corridor_map([50.0, 51.0], [1.0, 2.0], current_index=1)
    assert "Remaining route" not in {t.name for t in fig.data}


def test_corridor_map_clamps_an_out_of_range_index():
    fig = corridor_map([50.0, 51.0], [1.0, 2.0], current_index=99)
    assert list(next(t for t in fig.data if t.name == "Simulated position").lat) == [51.0]


def test_corridor_map_draws_the_trigger_edge_only_when_given():
    base = corridor_map([50.0, 51.0, 52.0], [1.0, 2.0, 3.0], 0)
    assert "Weather-event edge" not in {t.name for t in base.data}
    withedge = corridor_map([50.0, 51.0, 52.0], [1.0, 2.0, 3.0], 0, trigger_edge=((51.0, 2.0), (52.0, 3.0)))
    edge = next(t for t in withedge.data if t.name == "Weather-event edge")
    assert list(edge.lat) == [51.0, 52.0]


def test_corridor_map_with_no_route_returns_a_message_not_an_error():
    fig = corridor_map([], [], 0)
    assert len(fig.data) == 0


# --- speed profile ---


@pytest.fixture(scope="module")
def frames():
    s = make_scenario()
    original = profile_leg_frame(speed_profile_result_from_dict(s["departures"][0]["committed_profile"]))
    current = profile_leg_frame(speed_profile_result_from_dict(s["results"][0]["committed_profile"]))
    return original, current, s["departures"][0]["deadline_hours"]


def test_speed_profile_emphasises_current_over_original(frames):
    original, current, deadline = frames
    fig = speed_profile_chart(original, current, 80.0, deadline, "light")
    by_name = {t.name: t for t in fig.data}
    assert by_name["Original plan"].line.color == PALETTES["light"]["muted"]
    assert by_name["Current plan"].line.color == PALETTES["light"]["series"][0]
    assert by_name["Current plan"].line.shape == "hv"


def test_speed_profile_extends_the_last_step_to_the_final_leg_end(frames):
    original, _, deadline = frames
    fig = speed_profile_chart(original, original, 0.0, deadline)
    trace = fig.data[0]
    assert trace.x[-1] == pytest.approx(original["end_hours"].iloc[-1])
    assert trace.y[-1] == pytest.approx(original["speed_knots"].iloc[-1])


def test_speed_profile_draws_one_series_when_nothing_was_reoptimized(frames):
    original, _, deadline = frames
    fig = speed_profile_chart(original, original, 10.0, deadline)
    assert [t.name for t in fig.data] == ["Planned speed"]


def test_speed_profile_marks_now_and_deadline(frames):
    original, current, deadline = frames
    fig = speed_profile_chart(original, current, 80.0, deadline)
    xs = sorted(s.x0 for s in fig.layout.shapes)
    assert xs == [pytest.approx(80.0), pytest.approx(deadline)]


def test_speed_profile_range_follows_the_plan_not_a_long_finished_clock(frames):
    original, current, deadline = frames
    fig = speed_profile_chart(original, current, 7573.0, deadline)
    assert fig.layout.xaxis.range[1] < 2 * deadline  # not stretched to the 7573 h clock
    assert len(fig.layout.shapes) == 1  # only the deadline line; "now" is off the chart
    assert "Voyage complete" in fig.layout.annotations[-1].text
