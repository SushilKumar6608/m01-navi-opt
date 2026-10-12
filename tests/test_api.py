"""API tests: FastAPI TestClient with a fake Kafka reader and a fixture graph,
so no broker, CMEMS or network is involved. They prove the API logic, not the
real broker connection (see findings.md)."""
import copy
from datetime import datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")  # required by fastapi.testclient
from fastapi.testclient import TestClient  # noqa: E402

from navi_opt.api.app import create_app  # noqa: E402
from navi_opt.api.service import EventStore  # noqa: E402
from navi_opt.dashboard import kafka_reader as kr  # noqa: E402
from tests.dashboard_fixtures import make_scenario  # noqa: E402

NEW_CSV = (
    "corridor_index,epsilon_hours,transit_hours,fuel_tonnes,weather_risk_m_hours,"
    "severe_weather_hours_ge_4.5m,success,on_frontier\n"
    "0,260,259,580,800,3.0,True,True\n0,300,298,560,790,1.0,True,True\n"
    "1,300,299,590,820,5.0,True,False\n2,320,310,600,900,0.0,False,False\n"
)
OLD_CSV = "corridor_index,epsilon_hours,transit_hours,fuel_tonnes,weather_risk_m_hours,success,on_frontier\n0,260,259,580,800,True,True\n"


@pytest.fixture(scope="module")
def scenario():
    return make_scenario()


def fake_reader(scenario, reachable=True, with_voyage=True):
    data = {
        kr.TOPIC_VOYAGE_EVENTS: copy.deepcopy(scenario["departures"]) if with_voyage else [],
        kr.TOPIC_REOPTIMIZATION_RESULTS: copy.deepcopy(scenario["results"]) if with_voyage else [],
        kr.TOPIC_WEATHER_UPDATES: copy.deepcopy(scenario["weather_events"]) if with_voyage else [],
    }

    def read(bootstrap, topic):
        if not reachable:
            return [], f"{kr.UNREACHABLE_PREFIX} at {bootstrap}: test"
        return data[topic], None
    return read


def client(tmp_path, scenario, graph=True, **kw):
    if graph:
        from navi_opt.grid.graph_builder import save_graph
        save_graph(scenario["graph"], tmp_path / "ocean_digraph_rotterdam_ceyhan.pkl")
    reader = kw.pop("read_topic", None) or fake_reader(scenario)
    return TestClient(create_app(data_dir=tmp_path, bootstrap="test:9092", read_topic=reader,
                                 now=lambda: scenario["now"], ttl_seconds=0.0, **kw))


VID = "demo-test0001"


# --- health / frontier ---


def test_health(tmp_path, scenario):
    assert client(tmp_path, scenario).get("/health").json() == {"status": "ok"}


def test_frontier_returns_points_and_summary_of_the_whole_file(tmp_path, scenario):
    (tmp_path / "pareto_frontier.csv").write_text(NEW_CSV)
    body = client(tmp_path, scenario).get("/frontier?frontier_only=true").json()
    assert body["severe_threshold_m"] == 4.5
    assert len(body["points"]) == 2 and all(p["on_frontier"] for p in body["points"])
    assert body["summary"]["n_points"] == 4  # filters don't change the whole-file summary
    assert body["summary"]["n_unproven"] == 1


def test_frontier_corridor_filter_is_one_based(tmp_path, scenario):
    (tmp_path / "pareto_frontier.csv").write_text(NEW_CSV)
    pts = client(tmp_path, scenario).get("/frontier?corridor=3").json()["points"]
    assert [p["corridor"] for p in pts] == [3] and pts[0]["success"] is False


def test_frontier_old_csv_gives_null_severe_not_zero(tmp_path, scenario):
    (tmp_path / "pareto_frontier.csv").write_text(OLD_CSV)
    body = client(tmp_path, scenario).get("/frontier").json()
    assert body["points"][0]["severe_weather_hours"] is None
    assert body["severe_threshold_m"] is None


def test_frontier_missing_file_is_404_with_the_fix(tmp_path, scenario):
    r = client(tmp_path, scenario).get("/frontier")
    assert r.status_code == 404 and "run_pareto_sweep" in r.json()["detail"]


def test_frontier_rejects_path_traversal(tmp_path, scenario):
    assert client(tmp_path, scenario).get("/frontier", params={"file": "../secret.csv"}).status_code == 400


def test_frontier_bad_columns_is_422(tmp_path, scenario):
    (tmp_path / "pareto_frontier.csv").write_text("corridor_index,fuel_tonnes\n0,1.0\n")
    assert client(tmp_path, scenario).get("/frontier").status_code == 422


# --- voyages ---


def test_voyages_lists_the_voyage_with_simulated_position_flag(tmp_path, scenario):
    body = client(tmp_path, scenario).get("/voyages").json()
    (v,) = body["voyages"]
    assert v["voyage_id"] == VID and v["status"] == "sailing"
    assert v["positions_simulated"] is True
    assert v["reoptimizations"] == 1 and v["legs_sailed"] + 0 < v["total_legs"]


def test_voyages_status_filter(tmp_path, scenario):
    c = client(tmp_path, scenario)
    assert len(c.get("/voyages?status=sailing").json()["voyages"]) == 1
    assert c.get("/voyages?status=arrived").json()["voyages"] == []
    assert c.get("/voyages?status=bogus").status_code == 422


def test_voyage_detail_comparison_is_like_for_like_and_flags_injection(tmp_path, scenario):
    d = client(tmp_path, scenario).get(f"/voyages/{VID}").json()
    (step,) = d["steps"]
    cmp = step["comparison"]
    assert cmp["basis"] == "remaining_legs"
    assert cmp["n_legs"] == step["legs_resolved"]
    # the old figure is the matching tail, so strictly less than the whole original plan
    assert cmp["old_fuel_tonnes"] < d["original_total_fuel_tonnes"]
    assert cmp["fuel_delta_tonnes"] == pytest.approx(cmp["new_fuel_tonnes"] - cmp["old_fuel_tonnes"])
    assert step["injected"] is True
    assert "not real vessel tracking" in d["demo_clock_note"]


def test_unknown_voyage_is_404(tmp_path, scenario):
    assert client(tmp_path, scenario).get("/voyages/nope").status_code == 404


def test_profile_original_vs_current(tmp_path, scenario):
    c = client(tmp_path, scenario)
    orig = c.get(f"/voyages/{VID}/profile?which=original").json()["legs"]
    cur = c.get(f"/voyages/{VID}/profile").json()["legs"]
    assert len(orig) == 24 and 0 < len(cur) < len(orig)
    assert orig[0]["start_hours"] == pytest.approx(0.0)
    assert c.get(f"/voyages/{VID}/profile?which=nope").status_code == 422


def test_route_returns_coordinates_current_index_and_trigger_edge(tmp_path, scenario):
    r = client(tmp_path, scenario).get(f"/voyages/{VID}/route").json()
    assert len(r["lats"]) == len(r["lons"]) == 25
    assert 0 < r["current_index"] < 24
    assert r["trigger_edge"] is not None and r["positions_simulated"] is True


def test_route_without_a_graph_cache_is_503_with_the_fix(tmp_path, scenario):
    r = client(tmp_path, scenario, graph=False).get(f"/voyages/{VID}/route")
    assert r.status_code == 503 and "build_real_ocean_graph" in r.json()["detail"]


def test_route_with_a_mismatched_graph_is_409(tmp_path, scenario):
    import networkx as nx
    r = client(tmp_path, scenario, graph_loader=lambda p: nx.DiGraph()).get(f"/voyages/{VID}/route")
    assert r.status_code == 409


# --- kafka states ---


def test_kafka_unreachable_is_503_not_an_empty_list(tmp_path, scenario):
    c = client(tmp_path, scenario, read_topic=fake_reader(scenario, reachable=False))
    for url in ("/voyages", f"/voyages/{VID}", "/weather-events"):
        r = c.get(url)
        assert r.status_code == 503 and "Cannot reach Kafka" in r.json()["detail"]
    assert c.get("/health").status_code == 200  # liveness doesn't depend on the broker


def test_no_voyages_yet_is_an_empty_list_not_an_error(tmp_path, scenario):
    c = client(tmp_path, scenario, read_topic=fake_reader(scenario, with_voyage=False))
    assert c.get("/voyages").json()["voyages"] == []


def test_partial_read_notes_are_surfaced(tmp_path, scenario):
    base = fake_reader(scenario)

    def noisy(bootstrap, topic):
        events, _ = base(bootstrap, topic)
        return events, "Skipped 2 malformed message(s) on 'x'." if topic == kr.TOPIC_WEATHER_UPDATES else None
    body = client(tmp_path, scenario, read_topic=noisy).get("/voyages").json()
    assert body["kafka_notes"] == ["Skipped 2 malformed message(s) on 'x'."]


def test_weather_events_newest_first_with_injected_label(tmp_path, scenario):
    base = fake_reader(scenario)
    older = dict(scenario["weather_events"][0], sampled_at="2000-01-01T00:00:00", injected=False)

    def two(bootstrap, topic):
        events, note = base(bootstrap, topic)
        return (events + [older] if topic == kr.TOPIC_WEATHER_UPDATES else events), note
    evs = client(tmp_path, scenario, read_topic=two).get("/weather-events?limit=5").json()["events"]
    assert len(evs) == 2 and evs[0]["injected"] is True and evs[1]["injected"] is False


# --- EventStore cache ---


def test_event_store_caches_within_ttl_and_refreshes_after():
    calls, t = [], [0.0]

    def read(bootstrap, topic):
        calls.append(topic)
        return [], None
    store = EventStore(read, "b", ttl_seconds=5.0, clock=lambda: t[0])
    store.get("a"); store.get("a"); t[0] = 4.9; store.get("a")
    assert calls == ["a"]
    t[0] = 5.1; store.get("a"); store.get("b")
    assert calls == ["a", "a", "b"]


def test_detail_does_not_present_a_partial_plan_as_a_whole_voyage_total(tmp_path, scenario):
    """Regression for a flaw the first real-data check exposed: `current_total_*` (remaining legs only)
    sat beside `original_total_*` (whole voyage) and read like a 36% saving."""
    c = client(tmp_path, scenario)
    d = c.get(f"/voyages/{VID}").json()
    assert not any(k.startswith("current_total") for k in d)
    assert d["latest_plan_is_partial"] is True
    assert d["latest_plan_legs"] < d["total_legs"]
    assert d["latest_plan_fuel_tonnes"] < d["original_total_fuel_tonnes"]  # the trap: smaller only because partial
    # ...and a voyage that was never reoptimized IS a whole-voyage plan:
    c2 = client(tmp_path, scenario, read_topic=lambda b, t: (
        (copy.deepcopy(scenario["departures"]), None) if t == kr.TOPIC_VOYAGE_EVENTS else ([], None)))
    d2 = c2.get(f"/voyages/{VID}").json()
    assert d2["latest_plan_is_partial"] is False and d2["latest_plan_legs"] == d2["total_legs"]
