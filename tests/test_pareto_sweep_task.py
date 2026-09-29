import csv
from datetime import datetime, timedelta
from pathlib import Path

import networkx as nx
import pytest

import navi_opt.orchestration.pareto_sweep_task as task_mod
from navi_opt.orchestration.pareto_sweep_task import SweepConfig, SweepResult, run_pareto_sweep_task

BBOX = dict(west=-15.0, south=30.0, east=40.0, north=56.0)
VOYAGE_START = datetime(2024, 6, 1, 0, 0)
FETCH_WINDOW = timedelta(days=16)


def _two_leg_graph(dist_nm_each: float) -> nx.DiGraph:
    # Same shape as test_pareto.py / test_speed_profile.py's fixture — a
    # single simple path A->B->C, deliberately trivial since this module's
    # job is the wiring/I/O around the sweep, not the solver itself (that's
    # already covered directly in test_pareto.py).
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=0.0, lon=2.0)
    g.add_node("C", lat=0.0, lon=4.0)
    g.add_edge("A", "B", dist_nm=dist_nm_each)
    g.add_edge("B", "C", dist_nm=dist_nm_each)
    return g


def _base_config(tmp_path: Path, **overrides) -> SweepConfig:
    digraph_path = tmp_path / "digraph.pkl"
    anchors_path = tmp_path / "anchors.parquet"
    digraph_path.touch()
    anchors_path.touch()
    defaults = dict(
        origin_port="origin",
        destination_port="destination",
        digraph_path=digraph_path,
        anchors_path=anchors_path,
        output_csv=tmp_path / "out.csv",
        voyage_start=VOYAGE_START,
        fetch_window=FETCH_WINDOW,
        bbox=BBOX,
        k_corridors=1,  # the fixture graph has exactly one simple path
    )
    defaults.update(overrides)
    return SweepConfig(**defaults)


def _patch_pipeline(monkeypatch, graph: nx.DiGraph, anchors: dict, weather_lookup=None):
    """Monkeypatches the four external calls `run_pareto_sweep_task` makes
    (graph/anchors loading, CMEMS field fetch, weather-lookup construction)
    so a test never touches disk-cache files or the network — only the
    wiring and I/O this module actually adds is under test here; the
    solver/sweep/frontier logic itself is covered directly in
    test_pareto.py."""
    monkeypatch.setattr(task_mod, "load_graph", lambda path: graph)
    monkeypatch.setattr(task_mod, "load_anchors", lambda path: anchors)
    monkeypatch.setattr(task_mod, "open_weather_field_remote", lambda **kwargs: object())

    def _calm(u, v, t):
        return 0.0, 0.0

    monkeypatch.setattr(
        task_mod, "make_weather_lookup", lambda digraph, field, start: weather_lookup or _calm
    )


# --- prerequisite / input validation: raises rather than prints-and-returns ---


def test_raises_filenotfound_when_cache_missing(tmp_path):
    config = SweepConfig(
        origin_port="origin",
        destination_port="destination",
        digraph_path=tmp_path / "missing.pkl",
        anchors_path=tmp_path / "missing.parquet",
        output_csv=tmp_path / "out.csv",
        voyage_start=VOYAGE_START,
        fetch_window=FETCH_WINDOW,
        bbox=BBOX,
    )
    with pytest.raises(FileNotFoundError):
        run_pareto_sweep_task(config)


def test_raises_valueerror_when_port_not_in_anchors(tmp_path, monkeypatch):
    graph = _two_leg_graph(120.0)
    _patch_pipeline(monkeypatch, graph, anchors={"origin": "A"})  # "destination" missing
    config = _base_config(tmp_path)
    with pytest.raises(ValueError, match="destination"):
        run_pareto_sweep_task(config)


def test_raises_valueerror_when_no_path_between_ports(tmp_path, monkeypatch):
    graph = _two_leg_graph(120.0)
    graph.add_node("Z", lat=9.0, lon=9.0)  # unreachable from A
    _patch_pipeline(monkeypatch, graph, anchors={"origin": "A", "destination": "Z"})
    config = _base_config(tmp_path)
    with pytest.raises(ValueError, match="No path found"):
        run_pareto_sweep_task(config)


# --- happy path: the actual wiring/I/O this module adds ---


def test_writes_csv_with_severe_weather_column_and_matching_row_count(tmp_path, monkeypatch):
    graph = _two_leg_graph(150.0)
    _patch_pipeline(monkeypatch, graph, anchors={"origin": "A", "destination": "C"})
    config = _base_config(tmp_path)

    result = run_pareto_sweep_task(config)

    assert isinstance(result, SweepResult)
    assert result.output_csv == config.output_csv
    assert result.output_csv.exists()

    with open(result.output_csv, newline="") as f:
        rows = list(csv.reader(f))
    header, data_rows = rows[0], rows[1:]
    assert header == [
        "corridor_index", "epsilon_hours", "transit_hours", "fuel_tonnes",
        "weather_risk_m_hours", f"severe_weather_hours_ge_{config.severe_weather_threshold_m:g}m",
        "success", "on_frontier",
    ]
    # Every solved point is a CSV row — the CSV isn't a subset or a
    # re-derived summary, it's the full swept set with frontier flagged.
    assert len(data_rows) == len(result.all_points)
    assert len(result.frontier) > 0
    assert len(result.frontier) <= len(result.all_points)


def test_calm_water_sweep_has_zero_severe_weather_hours_throughout(tmp_path, monkeypatch):
    # A direct check that the wiring passes the real weather_lookup through
    # correctly end to end: an all-calm lookup must produce zero severe-
    # weather-hours on every point, not just a plausible-looking non-error.
    graph = _two_leg_graph(150.0)
    _patch_pipeline(monkeypatch, graph, anchors={"origin": "A", "destination": "C"})
    config = _base_config(tmp_path)

    result = run_pareto_sweep_task(config)
    assert all(p.severe_weather_hours == pytest.approx(0.0) for p in result.all_points)


def test_on_corridor_start_called_once_per_corridor(tmp_path, monkeypatch):
    graph = _two_leg_graph(150.0)
    _patch_pipeline(monkeypatch, graph, anchors={"origin": "A", "destination": "C"})
    config = _base_config(tmp_path)

    seen = []
    run_pareto_sweep_task(config, on_corridor_start=lambda idx, total: seen.append((idx, total)))
    assert seen == [(0, 1)]  # k_corridors=1 in _base_config


def test_on_progress_forwarded_through_to_the_sweep(tmp_path, monkeypatch):
    graph = _two_leg_graph(150.0)
    _patch_pipeline(monkeypatch, graph, anchors={"origin": "A", "destination": "C"})
    config = _base_config(tmp_path, sweep_n_points=3, sweep_max_margin=1.5)

    seen = []
    run_pareto_sweep_task(config, on_progress=lambda idx, eps, point, elapsed: seen.append(idx))
    assert seen == list(range(3))  # called once per epsilon, in order


def test_respects_a_custom_severe_weather_threshold(tmp_path, monkeypatch):
    graph = _two_leg_graph(150.0)

    def rough_second_leg(u, v, t):
        if (u, v) == ("B", "C"):
            return 3.0, 20.0
        return 0.0, 0.0

    _patch_pipeline(
        monkeypatch, graph, anchors={"origin": "A", "destination": "C"},
        weather_lookup=rough_second_leg,
    )
    config = _base_config(tmp_path, severe_weather_threshold_m=2.0)

    result = run_pareto_sweep_task(config)
    with open(result.output_csv, newline="") as f:
        header = next(csv.reader(f))
    assert "severe_weather_hours_ge_2m" in header
    assert any(p.severe_weather_hours > 0.0 for p in result.all_points)
