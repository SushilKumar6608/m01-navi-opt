from datetime import datetime, timedelta

import networkx as nx
import numpy as np
import pytest
import xarray as xr

from navi_opt.weather.resistance_model import relative_headwind_component
from navi_opt.weather.weather_field import (
    MS_TO_KNOTS,
    WeatherField,
    make_weather_lookup,
)

LATS = [50.0, 51.0]
LONS = [3.0, 4.0]
TIMES = [np.datetime64("2026-01-01T00:00:00"), np.datetime64("2026-01-01T06:00:00")]


def _wave_dataset(values) -> xr.Dataset:
    """values: dict[(lat, lon, time_idx)] -> wave height, defaults to 1.0 elsewhere."""
    data = np.ones((len(TIMES), len(LATS), len(LONS)))
    for (lat, lon, t_idx), v in values.items():
        data[t_idx, LATS.index(lat), LONS.index(lon)] = v
    return xr.Dataset(
        {"VHM0": (["time", "latitude", "longitude"], data)},
        coords={"time": TIMES, "latitude": LATS, "longitude": LONS},
    )


def _wind_dataset(eastward_values, northward_values) -> xr.Dataset:
    eastward = np.zeros((len(TIMES), len(LATS), len(LONS)))
    northward = np.zeros((len(TIMES), len(LATS), len(LONS)))
    for (lat, lon, t_idx), v in eastward_values.items():
        eastward[t_idx, LATS.index(lat), LONS.index(lon)] = v
    for (lat, lon, t_idx), v in northward_values.items():
        northward[t_idx, LATS.index(lat), LONS.index(lon)] = v
    return xr.Dataset(
        {
            "eastward_wind": (["time", "latitude", "longitude"], eastward),
            "northward_wind": (["time", "latitude", "longitude"], northward),
        },
        coords={"time": TIMES, "latitude": LATS, "longitude": LONS},
    )


def test_sample_returns_exact_wave_height_at_grid_point():
    wave_ds = _wave_dataset({(50.0, 3.0, 0): 2.5})
    wind_ds = _wind_dataset({}, {})
    field = WeatherField(wave_ds, wind_ds)

    wave_h, _, _ = field.sample(50.0, 3.0, datetime(2026, 1, 1, 0, 0))
    assert wave_h == pytest.approx(2.5)


def test_sample_converts_wind_ms_to_knots():
    wave_ds = _wave_dataset({})
    # pure eastward 10 m/s wind at (50.0, 3.0), t=0
    wind_ds = _wind_dataset({(50.0, 3.0, 0): 10.0}, {(50.0, 3.0, 0): 0.0})
    field = WeatherField(wave_ds, wind_ds)

    _, wind_speed_kn, wind_dir_deg = field.sample(50.0, 3.0, datetime(2026, 1, 1, 0, 0))
    assert wind_speed_kn == pytest.approx(10.0 * MS_TO_KNOTS)
    # eastward_wind>0, northward_wind=0 -> blowing toward due east (90 deg),
    # matching wind_uv_to_speed_direction's pyproj.Geod-compatible convention.
    assert wind_dir_deg == pytest.approx(90.0)


def test_sample_picks_nearest_grid_point_and_timestep():
    wave_ds = _wave_dataset({(51.0, 4.0, 1): 9.9})
    wind_ds = _wind_dataset({}, {})
    field = WeatherField(wave_ds, wind_ds)

    # query point closer to (51.0, 4.0) than (50.0, 3.0); query time closer to
    # the second timestep (06:00) than the first (00:00).
    wave_h, _, _ = field.sample(50.9, 3.9, datetime(2026, 1, 1, 5, 0))
    assert wave_h == pytest.approx(9.9)


def _all_nan_wave_dataset() -> xr.Dataset:
    """No valid point anywhere -> WeatherField._build_valid_tree() returns
    (None, None) -> no fallback is possible -> sample() must raise, same as
    before the nearest-valid-point fallback existed."""
    data = np.full((len(TIMES), len(LATS), len(LONS)), float("nan"))
    return xr.Dataset(
        {"VHM0": (["time", "latitude", "longitude"], data)},
        coords={"time": TIMES, "latitude": LATS, "longitude": LONS},
    )


def _all_nan_wind_dataset() -> xr.Dataset:
    data = np.full((len(TIMES), len(LATS), len(LONS)), float("nan"))
    return xr.Dataset(
        {
            "eastward_wind": (["time", "latitude", "longitude"], data),
            "northward_wind": (["time", "latitude", "longitude"], data.copy()),
        },
        coords={"time": TIMES, "latitude": LATS, "longitude": LONS},
    )


def test_sample_raises_on_nan_wave_height_with_no_valid_point_anywhere():
    wave_ds = _all_nan_wave_dataset()
    wind_ds = _wind_dataset({}, {})
    field = WeatherField(wave_ds, wind_ds)

    with pytest.raises(ValueError, match="NaN"):
        field.sample(50.0, 3.0, datetime(2026, 1, 1, 0, 0))


def test_sample_raises_on_nan_wind_with_no_valid_point_anywhere():
    wave_ds = _wave_dataset({})
    wind_ds = _all_nan_wind_dataset()
    field = WeatherField(wave_ds, wind_ds)

    with pytest.raises(ValueError, match="NaN"):
        field.sample(50.0, 3.0, datetime(2026, 1, 1, 0, 0))


# 3x3 grid with non-uniform spacing (so "nearest valid point" is unambiguous,
# unlike the 2x2 LATS/LONS grid above, where a corner NaN would tie two
# neighbors) — used by the nearest-valid-point-fallback tests below.
LATS3 = [50.0, 50.4, 51.0]
LONS3 = [3.0, 3.6, 4.0]


def _wave_dataset_3x3(values) -> xr.Dataset:
    """values: dict[(lat, lon, time_idx)] -> wave height; NaN everywhere else."""
    data = np.full((len(TIMES), len(LATS3), len(LONS3)), float("nan"))
    for (lat, lon, t_idx), v in values.items():
        data[t_idx, LATS3.index(lat), LONS3.index(lon)] = v
    return xr.Dataset(
        {"VHM0": (["time", "latitude", "longitude"], data)},
        coords={"time": TIMES, "latitude": LATS3, "longitude": LONS3},
    )


def _wind_dataset_3x3(eastward_values, northward_values) -> xr.Dataset:
    # Defaults to 0.0 (a fully valid grid), matching _wind_dataset() above —
    # these fixtures exist to test the WAVE fallback in isolation, so wind
    # must not itself be all-NaN (which would make wind sampling raise
    # before the wave-specific assertion is even reached).
    eastward = np.zeros((len(TIMES), len(LATS3), len(LONS3)))
    northward = np.zeros((len(TIMES), len(LATS3), len(LONS3)))
    for (lat, lon, t_idx), v in eastward_values.items():
        eastward[t_idx, LATS3.index(lat), LONS3.index(lon)] = v
    for (lat, lon, t_idx), v in northward_values.items():
        northward[t_idx, LATS3.index(lat), LONS3.index(lon)] = v
    return xr.Dataset(
        {
            "eastward_wind": (["time", "latitude", "longitude"], eastward),
            "northward_wind": (["time", "latitude", "longitude"], northward),
        },
        coords={"time": TIMES, "latitude": LATS3, "longitude": LONS3},
    )


def test_sample_falls_back_to_nearest_valid_point_when_exact_point_is_nan():
    # (50.0, 3.0) is NaN (never set -> stays NaN); nearest valid point is
    # (50.4, 3.0) [0.4 away] vs. (50.0, 3.6) [0.6 away] vs. (51.0, 3.0)
    # [1.0 away] -> (50.4, 3.0) is the unambiguous nearest.
    wave_ds = _wave_dataset_3x3({(50.4, 3.0, 0): 5.5})
    wind_ds = _wind_dataset_3x3({}, {})
    field = WeatherField(wave_ds, wind_ds)

    wave_h, _, _ = field.sample(50.0, 3.0, datetime(2026, 1, 1, 0, 0))
    assert wave_h == pytest.approx(5.5)


def test_sample_fallback_reraises_the_actual_requested_time_not_the_reference_time():
    # The fallback point (50.4, 3.0) is valid at BOTH timesteps but with
    # DIFFERENT values -> proves the fallback re-queries at the real
    # requested time rather than freezing whatever value existed when the
    # nearest-valid-point tree was built (t=0).
    wave_ds = _wave_dataset_3x3({(50.4, 3.0, 0): 5.5, (50.4, 3.0, 1): 9.9})
    wind_ds = _wind_dataset_3x3({}, {})
    field = WeatherField(wave_ds, wind_ds)

    wave_h_t0, _, _ = field.sample(50.0, 3.0, datetime(2026, 1, 1, 0, 0))
    wave_h_t1, _, _ = field.sample(50.0, 3.0, datetime(2026, 1, 1, 6, 0))
    assert wave_h_t0 == pytest.approx(5.5)
    assert wave_h_t1 == pytest.approx(9.9)


def test_sample_raises_when_fallback_point_is_also_nan_at_the_requested_time():
    # Fallback point (50.4, 3.0) is valid at t=0 (so the tree considers it
    # valid) but NaN at t=1 -> querying at t=1 must still raise, not return
    # a stale/wrong value from t=0.
    wave_ds = _wave_dataset_3x3({(50.4, 3.0, 0): 5.5})  # NaN at t=1 (never set)
    wind_ds = _wind_dataset_3x3({}, {})
    field = WeatherField(wave_ds, wind_ds)

    with pytest.raises(ValueError, match="NaN"):
        field.sample(50.0, 3.0, datetime(2026, 1, 1, 6, 0))


def test_sample_fallback_prints_a_note_once_per_location():
    wave_ds = _wave_dataset_3x3({(50.4, 3.0, 0): 5.5, (50.4, 3.0, 1): 5.5})
    wind_ds = _wind_dataset_3x3({}, {})
    field = WeatherField(wave_ds, wind_ds)

    field.sample(50.0, 3.0, datetime(2026, 1, 1, 0, 0))
    assert len(field._warned_fallback_locations) == 1
    field.sample(50.0, 3.0, datetime(2026, 1, 1, 6, 0))  # same fallback location again
    assert len(field._warned_fallback_locations) == 1  # not warned twice


def test_weather_field_rejects_missing_variable():
    wave_ds = _wave_dataset({}).rename({"VHM0": "not_the_right_name"})
    wind_ds = _wind_dataset({}, {})
    with pytest.raises(KeyError, match="not_the_right_name|VHM0"):
        WeatherField(wave_ds, wind_ds)


def test_weather_field_rejects_missing_coordinate():
    wave_ds = _wave_dataset({}).rename({"latitude": "lat"})
    wind_ds = _wind_dataset({}, {})
    with pytest.raises(KeyError, match="latitude"):
        WeatherField(wave_ds, wind_ds)


def _digraph_with_one_eastbound_edge() -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_node("A", lat=50.0, lon=3.0)
    g.add_node("B", lat=50.0, lon=3.5)
    g.add_edge("A", "B", dist_nm=20.0, bearing_deg=90.0)  # due east
    return g


def test_make_weather_lookup_samples_at_departure_node_and_time():
    # wave height distinguishable at t=0 vs t=1 so we can confirm the lookup
    # used the right absolute time (voyage_start_time + arrival_time_hours).
    wave_ds = _wave_dataset({(50.0, 3.0, 0): 1.0, (50.0, 3.0, 1): 7.0})
    wind_ds = _wind_dataset({}, {})
    field = WeatherField(wave_ds, wind_ds)
    graph = _digraph_with_one_eastbound_edge()

    voyage_start = datetime(2026, 1, 1, 0, 0)
    lookup = make_weather_lookup(graph, field, voyage_start)

    wave_h_at_start, _ = lookup("A", "B", 0.0)
    assert wave_h_at_start == pytest.approx(1.0)

    wave_h_later, _ = lookup("A", "B", 6.0)  # start + 6h lands on the t=1 (06:00) slice
    assert wave_h_later == pytest.approx(7.0)


def test_make_weather_lookup_computes_headwind_matching_resistance_model_directly():
    # Wind blowing due east (90 deg) at 20 knots; vessel travels due east (bearing 90).
    # A following wind is a pure tailwind -> headwind component must be exactly 0,
    # per relative_headwind_component()'s documented tailwind-clipped-to-zero behavior.
    wave_ds = _wave_dataset({})
    eastward_ms = 20.0 / MS_TO_KNOTS
    wind_ds = _wind_dataset({(50.0, 3.0, 0): eastward_ms}, {})
    field = WeatherField(wave_ds, wind_ds)
    graph = _digraph_with_one_eastbound_edge()

    lookup = make_weather_lookup(graph, field, datetime(2026, 1, 1, 0, 0))
    _, headwind_kn = lookup("A", "B", 0.0)
    assert headwind_kn == pytest.approx(0.0, abs=1e-6)


def test_make_weather_lookup_headwind_matches_independent_computation():
    # Wind blowing due WEST (270 deg) at 15 knots opposing an eastbound (90 deg)
    # vessel is a pure headwind -> headwind component must equal the wind speed.
    wave_ds = _wave_dataset({})
    westward_ms = -15.0 / MS_TO_KNOTS  # eastward component negative = blowing west
    wind_ds = _wind_dataset({(50.0, 3.0, 0): westward_ms}, {})
    field = WeatherField(wave_ds, wind_ds)
    graph = _digraph_with_one_eastbound_edge()

    lookup = make_weather_lookup(graph, field, datetime(2026, 1, 1, 0, 0))
    _, headwind_kn = lookup("A", "B", 0.0)

    expected = relative_headwind_component(
        vessel_heading_deg=90.0, wind_blowing_toward_deg=270.0, wind_speed=15.0
    )
    assert headwind_kn == pytest.approx(expected, abs=1e-6)
    assert headwind_kn == pytest.approx(15.0, abs=1e-6)


def test_make_weather_lookup_raises_on_missing_edge():
    wave_ds = _wave_dataset({})
    wind_ds = _wind_dataset({}, {})
    field = WeatherField(wave_ds, wind_ds)
    graph = _digraph_with_one_eastbound_edge()
    lookup = make_weather_lookup(graph, field, datetime(2026, 1, 1, 0, 0))

    with pytest.raises(ValueError, match="No edge"):
        lookup("B", "A", 0.0)  # only A->B exists, not B->A


def test_make_weather_lookup_raises_on_missing_lat_lon():
    wave_ds = _wave_dataset({})
    wind_ds = _wind_dataset({}, {})
    field = WeatherField(wave_ds, wind_ds)

    g = nx.DiGraph()
    g.add_node("A")  # no lat/lon set
    g.add_node("B")
    g.add_edge("A", "B", dist_nm=10.0, bearing_deg=90.0)

    lookup = make_weather_lookup(g, field, datetime(2026, 1, 1, 0, 0))
    with pytest.raises(KeyError, match="lat/lon"):
        lookup("A", "B", 0.0)
