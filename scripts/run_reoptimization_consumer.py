"""
Phase 6: the real Kafka consumer loop around
`navi_opt.streaming.reoptimization_consumer` — subscribes to
`voyage-events` and `weather-updates`, keeps an in-memory
`VoyageRegistry` (see that module's docstring for the "in-memory, not
Postgres-backed" scope decision), dispatches each message to
`handle_voyage_departed()` / `handle_weather_update()`, and publishes a
`ReoptimizationResultEvent` per actually-reoptimized voyage to
`reoptimization-results`.

Per `handle_weather_update()`'s own docstring, a weather event is treated
as a TRIGGER, not ground truth: on every `weather-updates` message this
loop does a REAL fresh CMEMS fetch (`open_weather_field_remote()` +
`make_weather_lookup()`) rather than trusting the event's own embedded
wave-height values for the reoptimization math.

In-memory state means a restart loses every registered voyage — this
consumer subscribes from the EARLIEST offset on `voyage-events`
specifically so a restart can replay past departures and rebuild its
registry (see `_consume_departure_backlog()` below); `weather-updates` is
read live (`latest`) since replaying old weather diffs on every restart
would just re-trigger reoptimizations of voyages that were already
reoptimized against that same change.

Run with the Kafka broker up (`docker compose up -d` from `infra/`),
COPERNICUSMARINE_SERVICE_USERNAME/_PASSWORD set in .env, and after (or
alongside) `scripts/depart_demo_voyage.py` / `scripts/run_weather_producer.py`:

    python scripts/run_reoptimization_consumer.py
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from confluent_kafka import Consumer, KafkaError, Producer, TopicPartition  # noqa: E402

from navi_opt.grid.graph_builder import load_graph  # noqa: E402
from navi_opt.streaming.reoptimization_consumer import (  # noqa: E402
    handle_voyage_departed,
    handle_weather_update,
)
from navi_opt.streaming.voyage_registry import VoyageRegistry  # noqa: E402
from navi_opt.weather.weather_field import make_weather_lookup, open_weather_field_remote  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "processed"
DIGRAPH_PATH = DATA_DIR / "ocean_digraph_rotterdam_ceyhan.pkl"

# Same bbox/window every other Phase 4/5/6 script for this corridor uses —
# see depart_demo_voyage.py's own constants for why these stay identical.
BBOX = dict(west=-15.0, south=30.0, east=40.0, north=56.0)
VOYAGE_START = datetime(2024, 6, 1, 0, 0)
FETCH_WINDOW = timedelta(days=16)

DEFAULT_KAFKA_BOOTSTRAP_SERVERS = "localhost:9092"
TOPIC_VOYAGE_EVENTS = "voyage-events"
TOPIC_WEATHER_UPDATES = "weather-updates"
TOPIC_REOPTIMIZATION_RESULTS = "reoptimization-results"
CONSUMER_GROUP_ID = "navi-opt-reoptimization-consumer"


def _consume_departure_backlog(bootstrap_servers: str, registry: VoyageRegistry) -> None:
    """Replays every `voyage-events` message from the beginning, on a
    throwaway consumer/group, to rebuild `registry` before the main loop
    starts — the fix for the "in-memory state is lost on restart"
    limitation `VoyageRegistry`'s own docstring names but doesn't itself
    solve (that module deliberately has no persistence of its own). Not a
    full replacement for real durability: a voyage's REOPTIMIZED profile
    (as opposed to its original departure) is not itself republished to
    `voyage-events`, so a restart mid-voyage rebuilds each voyage from its
    ORIGINAL committed profile, not its latest reoptimized one — a real
    gap, left open here rather than silently masked, since closing it
    properly means either replaying `reoptimization-results` too (last
    result wins) or the Postgres-backed registry this phase explicitly
    chose not to build yet (see `voyage_registry.py`'s docstring)."""
    consumer = Consumer({
        "bootstrap.servers": bootstrap_servers,
        "group.id": f"{CONSUMER_GROUP_ID}-backlog-{datetime.utcnow().timestamp()}",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    try:
        try:
            partitions = consumer.list_topics(TOPIC_VOYAGE_EVENTS, timeout=10.0) \
                .topics[TOPIC_VOYAGE_EVENTS].partitions
        except Exception as exc:
            # A brand-new cluster with no voyage-events topic yet (nobody
            # has run depart_demo_voyage.py at all) is a normal starting
            # state, not a failure — nothing to replay, start with an
            # empty registry.
            print(f"   No existing '{TOPIC_VOYAGE_EVENTS}' topic to replay ({exc}) — "
                  f"starting with an empty registry.")
            return

        assignments = [TopicPartition(TOPIC_VOYAGE_EVENTS, p, 0) for p in partitions]
        consumer.assign(assignments)
        end_offsets = {
            tp.partition: consumer.get_watermark_offsets(tp, timeout=10.0)[1]
            for tp in assignments
        }
        remaining = {p: off for p, off in end_offsets.items() if off > 0}
        n_replayed = 0
        while remaining:
            msg = consumer.poll(2.0)
            if msg is None:
                break
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                break
            payload = json.loads(msg.value())
            handle_voyage_departed(registry, payload)
            n_replayed += 1
            if msg.offset() + 1 >= remaining.get(msg.partition(), 0):
                remaining.pop(msg.partition(), None)
        print(f"Rebuilt registry from {n_replayed} past departure(s): "
              f"{registry.all_voyage_ids()}")
    finally:
        consumer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-servers", default=DEFAULT_KAFKA_BOOTSTRAP_SERVERS)
    args = parser.parse_args()

    if not DIGRAPH_PATH.exists():
        print(f"Missing {DIGRAPH_PATH} — run scripts/build_real_ocean_graph.py first.")
        return

    print("Loading cached digraph...")
    digraph = load_graph(path=DIGRAPH_PATH)

    registry = VoyageRegistry()
    print("Replaying past 'voyage departed' events to rebuild registry...")
    _consume_departure_backlog(args.bootstrap_servers, registry)

    producer = Producer({"bootstrap.servers": args.bootstrap_servers})
    consumer = Consumer({
        "bootstrap.servers": args.bootstrap_servers,
        "group.id": CONSUMER_GROUP_ID,
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
    })
    consumer.subscribe([TOPIC_VOYAGE_EVENTS, TOPIC_WEATHER_UPDATES])

    print(f"Listening on '{TOPIC_VOYAGE_EVENTS}' and '{TOPIC_WEATHER_UPDATES}' "
          f"({args.bootstrap_servers}). Ctrl-C to stop.")
    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                print(f"   Consumer error: {msg.error()}")
                continue

            payload = json.loads(msg.value())

            if msg.topic() == TOPIC_VOYAGE_EVENTS:
                voyage = handle_voyage_departed(registry, payload)
                print(f"[voyage-events] Registered {voyage.voyage_id!r} "
                      f"({voyage.committed_profile.path[0]} -> "
                      f"{voyage.committed_profile.path[-1]})")

            elif msg.topic() == TOPIC_WEATHER_UPDATES:
                print(f"[weather-updates] {payload['u']} -> {payload['v']}: "
                      f"{payload['previous_wave_height_m']:.2f}m -> "
                      f"{payload['new_wave_height_m']:.2f}m — re-fetching real CMEMS "
                      f"and reoptimizing affected voyages...")
                now = datetime.utcnow()
                weather_field = open_weather_field_remote(
                    start=VOYAGE_START, end=VOYAGE_START + FETCH_WINDOW, **BBOX,
                )
                weather_lookup = make_weather_lookup(digraph, weather_field, VOYAGE_START)
                results = handle_weather_update(registry, digraph, weather_lookup, payload, now)

                for result in results:
                    print(f"   Reoptimized {result.voyage_id!r}: "
                          f"fuel {result.old_fuel_tonnes:.1f} -> {result.new_fuel_tonnes:.1f}t, "
                          f"transit {result.old_transit_hours:.1f} -> "
                          f"{result.new_transit_hours:.1f}h")
                    producer.produce(
                        TOPIC_REOPTIMIZATION_RESULTS,
                        key=result.voyage_id.encode(),
                        value=json.dumps(asdict(result)).encode(),
                    )
                if not results:
                    print("   No registered voyage still has that edge ahead of it.")
                producer.flush(timeout=10.0)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
