"""
Phase 6: solves ONE real initial speed profile for the Rotterdam<->Ceyhan
corridor (reusing the same cached digraph/anchors and real CMEMS fetch
every earlier phase has used) and publishes it as a "voyage departed"
event to the `voyage-events` Kafka topic — the only place in Phase 6 that
solves a voyage's FIRST profile; `reoptimization_consumer.py`'s
`handle_voyage_departed()` deliberately just registers whatever profile
this script already solved and published (see its docstring).

Run this AFTER `scripts/build_real_ocean_graph.py` (reuses its cached
digraph/anchors), with COPERNICUSMARINE_SERVICE_USERNAME/_PASSWORD set in
.env, and with the Kafka broker from `infra/docker-compose.yml` up
(`docker compose up -d` from `infra/`):

    python scripts/depart_demo_voyage.py
    python scripts/depart_demo_voyage.py --voyage-id my-voyage --deadline-margin 1.4

Each run publishes exactly one voyage. Run it more than once (distinct
`--voyage-id`s, the default already randomizes one) to have several
voyages in flight at once against `run_reoptimization_consumer.py` — nothing
here prevents that, since `VoyageRegistry` (see its own docstring) is keyed
by voyage_id with no assumption of exactly one voyage.

Same network/credential requirements, and the same "can't be verified from
the sandbox that built this script" caveat, as
`scripts/run_weather_aware_speed_profile.py` and `scripts/run_pareto_sweep.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from confluent_kafka import Producer  # noqa: E402

from navi_opt.grid.graph_builder import load_graph  # noqa: E402
from navi_opt.grid.port_anchors import load_anchors  # noqa: E402
from navi_opt.optimization.speed_profile import optimize_speed_profile  # noqa: E402
from navi_opt.routing.k_shortest import YenKShortestPaths  # noqa: E402
from navi_opt.streaming.reoptimizer import (  # noqa: E402
    DEFAULT_REOPTIMIZE_MAXITER,
    DEFAULT_REOPTIMIZE_MAX_SECONDS,
)
from navi_opt.streaming.serialization import speed_profile_result_to_dict  # noqa: E402
from navi_opt.weather.vessel_profiles import load_vessel_profile  # noqa: E402
from navi_opt.weather.weather_field import make_weather_lookup, open_weather_field_remote  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "processed"
DIGRAPH_PATH = DATA_DIR / "ocean_digraph_rotterdam_ceyhan.pkl"
ANCHORS_PATH = DATA_DIR / "port_h3_anchors_rotterdam_ceyhan.parquet"

# Same bbox/window/vessel every other Phase 4/5/6 script uses for this
# corridor — kept identical rather than reintroduced as separate constants
# so a departed voyage's `voyage_start_time` lines up with the same CMEMS
# window `run_reoptimization_consumer.py` re-fetches against later.
BBOX = dict(west=-15.0, south=30.0, east=40.0, north=56.0)
VOYAGE_START = datetime(2024, 6, 1, 0, 0)
FETCH_WINDOW = timedelta(days=16)
V_MAX_KNOTS = 16.0
VESSEL_KEY = "mr2_product_tanker"
CARGO_CONDITION = "laden"
ORIGIN_PORT = "rotterdam"
DESTINATION_PORT = "ceyhan"

# How generous a deadline to solve the initial profile against, as a
# multiple of the corridor's fastest-possible flat-out transit — same
# "floor to margin" epsilon-sweep framing `run_pareto_sweep.py`'s
# `SWEEP_MAX_MARGIN` uses, picked here at a single fixed point (1.5x)
# rather than swept, since this script solves ONE profile to depart with,
# not a frontier.
DEFAULT_DEADLINE_MARGIN = 1.5

DEFAULT_KAFKA_BOOTSTRAP_SERVERS = "localhost:9092"
TOPIC_VOYAGE_EVENTS = "voyage-events"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--voyage-id", default=None,
        help="Defaults to a random 'demo-<8 hex chars>' id.",
    )
    parser.add_argument(
        "--deadline-margin", type=float, default=DEFAULT_DEADLINE_MARGIN,
        help=f"Multiple of the corridor's flat-out floor transit time "
             f"(default {DEFAULT_DEADLINE_MARGIN}).",
    )
    parser.add_argument("--bootstrap-servers", default=DEFAULT_KAFKA_BOOTSTRAP_SERVERS)
    args = parser.parse_args()

    voyage_id = args.voyage_id or f"demo-{uuid.uuid4().hex[:8]}"

    if not DIGRAPH_PATH.exists() or not ANCHORS_PATH.exists():
        print(f"Missing {DIGRAPH_PATH} or {ANCHORS_PATH} — run "
              f"scripts/build_real_ocean_graph.py first.")
        return

    print(f"1. Loading cached digraph and port anchors...")
    digraph = load_graph(path=DIGRAPH_PATH)
    anchors = load_anchors(path=ANCHORS_PATH)
    if ORIGIN_PORT not in anchors or DESTINATION_PORT not in anchors:
        print(f"   Port anchor missing for {ORIGIN_PORT!r} or {DESTINATION_PORT!r} — "
              f"check {ANCHORS_PATH}.")
        return

    print(f"2. Streaming CMEMS wave/wind fields over {BBOX}, "
          f"{VOYAGE_START.isoformat()} + {FETCH_WINDOW} directly into memory...")
    weather_field = open_weather_field_remote(
        start=VOYAGE_START, end=VOYAGE_START + FETCH_WINDOW, **BBOX,
    )
    weather_lookup = make_weather_lookup(digraph, weather_field, VOYAGE_START)

    print(f"3. Finding a corridor from {ORIGIN_PORT!r} to {DESTINATION_PORT!r}...")

    def _calm_water_cost_evaluator(u, v, current_t):
        hours = digraph[u][v]["dist_nm"] / V_MAX_KNOTS
        return hours, hours

    yen = YenKShortestPaths(
        digraph, _calm_water_cost_evaluator, v_max_knots=V_MAX_KNOTS, enforce_diversity=True
    )
    corridors = yen.find_k_paths(
        anchors[ORIGIN_PORT], anchors[DESTINATION_PORT], k=1, start_time_hours=0.0
    )
    if not corridors:
        print(f"   No path found between {ORIGIN_PORT!r} and {DESTINATION_PORT!r} "
              f"on the cached digraph.")
        return
    path = corridors[0]["path"]
    floor_hours = sum(digraph[u][v]["dist_nm"] for u, v in zip(path, path[1:])) / V_MAX_KNOTS
    deadline_hours = floor_hours * args.deadline_margin

    print(f"4. Solving the real-weather speed profile "
          f"({len(path)} nodes, deadline {deadline_hours:.1f}h = "
          f"{args.deadline_margin:g}x the {floor_hours:.1f}h flat-out floor)...")
    vessel = load_vessel_profile(VESSEL_KEY)
    # Bounded, same as every other solve on this corridor: an unbounded
    # SLSQP run over ~390 legs was measured at 217s to 32+ minutes in
    # Phase 4 (see findings.md), so this reuses the reoptimizer's budget
    # rather than leaving the first solve open-ended. A timed-out result
    # is still a real, usable profile (unproven-optimal), not a failure.
    profile = optimize_speed_profile(
        path, digraph, vessel, CARGO_CONDITION,
        weather_lookup=weather_lookup, start_time_hours=0.0, max_transit_hours=deadline_hours,
        maxiter=DEFAULT_REOPTIMIZE_MAXITER, max_seconds=DEFAULT_REOPTIMIZE_MAX_SECONDS,
    )
    if not profile.success:
        print(f"   Solve did not converge cleanly ({profile.message}) — "
              f"publishing anyway, it's still a real usable profile.")

    demo_departed_at = datetime.utcnow()
    payload = {
        "voyage_id": voyage_id,
        "vessel_key": VESSEL_KEY,
        "cargo_condition": CARGO_CONDITION,
        "voyage_start_time": VOYAGE_START.isoformat(),
        "deadline_hours": deadline_hours,
        "demo_departed_at": demo_departed_at.isoformat(),
        "committed_profile": speed_profile_result_to_dict(profile),
    }

    print(f"5. Publishing 'voyage departed' event for {voyage_id!r} to "
          f"'{TOPIC_VOYAGE_EVENTS}' ({args.bootstrap_servers})...")
    producer = Producer({"bootstrap.servers": args.bootstrap_servers})
    producer.produce(
        TOPIC_VOYAGE_EVENTS, key=voyage_id.encode(), value=json.dumps(payload).encode(),
    )
    producer.flush(timeout=10.0)

    print(f"\n   Departed: {voyage_id}")
    print(f"   Path: {path[0]} -> ... -> {path[-1]} ({len(path)} nodes)")
    print(f"   Fuel: {profile.total_fuel_tonnes:.1f}t, transit: {profile.total_transit_hours:.1f}h, "
          f"deadline: {deadline_hours:.1f}h")
    print(f"   Now run scripts/run_reoptimization_consumer.py (if not already running) "
          f"and scripts/run_weather_producer.py to watch it get reoptimized on real "
          f"weather changes.")


if __name__ == "__main__":
    main()
