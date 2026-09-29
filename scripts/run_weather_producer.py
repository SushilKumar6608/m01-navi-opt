"""
Phase 6: the real CLI loop around `navi_opt.streaming.weather_producer`
— repeatedly polls real CMEMS for this corridor's current forecast,
diffs it against the previous poll, and publishes a `WeatherUpdateEvent`
per meaningfully-changed edge (see `weather_producer.py`'s module
docstring for the "why elapsed_hours=0, single fixed reference time"
scoping decision) to the `weather-updates` Kafka topic.

This is the thin network/Kafka-facing wrapper `weather_producer.py`'s
own docstring says stays out of that module and untested from this
sandbox — `poll_once()`/`diff_samples()` underneath it are already
covered by `tests/test_weather_producer.py`; this script is just the
sleep/repeat/publish loop around them.

Run with the Kafka broker up (`docker compose up -d` from `infra/`) and
COPERNICUSMARINE_SERVICE_USERNAME/_PASSWORD set in .env:

    python scripts/run_weather_producer.py
    python scripts/run_weather_producer.py --poll-interval-seconds 60

A real CMEMS forecast doesn't meaningfully change on a sub-hour cadence
(see `DEFAULT_POLL_INTERVAL_SECONDS`'s docstring), so `--poll-interval-
seconds` exists mainly for demoing this without waiting a real hour
between polls — lowering it doesn't manufacture events that wouldn't be
real, it just checks for the same real revisions more often.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from confluent_kafka import Producer  # noqa: E402

from navi_opt.grid.graph_builder import load_graph  # noqa: E402
from navi_opt.streaming.weather_producer import (  # noqa: E402
    DEFAULT_POLL_INTERVAL_SECONDS,
    poll_once,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "processed"
DIGRAPH_PATH = DATA_DIR / "ocean_digraph_rotterdam_ceyhan.pkl"

# Same bbox/window every other Phase 4/5/6 script for this corridor uses —
# see depart_demo_voyage.py's own constants for why these stay identical
# across scripts rather than drifting into per-script values.
BBOX = dict(west=-15.0, south=30.0, east=40.0, north=56.0)
VOYAGE_START = datetime(2024, 6, 1, 0, 0)
FETCH_WINDOW = timedelta(days=16)

DEFAULT_KAFKA_BOOTSTRAP_SERVERS = "localhost:9092"
TOPIC_WEATHER_UPDATES = "weather-updates"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    parser.add_argument("--bootstrap-servers", default=DEFAULT_KAFKA_BOOTSTRAP_SERVERS)
    args = parser.parse_args()

    if not DIGRAPH_PATH.exists():
        print(f"Missing {DIGRAPH_PATH} — run scripts/build_real_ocean_graph.py first.")
        return

    print("Loading cached digraph...")
    digraph = load_graph(path=DIGRAPH_PATH)

    producer = Producer({"bootstrap.servers": args.bootstrap_servers})
    previous_sample: Optional[Dict[Tuple[str, str], float]] = None

    print(f"Polling real CMEMS every {args.poll_interval_seconds:.0f}s, publishing "
          f"changed edges (>= {BBOX}) to '{TOPIC_WEATHER_UPDATES}' "
          f"({args.bootstrap_servers}). Ctrl-C to stop.")
    try:
        while True:
            now = datetime.utcnow()
            print(f"\n[{now.isoformat()}] Fetching current CMEMS forecast...")
            is_first_poll = previous_sample is None
            current_sample, events = poll_once(
                digraph, BBOX, VOYAGE_START, FETCH_WINDOW, previous_sample, now,
            )
            previous_sample = current_sample

            if not events:
                print("   No baseline yet." if is_first_poll else
                      "   No edges changed >= threshold.")
            for event in events:
                print(f"   Changed: {event.u} -> {event.v}: "
                      f"{event.previous_wave_height_m:.2f}m -> {event.new_wave_height_m:.2f}m")
                producer.produce(
                    TOPIC_WEATHER_UPDATES,
                    key=f"{event.u}:{event.v}".encode(),
                    value=json.dumps(asdict(event)).encode(),
                )
            producer.flush(timeout=10.0)
            print(f"   Sampled {len(current_sample)} edges, published {len(events)} event(s). "
                  f"Sleeping {args.poll_interval_seconds:.0f}s...")
            time.sleep(args.poll_interval_seconds)
    except KeyboardInterrupt:
        print("\nStopping — flushing any pending messages...")
        producer.flush(timeout=10.0)


if __name__ == "__main__":
    main()
