"""
Phase 7: reads a whole Kafka topic into a list of dicts for the dashboard.

Thin broker-facing glue, deliberately kept out of `data.py` so that module
stays unit-testable offline — same split `weather_producer.poll_once()` /
`diff_samples()` and the Phase 6 scripts use. This function needs a real
broker to exercise and is NOT covered by the offline tests; the Streamlit
smoke test replaces it with a fake.

Each call uses a throwaway consumer group and reads every partition from the
beginning up to the high watermark that existed when the call started, then
closes — a snapshot, not a subscription. That is the right shape for a
dashboard that re-reads on refresh: it never commits offsets, so it can't
steal messages from `run_reoptimization_consumer.py`'s own group.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BOOTSTRAP_SERVERS = "localhost:9092"
TOPIC_VOYAGE_EVENTS = "voyage-events"
TOPIC_WEATHER_UPDATES = "weather-updates"
TOPIC_REOPTIMIZATION_RESULTS = "reoptimization-results"

#: Hard stop so a dashboard refresh can never hang on a slow/odd broker.
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_MESSAGES = 50_000


def read_topic(
    bootstrap_servers: str,
    topic: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Returns `(events, note)`.

    `note` is None on a clean read, otherwise a short human-readable reason
    the result may be incomplete or empty: broker unreachable, messages
    skipped as malformed, the message cap hit, or the time limit reached. A
    topic that simply doesn't exist yet returns `([], None)` — a fresh
    broker with nothing published is a normal state, not an error.
    """
    from confluent_kafka import Consumer, TopicPartition  # imported lazily: only the app needs it

    deadline = time.monotonic() + timeout_seconds
    try:
        consumer = Consumer({
            "bootstrap.servers": bootstrap_servers,
            "group.id": f"navi-opt-dashboard-{datetime.utcnow().timestamp()}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "socket.timeout.ms": int(timeout_seconds * 1000),
        })
    except Exception as exc:  # bad config
        return [], f"Could not create a Kafka consumer: {exc}"

    try:
        try:
            metadata = consumer.list_topics(topic, timeout=timeout_seconds)
        except Exception as exc:
            return [], f"Cannot reach Kafka at {bootstrap_servers}: {exc}"

        topic_meta = metadata.topics.get(topic)
        if topic_meta is None or topic_meta.error is not None or not topic_meta.partitions:
            return [], None

        assignments = [TopicPartition(topic, p, 0) for p in topic_meta.partitions]
        consumer.assign(assignments)
        remaining: Dict[int, int] = {}
        for tp in assignments:
            _, high = consumer.get_watermark_offsets(tp, timeout=timeout_seconds)
            if high > 0:
                remaining[tp.partition] = high

        events: List[Dict[str, Any]] = []
        malformed = 0
        note: Optional[str] = None
        while remaining:
            if time.monotonic() > deadline:
                note = f"Stopped after {timeout_seconds:.0f}s; '{topic}' may be incompletely read."
                break
            if len(events) >= max_messages:
                note = f"Stopped at {max_messages} messages; '{topic}' may be incompletely read."
                break
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                note = f"Kafka error while reading '{topic}': {msg.error()}"
                break
            try:
                events.append(json.loads(msg.value()))
            except (ValueError, TypeError):
                malformed += 1
            if msg.offset() + 1 >= remaining.get(msg.partition(), 0):
                remaining.pop(msg.partition(), None)

        if malformed and note is None:
            note = f"Skipped {malformed} malformed message(s) on '{topic}'."
        return events, note
    finally:
        consumer.close()
