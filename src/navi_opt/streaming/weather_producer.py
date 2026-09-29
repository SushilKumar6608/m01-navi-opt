"""
Phase 6: publishes a Kafka event whenever this corridor's real CMEMS
weather forecast for a given (edge, target-time) pair has meaningfully
changed since the last time this producer checked.

Scope decision, made explicit rather than silently narrowed (same pattern
Phase 5's findings.md entry used for its own "one corridor, not a fleet"
call): this producer is decoupled from any specific voyage's schedule —
it does NOT know which voyages are registered in
`voyage_registry.VoyageRegistry` (that lives in the consumer process, and
Kafka producers/consumers being mutually unaware of each other's internal
state is the whole point of an event bus, not a gap to close). Instead it
monitors every edge of the cached digraph at a single fixed reference
time, `elapsed_hours=0` — i.e. "what would the near-term/nowcast wave
height be for a voyage departing right now" — each poll, and diffs that
against the previous poll's sample at the same reference time. This is a
real, defensible proxy for "has the forecast changed" (CMEMS forecasts DO
get revised as new observations come in, even for the same target dates),
not a claim that it monitors every voyage's actual scheduled transit time
for every leg — a more complete system would sample at each currently-
active voyage's own per-leg scheduled time instead, which needs the
producer to read voyage state and is a real architecture change, not
implemented here.

The pure, testable part is `diff_samples()`; everything network/Kafka-
facing is kept out of it and out of any function this module unit-tests
directly — see `tests/test_weather_producer.py`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import networkx as nx

from navi_opt.weather.weather_field import WeatherField, make_weather_lookup, open_weather_field_remote

log = logging.getLogger(__name__)

#: Meaningful-change filter (metres) — same "don't fire on solver/sensor
#: noise, only on a change actually worth reacting to" reasoning as
#: `pareto.py`'s `FUEL_TOL_TONNES`/etc., though this threshold is chosen
#: for a different reason: without it, ordinary forecast-to-forecast
#: floating-point-level revision would fire an event, and a real
#: reoptimization consumer, per Phase 6's scoping decision, always does a
#: genuine re-solve in reaction — a 180s-bounded optimizer call per Phase
#: 4/5's `DEFAULT_SWEEP_MAX_SECONDS` reasoning — so an unfiltered producer
#: would trigger real, non-trivial work for changes too small to matter.
WAVE_HEIGHT_CHANGE_THRESHOLD_M = 0.5

#: Real CMEMS forecasts don't meaningfully update on a sub-hour cadence,
#: so polling faster than this wastes API calls without more signal — a
#: documented default, not a derived value (same pattern as
#: `DEFAULT_SEVERE_WAVE_THRESHOLD_M`), overridable by a caller (e.g. a
#: demo wanting to see an event fire without waiting a real hour).
DEFAULT_POLL_INTERVAL_SECONDS = 3600.0

#: Reference elapsed-hours this producer always samples at — see module
#: docstring for the "why elapsed_hours=0, not per-voyage" scoping call.
REFERENCE_ELAPSED_HOURS = 0.0


@dataclass(frozen=True)
class WeatherUpdateEvent:
    """One meaningfully-changed edge, ready to be JSON-serialized (it's
    already flat — `dataclasses.asdict()` needs no special handling,
    unlike `SpeedProfileResult`/`LegResult` in `serialization.py`) and
    published to the `weather-updates` Kafka topic."""

    u: str
    v: str
    previous_wave_height_m: float
    new_wave_height_m: float
    sampled_at: str  # ISO 8601 — real publish time, not the weather's own reference time


def sample_all_edges(
    digraph: nx.DiGraph, weather_lookup, at_elapsed_hours: float = REFERENCE_ELAPSED_HOURS
) -> Dict[Tuple[str, str], float]:
    """Wave height at every edge of `digraph`, sampled via `weather_lookup`
    at a single fixed `at_elapsed_hours` for all of them (see module
    docstring). Returns a plain `{(u, v): wave_height_m}` dict — the
    natural key for diffing two consecutive polls against each other."""
    return {
        (u, v): weather_lookup(u, v, at_elapsed_hours)[0]
        for u, v in digraph.edges
    }


def diff_samples(
    previous: Optional[Dict[Tuple[str, str], float]],
    current: Dict[Tuple[str, str], float],
    sampled_at: datetime,
    threshold_m: float = WAVE_HEIGHT_CHANGE_THRESHOLD_M,
) -> List[WeatherUpdateEvent]:
    """The actual "did anything change enough to matter" logic, kept pure
    (no Kafka, no network) so it's directly unit-testable.

    `previous=None` (the producer's first poll, with nothing to diff
    against yet) always yields an empty list, deliberately — publishing
    an event for every edge on startup would look like "the weather just
    changed everywhere," which is misleading; the first poll establishes
    a baseline, not a change.
    """
    if previous is None:
        return []

    events = []
    for edge, new_height in current.items():
        old_height = previous.get(edge)
        if old_height is None:
            continue  # edge wasn't in the previous sample (digraph changed) — not a weather change
        if abs(new_height - old_height) >= threshold_m:
            events.append(WeatherUpdateEvent(
                u=edge[0], v=edge[1],
                previous_wave_height_m=old_height, new_wave_height_m=new_height,
                sampled_at=sampled_at.isoformat(),
            ))
    return events


def poll_once(
    digraph: nx.DiGraph,
    bbox: Dict[str, float],
    voyage_start_time: datetime,
    fetch_window,
    previous_sample: Optional[Dict[Tuple[str, str], float]],
    now: datetime,
    threshold_m: float = WAVE_HEIGHT_CHANGE_THRESHOLD_M,
) -> Tuple[Dict[Tuple[str, str], float], List[WeatherUpdateEvent]]:
    """One real poll: fetches the CURRENT CMEMS forecast for `bbox` fresh
    (network — `open_weather_field_remote()`), samples every edge, diffs
    against `previous_sample`, and returns `(new_sample, events)` — the
    caller (the producer loop) is responsible for publishing `events` to
    Kafka and holding onto `new_sample` as next poll's `previous_sample`.
    Kept separate from the publish step and the sleep/repeat loop so this
    one function is the only piece that needs a real network call to
    exercise, and everything else (`diff_samples()` above, the loop's own
    control flow) doesn't.
    """
    weather_field: WeatherField = open_weather_field_remote(
        start=voyage_start_time, end=voyage_start_time + fetch_window, **bbox,
    )
    weather_lookup = make_weather_lookup(digraph, weather_field, voyage_start_time)
    current_sample = sample_all_edges(digraph, weather_lookup)
    events = diff_samples(previous_sample, current_sample, now, threshold_m)
    return current_sample, events
