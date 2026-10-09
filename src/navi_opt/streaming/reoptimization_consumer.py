"""
Phase 6: message-handling logic for reacting to `voyage-events` (a voyage
departing) and `weather-updates` (a real forecast change) messages.

Kept as plain functions taking already-parsed dicts and a `VoyageRegistry`,
deliberately separate from any real `confluent_kafka.Consumer` polling
loop — the poll loop itself needs a real broker to exercise and isn't
unit-tested directly (same split `weather_producer.py`'s
`poll_once()`/`diff_samples()` already uses: the network/broker-facing
glue is thin and stays untested from this sandbox, the actual decision
logic underneath it is fully covered). See
`scripts/run_reoptimization_consumer.py` for the real Kafka wiring around
these functions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List

import networkx as nx

from navi_opt.optimization.speed_profile import WeatherLookup
from navi_opt.streaming.reoptimizer import VoyageAlreadyArrived, reoptimize_voyage
from navi_opt.streaming.serialization import (
    speed_profile_result_from_dict,
    speed_profile_result_to_dict,
)
from navi_opt.streaming.voyage_registry import Voyage, VoyageRegistry
from navi_opt.weather.vessel_profiles import load_vessel_profile

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReoptimizationResultEvent:
    """Published to the `reoptimization-results` Kafka topic after a
    voyage is actually reoptimized — the visible, inspectable output of
    this whole subsystem (Kafka UI can show these directly, JSON, no
    special tooling needed, same reasoning as `serialization.py`'s choice
    of plain JSON over a schema-registry format).

    `old_fuel_tonnes`/`old_transit_hours` describe the WHOLE previously
    committed profile, which covers legs the voyage has already sailed;
    `new_*` cover only the legs still ahead. They are NOT comparable —
    the first real run showed "369.7 -> 235.5t" for a voyage that was
    simply 37% done, which reads like a 36% saving and is nothing of the
    kind. The like-for-like comparison is `old_remaining_*` (the same
    remaining legs of the OLD profile, i.e. what the voyage was going to
    burn/take from here on before this reoptimization) against `new_*`;
    use those for any before/after claim. The whole-profile `old_*` fields
    are kept for the record, not for comparison."""

    voyage_id: str
    trigger_u: str
    trigger_v: str
    old_fuel_tonnes: float
    new_fuel_tonnes: float
    old_transit_hours: float
    new_transit_hours: float
    reoptimized_at: str
    committed_profile: Dict[str, Any]
    old_remaining_fuel_tonnes: float = 0.0
    old_remaining_transit_hours: float = 0.0


def handle_voyage_departed(registry: VoyageRegistry, payload: Dict[str, Any]) -> Voyage:
    """Registers a new voyage from a `voyage-events` "departed" message.

    `payload` carries an ALREADY-SOLVED initial `committed_profile` (see
    `scripts/depart_demo_voyage.py` — it runs that first real solve once,
    itself, before publishing). This consumer never solves a voyage's
    FIRST profile, only reoptimizes an already-departed one — publishing
    is what marks a voyage as real and tracked, not a side effect of this
    function guessing at an initial solve.

    Raises `KeyError` for a payload missing a required field rather than
    silently dropping the departure: a lost "voyage departed" event means
    this consumer will never react to weather on a voyage that's actually
    sailing, exactly the class of silent failure this project avoids
    elsewhere (see `pareto_sweep_task.py`'s docstring) by raising instead.
    """
    voyage = Voyage(
        voyage_id=payload["voyage_id"],
        vessel_key=payload["vessel_key"],
        cargo_condition=payload["cargo_condition"],
        voyage_start_time=datetime.fromisoformat(payload["voyage_start_time"]),
        deadline_hours=payload["deadline_hours"],
        demo_departed_at=datetime.fromisoformat(payload["demo_departed_at"]),
        committed_profile=speed_profile_result_from_dict(payload["committed_profile"]),
    )
    registry.register(voyage)
    log.info(
        "Voyage %s departed: %s -> %s, deadline %.1fh",
        voyage.voyage_id, voyage.committed_profile.path[0], voyage.committed_profile.path[-1],
        voyage.deadline_hours,
    )
    return voyage


def handle_weather_update(
    registry: VoyageRegistry,
    digraph: nx.DiGraph,
    weather_lookup: WeatherLookup,
    event: Dict[str, Any],
    now: datetime,
) -> List[ReoptimizationResultEvent]:
    """Reacts to one `weather-updates` message: finds every registered
    voyage that still has the changed edge ahead of it, reoptimizes each,
    commits the new profile to `registry`, and returns one
    `ReoptimizationResultEvent` per voyage actually reoptimized — the
    caller (the real consumer loop) publishes these to
    `reoptimization-results`.

    `weather_lookup` is the CALLER's responsibility to have built fresh
    for this event (a real `open_weather_field_remote()` +
    `make_weather_lookup()` pair, typically). This function deliberately
    does NOT reuse the event's own embedded previous/new wave-height
    values for the actual reoptimization math — the event is a TRIGGER
    ("go re-check edge X"), not ground truth to solve against. A real
    event-driven system re-derives the truth it acts on rather than
    trusting a notification's own payload for a decision with real cost
    (a ~180s-bounded solve per affected voyage — see
    `reoptimizer.DEFAULT_REOPTIMIZE_MAX_SECONDS`) attached to it.

    A voyage that's already arrived by `now` (`reoptimizer
    .VoyageAlreadyArrived`) is skipped, not treated as an error — an
    expected, normal outcome of real time passing between a voyage's
    departure and this event, not a bug to raise on.
    """
    u, v = event["u"], event["v"]
    affected_ids = registry.affected_voyage_ids(u, v, now)
    results: List[ReoptimizationResultEvent] = []

    for voyage_id in affected_ids:
        voyage = registry.get(voyage_id)
        vessel = load_vessel_profile(voyage.vessel_key)
        old_profile = voyage.committed_profile
        try:
            new_profile = reoptimize_voyage(voyage, digraph, vessel, weather_lookup, now)
        except VoyageAlreadyArrived:
            log.info("Voyage %s already arrived by %s — skipping", voyage_id, now)
            continue

        # The new profile covers a suffix of the old one (see
        # reoptimizer.py), so the old profile's matching tail is exactly
        # the legs the voyage was going to sail from here on.
        old_tail = old_profile.legs[len(old_profile.legs) - len(new_profile.legs):]
        old_remaining_fuel = sum(leg.fuel_tonnes for leg in old_tail)
        old_remaining_hours = sum(leg.transit_hours for leg in old_tail)

        registry.commit(voyage_id, new_profile)
        results.append(ReoptimizationResultEvent(
            voyage_id=voyage_id, trigger_u=u, trigger_v=v,
            old_fuel_tonnes=old_profile.total_fuel_tonnes,
            new_fuel_tonnes=new_profile.total_fuel_tonnes,
            old_transit_hours=old_profile.total_transit_hours,
            new_transit_hours=new_profile.total_transit_hours,
            reoptimized_at=now.isoformat(),
            committed_profile=speed_profile_result_to_dict(new_profile),
            old_remaining_fuel_tonnes=old_remaining_fuel,
            old_remaining_transit_hours=old_remaining_hours,
        ))
        log.info(
            "Reoptimized voyage %s after weather change on %s->%s: remaining legs "
            "fuel %.1f -> %.1ft, transit %.1f -> %.1fh",
            voyage_id, u, v, old_remaining_fuel, new_profile.total_fuel_tonnes,
            old_remaining_hours, new_profile.total_transit_hours,
        )

    return results
