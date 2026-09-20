"""
Phase 4: fuel-vs-duration-vs-weather-risk Pareto trade-off analysis over the
Rotterdam <-> Ceyhan corridor, using real CMEMS weather.

This is the natural next consumer of Phase 2 (diverse corridors) and
Phase 3 (weather-aware, deadline-aware speed optimization): for each of
the k diverse corridors, sweeps `max_transit_hours` (the epsilon-constraint
method — see `navi_opt.optimization.pareto`'s module docstring for why
that needed almost no new solver code) from each corridor's fastest-
possible flat-out transit up to a generous margin, runs the real-weather
speed optimizer at each point, and reports the non-dominated (fuel,
duration, weather-risk) frontier across all corridors and epsilons pooled
together.

Run this AFTER `scripts/build_real_ocean_graph.py` (reuses its cached
digraph/anchors) and with COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD
set in .env:
    python scripts/run_pareto_sweep.py

Same network/credential requirements as
`scripts/run_weather_aware_speed_profile.py` — can't be verified from the
sandbox that built this script (no route to CMEMS from there), so read the
printed output before trusting it.

Expected runtime: bounded but genuinely slow. Real solves on the actual
Rotterdam<->Ceyhan corridor (~390 legs) have been measured from 217.7s to
over 32 minutes for a SINGLE epsilon, and 32-minute one didn't even fully
converge — see findings.md's Phase 4 entry for the full story, including
why an earlier iteration-count cap alone failed to bound this. Each solve
now has a hard `DEFAULT_SWEEP_MAX_SECONDS` wall-clock budget (see
`pareto.py`), so the true worst case is `SWEEP_N_POINTS x k_corridors x
DEFAULT_SWEEP_MAX_SECONDS` — with the current settings (5 points, 3
corridors, 180s), that's a genuine, plannable ~45 minutes worst case, not
an open-ended wait. A timed-out point is still returned (an unproven-
optimal but real speed profile, printed as "TIMED OUT" per-epsilon), never
silently dropped.
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from navi_opt.grid.graph_builder import load_graph  # noqa: E402
from navi_opt.grid.port_anchors import load_anchors  # noqa: E402
from navi_opt.optimization.pareto import (  # noqa: E402
    default_epsilon_sweep,
    pareto_frontier,
    sweep_epsilon_constraint,
)
from navi_opt.routing.k_shortest import YenKShortestPaths  # noqa: E402
from navi_opt.weather.vessel_profiles import load_vessel_profile  # noqa: E402
from navi_opt.weather.weather_field import (  # noqa: E402
    make_weather_lookup,
    open_weather_field_remote,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "processed"
DIGRAPH_PATH = DATA_DIR / "ocean_digraph_rotterdam_ceyhan.pkl"
ANCHORS_PATH = DATA_DIR / "port_h3_anchors_rotterdam_ceyhan.parquet"
OUTPUT_CSV = DATA_DIR / "pareto_frontier_rotterdam_ceyhan.csv"

# Matches build_real_ocean_graph.py / run_weather_aware_speed_profile.py.
BBOX = dict(west=-15.0, south=30.0, east=40.0, north=56.0)
VOYAGE_START = datetime(2024, 6, 1, 0, 0)
FETCH_WINDOW = timedelta(days=16)

V_MAX_KNOTS = 16.0

# Sweep from each corridor's fastest-possible flat-out floor up to 60%
# margin over it. Lowered from an original 10 to 5 after a real run showed
# each solve on this corridor's real scale (~390 legs) costs low-single-
# digit minutes — SLSQP's finite-difference gradient makes per-iteration
# cost, not iteration count, the real driver, so this is the one lever
# that reliably cuts wall-clock time (proportionally) without touching
# solver internals. See findings.md's Phase 4 entry for the real timing
# this is based on and what else was tried (maxiter cap, warm-starting).
SWEEP_N_POINTS = 5
SWEEP_MAX_MARGIN = 1.6


def calm_water_cost_evaluator(digraph):
    def _evaluator(u, v, current_t):
        hours = digraph[u][v]["dist_nm"] / V_MAX_KNOTS
        return hours, hours

    return _evaluator


def main() -> None:
    if not DIGRAPH_PATH.exists() or not ANCHORS_PATH.exists():
        print(f"Missing {DIGRAPH_PATH} or {ANCHORS_PATH} — run "
              f"scripts/build_real_ocean_graph.py first, this script reuses its cache.")
        return

    print("1. Loading cached digraph and port anchors...")
    digraph = load_graph(path=DIGRAPH_PATH)
    anchors = load_anchors(path=ANCHORS_PATH)
    print(f"   {digraph.number_of_nodes()} nodes, {digraph.number_of_edges()} edges")

    print(f"2. Streaming CMEMS wave/wind fields over {BBOX}, "
          f"{VOYAGE_START.isoformat()} + {FETCH_WINDOW} directly into memory...")
    weather_field = open_weather_field_remote(
        start=VOYAGE_START, end=VOYAGE_START + FETCH_WINDOW, **BBOX
    )
    weather_lookup = make_weather_lookup(digraph, weather_field, VOYAGE_START)
    print("   loaded.")

    print("3. Finding diverse corridors (same as Phase 2/3's smoke tests)...")
    origin_cell = anchors["rotterdam"]
    destination_cell = anchors["ceyhan"]
    yen = YenKShortestPaths(
        digraph, calm_water_cost_evaluator(digraph), v_max_knots=V_MAX_KNOTS, enforce_diversity=True
    )
    corridors = yen.find_k_paths(origin_cell, destination_cell, k=3, start_time_hours=0.0)
    if not corridors:
        print("   NO PATH FOUND — investigate before continuing.")
        return

    vessel = load_vessel_profile("mr2_product_tanker")

    print(f"\n4. Sweeping the epsilon-constraint (max_transit_hours) per corridor — "
          f"{SWEEP_N_POINTS} points each, floor to {SWEEP_MAX_MARGIN:.0%} margin, "
          f"real-weather speed profile at each point.")
    def _report(idx, eps, point, elapsed):
        if point is None:
            status = "infeasible"
        elif point.success:
            status = "ok"
        elif "max_seconds" in point.result.message:
            status = "TIMED OUT (result kept, unproven-optimal)"
        else:
            status = "ok (no-converge)"
        fuel = f", fuel={point.fuel_tonnes:.1f}t" if point is not None else ""
        print(f"      epsilon {idx + 1}/{SWEEP_N_POINTS} = {eps:.1f}h -> {status} "
              f"in {elapsed:.1f}s{fuel}")

    all_points = []
    for i, corridor in enumerate(corridors):
        path = corridor["path"]
        epsilons = default_epsilon_sweep(
            path, digraph, v_max_knots=V_MAX_KNOTS, n_points=SWEEP_N_POINTS, max_margin=SWEEP_MAX_MARGIN
        )
        print(f"   Corridor {i + 1}:")
        points = sweep_epsilon_constraint(
            i, path, digraph, vessel, "laden", weather_lookup, epsilons, on_progress=_report
        )
        print(f"   Corridor {i + 1}: {len(points)}/{len(epsilons)} epsilons feasible")
        all_points.extend(points)

    print(f"\n5. Pooling {len(all_points)} points across all corridors and epsilons, "
          f"filtering to the non-dominated (fuel, duration, weather-risk) frontier...")
    frontier = pareto_frontier(all_points)
    frontier_sorted = sorted(frontier, key=lambda p: p.transit_hours)

    print(f"\n   {len(frontier_sorted)} non-dominated points survive out of {len(all_points)} total "
          f"(every other point is strictly worse on all three objectives than some point below):")
    print(f"   {'corridor':>8} {'duration(h)':>12} {'fuel(t)':>10} {'risk(m*h)':>10}")
    for p in frontier_sorted:
        print(f"   {p.corridor_index + 1:>8} {p.transit_hours:>12.1f} {p.fuel_tonnes:>10.1f} "
              f"{p.weather_risk_m_hours:>10.1f}")

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["corridor_index", "epsilon_hours", "transit_hours", "fuel_tonnes",
                          "weather_risk_m_hours", "success", "on_frontier"])
        frontier_set = set(id(p) for p in frontier)
        for p in all_points:
            writer.writerow([
                p.corridor_index, f"{p.epsilon_hours:.3f}", f"{p.transit_hours:.3f}",
                f"{p.fuel_tonnes:.3f}", f"{p.weather_risk_m_hours:.3f}", p.success,
                id(p) in frontier_set,
            ])
    print(f"\n   Full sweep (all points, frontier flagged) written to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
