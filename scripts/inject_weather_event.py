"""
Phase 6 demo/test tool: publishes ONE hand-made `weather-updates` event on
an edge still ahead of a departed voyage, so the Kafka -> consumer ->
reoptimization path can be exercised without waiting for a real CMEMS
forecast revision (the project's fixed historical weather window almost
never revises — see findings.md's Phase 6 "Not yet done" note).

This is NOT a real weather change and is labelled as such: the event carries
`"injected": true`, and the consumer's reoptimization re-fetches REAL
weather anyway (event-as-trigger, not ground truth — see
`reoptimization_consumer.handle_weather_update`'s docstring). So the
reoptimized profile will usually come out close to the one it replaces: this
proves the plumbing end to end (event -> affected-voyage lookup -> bounded
re-solve -> commit -> result published), not that the optimizer reacts to
a storm. Showing a real response needs a weather lookup that actually
differs, which is a separate, larger piece of work.

The edge is chosen with the same tested position logic the consumer uses
(`voyage_registry.current_node_and_remaining_path`): the midpoint of the
voyage's REMAINING path as of now, so the voyage still has it ahead of it.
A voyage that has already arrived has nothing to inject on.

    python scripts/inject_weather_event.py
    python scripts/inject_weather_event.py --voyage-id demo-5c4bea74 --delta-m 1.5
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from confluent_kafka import Consumer, Producer, TopicPartition  # noqa: E402

from navi_opt.streaming.reoptimization_consumer import handle_voyage_departed  # noqa: E402
from navi_opt.streaming.voyage_registry import VoyageRegistry  # noqa: E402

DEFAULT_KAFKA_BOOTSTRAP_SERVERS = "localhost:9092"
TOPIC_VOYAGE_EVENTS = "voyage-events"
TOPIC_WEATHER_UPDATES = "weather-updates"


def _replay_voyages(bootstrap_servers: str, registry: VoyageRegistry) -> None:
    consumer = Consumer({
        "bootstrap.servers": bootstrap_servers,
        "group.id": f"navi-opt-inject-{datetime.utcnow().timestamp()}",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    try:
        partitions = consumer.list_topics(TOPIC_VOYAGE_EVENTS, timeout=10.0) \
            .topics[TOPIC_VOYAGE_EVENTS].partitions
        assignments = [TopicPartition(TOPIC_VOYAGE_EVENTS, p, 0) for p in partitions]
        consumer.assign(assignments)
        remaining = {}
        for tp in assignments:
            _, high = consumer.get_watermark_offsets(tp, timeout=10.0)
            if high > 0:
                remaining[tp.partition] = high
        while remaining:
            msg = consumer.poll(2.0)
            if msg is None:
                break
            if msg.error():
                break
            handle_voyage_departed(registry, json.loads(msg.value()))
            if msg.offset() + 1 >= remaining.get(msg.partition(), 0):
                remaining.pop(msg.partition(), None)
    finally:
        consumer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voyage-id", default=None, help="Defaults to the most recently departed voyage.")
    parser.add_argument("--delta-m", type=float, default=1.0,
                        help="Wave-height change (metres) to claim on the chosen edge (default 1.0).")
    parser.add_argument("--bootstrap-servers", default=DEFAULT_KAFKA_BOOTSTRAP_SERVERS)
    args = parser.parse_args()

    registry = VoyageRegistry()
    _replay_voyages(args.bootstrap_servers, registry)
    voyage_ids = registry.all_voyage_ids()
    if not voyage_ids:
        print(f"No departed voyage found on '{TOPIC_VOYAGE_EVENTS}' — run depart_demo_voyage.py first.")
        return

    voyage_id = args.voyage_id or voyage_ids[-1]
    if voyage_id not in voyage_ids:
        print(f"Voyage {voyage_id!r} not found. Known: {voyage_ids}")
        return

    now = datetime.utcnow()
    node, remaining_path = registry.current_node_and_remaining_path(voyage_id, now)
    if len(remaining_path) < 2:
        print(f"Voyage {voyage_id!r} has already arrived ({node}) — nothing ahead to inject on. "
              f"Depart a new one with depart_demo_voyage.py.")
        return

    mid = (len(remaining_path) - 1) // 2
    u, v = remaining_path[mid], remaining_path[mid + 1]
    elapsed = registry.elapsed_hours(voyage_id, now)
    event = {
        "u": u, "v": v,
        "previous_wave_height_m": 2.0,
        "new_wave_height_m": 2.0 + args.delta_m,
        "sampled_at": now.isoformat(),
        "injected": True,
    }

    print(f"Voyage {voyage_id}: {elapsed:.0f} simulated hours in, currently at {node}, "
          f"{len(remaining_path) - 1} legs remaining.")
    print(f"Injecting a labelled test event on {u} -> {v} (leg {mid + 1} of the remaining path) "
          f"to '{TOPIC_WEATHER_UPDATES}'...")
    producer = Producer({"bootstrap.servers": args.bootstrap_servers})
    producer.produce(TOPIC_WEATHER_UPDATES, key=f"{u}:{v}".encode(), value=json.dumps(event).encode())
    producer.flush(timeout=10.0)
    print("Published. Watch the consumer window: it should re-fetch real CMEMS (~20s), "
          "re-solve the remaining legs (up to 180s), and print the before/after.")


if __name__ == "__main__":
    main()
