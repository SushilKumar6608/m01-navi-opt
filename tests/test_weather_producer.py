from datetime import datetime, timedelta

import networkx as nx
import pytest

import navi_opt.streaming.weather_producer as producer_mod
from navi_opt.streaming.weather_producer import (
    WAVE_HEIGHT_CHANGE_THRESHOLD_M,
    diff_samples,
    poll_once,
    sample_all_edges,
)

SAMPLED_AT = datetime(2026, 1, 1, 12, 0)


def _two_edge_graph() -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_node("A", lat=0.0, lon=0.0)
    g.add_node("B", lat=0.0, lon=1.0)
    g.add_node("C", lat=0.0, lon=2.0)
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    return g


# --- sample_all_edges: pure sampling ---


def test_sample_all_edges_covers_every_edge():
    g = _two_edge_graph()

    def lookup(u, v, t):
        return (1.0 if (u, v) == ("A", "B") else 2.0), 0.0

    sample = sample_all_edges(g, lookup)
    assert sample == {("A", "B"): 1.0, ("B", "C"): 2.0}


def test_sample_all_edges_uses_the_given_elapsed_hours():
    g = _two_edge_graph()
    seen_times = []

    def lookup(u, v, t):
        seen_times.append(t)
        return 0.0, 0.0

    sample_all_edges(g, lookup, at_elapsed_hours=42.0)
    assert all(t == 42.0 for t in seen_times)


# --- diff_samples: the actual "did anything change enough" logic ---


def test_diff_samples_returns_nothing_on_first_poll():
    current = {("A", "B"): 3.0}
    assert diff_samples(None, current, SAMPLED_AT) == []


def test_diff_samples_flags_a_change_at_or_above_threshold():
    previous = {("A", "B"): 1.0}
    current = {("A", "B"): 1.0 + WAVE_HEIGHT_CHANGE_THRESHOLD_M}
    events = diff_samples(previous, current, SAMPLED_AT)
    assert len(events) == 1
    e = events[0]
    assert (e.u, e.v) == ("A", "B")
    assert e.previous_wave_height_m == pytest.approx(1.0)
    assert e.new_wave_height_m == pytest.approx(1.0 + WAVE_HEIGHT_CHANGE_THRESHOLD_M)
    assert e.sampled_at == SAMPLED_AT.isoformat()


def test_diff_samples_ignores_a_change_below_threshold():
    previous = {("A", "B"): 1.0}
    current = {("A", "B"): 1.0 + WAVE_HEIGHT_CHANGE_THRESHOLD_M * 0.5}
    assert diff_samples(previous, current, SAMPLED_AT) == []


def test_diff_samples_only_flags_edges_that_actually_changed():
    previous = {("A", "B"): 1.0, ("B", "C"): 4.0}
    current = {("A", "B"): 1.0, ("B", "C"): 4.0 + WAVE_HEIGHT_CHANGE_THRESHOLD_M}
    events = diff_samples(previous, current, SAMPLED_AT)
    assert len(events) == 1
    assert (events[0].u, events[0].v) == ("B", "C")


def test_diff_samples_respects_a_custom_threshold():
    previous = {("A", "B"): 1.0}
    current = {("A", "B"): 1.2}
    assert diff_samples(previous, current, SAMPLED_AT, threshold_m=0.5) == []
    assert len(diff_samples(previous, current, SAMPLED_AT, threshold_m=0.1)) == 1


def test_diff_samples_skips_an_edge_missing_from_the_previous_sample():
    previous = {}  # not None (that's the "first poll" case) — just missing this edge
    current = {("A", "B"): 5.0}
    assert diff_samples(previous, current, SAMPLED_AT) == []


# --- poll_once: wires sample_all_edges + diff_samples around a mocked fetch ---


def test_poll_once_returns_the_new_sample_and_diffs_against_the_old_one(monkeypatch):
    g = _two_edge_graph()

    calls = {"n": 0}

    def fake_lookup_factory(digraph, field, start):
        n = calls["n"]
        calls["n"] += 1

        def lookup(u, v, t):
            # First call: baseline. Second call: B->C got rougher.
            if (u, v) == ("B", "C") and n == 1:
                return 4.0, 0.0
            return 1.0, 0.0

        return lookup

    monkeypatch.setattr(producer_mod, "open_weather_field_remote", lambda **kwargs: object())
    monkeypatch.setattr(producer_mod, "make_weather_lookup", fake_lookup_factory)

    voyage_start = datetime(2024, 6, 1)
    bbox = dict(west=-1.0, south=-1.0, east=1.0, north=1.0)

    sample1, events1 = poll_once(g, bbox, voyage_start, timedelta(days=16), None, SAMPLED_AT)
    assert events1 == []  # first poll: baseline only
    assert sample1 == {("A", "B"): 1.0, ("B", "C"): 1.0}

    sample2, events2 = poll_once(
        g, bbox, voyage_start, timedelta(days=16), sample1, SAMPLED_AT + timedelta(hours=1)
    )
    assert len(events2) == 1
    assert (events2[0].u, events2[0].v) == ("B", "C")
    assert sample2 == {("A", "B"): 1.0, ("B", "C"): 4.0}
