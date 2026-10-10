"""Smoke tests for the Streamlit app: it must render each state without an
exception. The Kafka reader is replaced with a fake (no broker here), so these
prove the page logic, not the broker connection."""
import copy
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from navi_opt.dashboard import kafka_reader  # noqa: E402
from navi_opt.grid.graph_builder import save_graph  # noqa: E402
from tests.dashboard_fixtures import make_scenario  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "navi_opt" / "dashboard" / "app.py"
CSV = (
    "corridor_index,epsilon_hours,transit_hours,fuel_tonnes,weather_risk_m_hours,"
    "severe_weather_hours_ge_4.5m,success,on_frontier\n"
    "0,260,259,580,800,3.0,True,True\n0,300,298,560,790,1.0,True,True\n"
    "1,300,299,590,820,5.0,True,False\n2,320,310,600,900,0.0,False,False\n"
)


@pytest.fixture(scope="module")
def scenario():
    return make_scenario()


@pytest.fixture
def data_dir(tmp_path, scenario, monkeypatch):
    (tmp_path / "pareto_frontier.csv").write_text(CSV)
    save_graph(scenario["graph"], tmp_path / "ocean_digraph_rotterdam_ceyhan.pkl")
    monkeypatch.setenv("NAVI_DATA_DIR", str(tmp_path))
    return tmp_path


def _fake_reader(scenario, reachable=True, with_voyage=True):
    shift = datetime.utcnow() - scenario["now"]
    deps = copy.deepcopy(scenario["departures"]) if with_voyage else []
    for d in deps:  # re-anchor the demo clock to the real "now" so progress is mid-voyage
        d["demo_departed_at"] = (datetime.fromisoformat(d["demo_departed_at"]) + shift).isoformat()
    data = {
        kafka_reader.TOPIC_VOYAGE_EVENTS: deps,
        kafka_reader.TOPIC_REOPTIMIZATION_RESULTS: copy.deepcopy(scenario["results"]) if with_voyage else [],
        kafka_reader.TOPIC_WEATHER_UPDATES: copy.deepcopy(scenario["weather_events"]) if with_voyage else [],
    }

    def read_topic(bootstrap, topic, *a, **k):
        if not reachable:
            return [], f"Cannot reach Kafka at {bootstrap}: test"
        return data[topic], None
    return read_topic


def _run(monkeypatch, reader):
    import streamlit as st
    st.cache_data.clear()
    monkeypatch.setattr(kafka_reader, "read_topic", reader)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    return at


def test_app_renders_with_a_voyage_and_a_sweep(data_dir, scenario, monkeypatch):
    at = _run(monkeypatch, _fake_reader(scenario))
    assert not at.exception, [e.value for e in at.exception]
    labels = [m.label for m in at.metric]
    assert any("Fuel" in l for l in labels)


def test_app_renders_when_kafka_is_unreachable(data_dir, scenario, monkeypatch):
    at = _run(monkeypatch, _fake_reader(scenario, reachable=False))
    assert not at.exception, [e.value for e in at.exception]
    assert any("Cannot reach Kafka" in w.value for w in at.warning)


def test_app_renders_with_no_voyages(data_dir, scenario, monkeypatch):
    at = _run(monkeypatch, _fake_reader(scenario, with_voyage=False))
    assert not at.exception, [e.value for e in at.exception]


def test_app_renders_when_sweep_csv_and_graph_are_missing(tmp_path, scenario, monkeypatch):
    monkeypatch.setenv("NAVI_DATA_DIR", str(tmp_path))
    at = _run(monkeypatch, _fake_reader(scenario))
    assert not at.exception, [e.value for e in at.exception]
