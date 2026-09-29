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

As of Phase 5, this script is a thin interactive wrapper: the actual sweep
pipeline (graph/anchors loading, corridor finding, the epsilon sweep, the
Pareto filter, the CSV write) lives in
`navi_opt.orchestration.pareto_sweep_task.run_pareto_sweep_task()`, so the
exact same logic also runs unattended from
`infra/airflow/dags/pareto_sweep_dag.py`. This file now only supplies:
the module-level constants as a `SweepConfig`, the corridor-level and
per-epsilon print callbacks for interactive progress reporting, and a
try/except around the missing-prerequisite-cache case so running this by
hand still gets the same friendly message it always did (the extracted
task function itself raises there instead, since a scheduled run has no
one watching stdout to notice a silent early return — see that module's
docstring).

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

import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from navi_opt.optimization.pareto import DEFAULT_SEVERE_WAVE_THRESHOLD_M  # noqa: E402
from navi_opt.orchestration.pareto_sweep_task import (  # noqa: E402
    SweepConfig,
    run_pareto_sweep_task,
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

# Significant-wave-height threshold (metres) for the severe-weather-hours
# risk-budget column — see pareto.py's DEFAULT_SEVERE_WAVE_THRESHOLD_M
# docstring for why 4.5m and why it's a documented default, not a derived
# value. Kept as its own constant here (rather than relying on the
# imported default silently) so a corridor-specific override is a
# one-line change in the one place this script's output actually depends
# on it.
SEVERE_WEATHER_THRESHOLD_M = DEFAULT_SEVERE_WAVE_THRESHOLD_M


def main() -> None:
    config = SweepConfig(
        origin_port="rotterdam",
        destination_port="ceyhan",
        digraph_path=DIGRAPH_PATH,
        anchors_path=ANCHORS_PATH,
        output_csv=OUTPUT_CSV,
        voyage_start=VOYAGE_START,
        fetch_window=FETCH_WINDOW,
        bbox=BBOX,
        v_max_knots=V_MAX_KNOTS,
        sweep_n_points=SWEEP_N_POINTS,
        sweep_max_margin=SWEEP_MAX_MARGIN,
        severe_weather_threshold_m=SEVERE_WEATHER_THRESHOLD_M,
    )

    print("1. Loading cached digraph and port anchors...")
    print(f"2. Streaming CMEMS wave/wind fields over {BBOX}, "
          f"{VOYAGE_START.isoformat()} + {FETCH_WINDOW} directly into memory...")
    print("3. Finding diverse corridors (same as Phase 2/3's smoke tests)...")
    print(f"4. Sweeping the epsilon-constraint (max_transit_hours) per corridor — "
          f"{SWEEP_N_POINTS} points each, floor to {SWEEP_MAX_MARGIN:.0%} margin, "
          f"real-weather speed profile at each point.")

    def _on_corridor_start(idx: int, total: int) -> None:
        print(f"   Corridor {idx + 1}/{total}:")

    def _on_progress(idx, eps, point, elapsed):
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

    try:
        result = run_pareto_sweep_task(
            config, on_progress=_on_progress, on_corridor_start=_on_corridor_start
        )
    except FileNotFoundError as exc:
        print(f"   {exc}")
        return
    except ValueError as exc:
        print(f"   {exc}")
        return

    frontier_sorted = sorted(result.frontier, key=lambda p: p.transit_hours)
    print(f"\n5. Pooled {len(result.all_points)} points across all corridors and epsilons, "
          f"filtering to the non-dominated (fuel, duration, weather-risk) frontier...")
    print(f"\n   {len(frontier_sorted)} non-dominated points survive out of "
          f"{len(result.all_points)} total (every other point is strictly worse on all "
          f"three objectives than some point below):")
    print(f"   {'corridor':>8} {'duration(h)':>12} {'fuel(t)':>10} {'risk(m*h)':>10} "
          f"{'severe(h)':>10}")
    for p in frontier_sorted:
        print(f"   {p.corridor_index + 1:>8} {p.transit_hours:>12.1f} {p.fuel_tonnes:>10.1f} "
              f"{p.weather_risk_m_hours:>10.1f} {p.severe_weather_hours:>10.1f}")

    print(f"\n   Full sweep (all points, frontier flagged, "
          f"severe-weather-hours >= {SEVERE_WEATHER_THRESHOLD_M:g}m) written to "
          f"{result.output_csv}")


if __name__ == "__main__":
    main()
