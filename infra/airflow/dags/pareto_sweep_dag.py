"""
Phase 5: Airflow DAG wrapping Phase 4's Rotterdam<->Ceyhan Pareto sweep as a
scheduled batch task, via `navi_opt.orchestration.pareto_sweep_task
.run_pareto_sweep_task()` — the same function `scripts/run_pareto_sweep.py`
now calls for interactive use (see that script's and that module's
docstrings for the full split of what moved where and why).

Deliberately still ONE corridor (Rotterdam<->Ceyhan), not a "fleet" loop
over multiple routes: this project has exactly one cached real-weather
digraph, built for that specific bbox by
`scripts/build_real_ocean_graph.py`. A genuine multi-route fleet batch
needs either a much larger shared ocean grid or a per-route graph-building
step first — that's a real scope decision for a later phase, not
something to fake here by looping this DAG's task over hardcoded port
pairs that share a graph it was never built to route across. This DAG is
the "batch orchestration" half of that eventual fleet-planning pipeline —
real, but scoped to what the project's data actually supports today.

Safety notes before enabling this DAG for real:

- It is genuinely slow and hits real infrastructure: up to ~45 minutes
  wall-clock (5 epsilons x 3 corridors x a 180s per-solve cap — see
  `pareto.py`'s `DEFAULT_SWEEP_MAX_SECONDS` docstring) and a real network
  round-trip to Copernicus Marine Service for live weather data. This is
  not a demo stub that returns instantly.
- `docker-compose.yml`'s `AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION: true`
  means this DAG loads paused and does nothing until a person explicitly
  unpauses it in the Airflow UI or CLI — the `schedule` below being
  `@daily` does not mean it starts running nightly the moment this file
  is dropped into `infra/airflow/dags/`.
- `retries=0` (see `default_args` below) is deliberate, not an oversight:
  a transient CMEMS network hiccup is exactly the kind of thing a naive
  `retries=1` would "fix" by silently re-running a ~45-minute task with no
  guarantee the second attempt succeeds either — better to fail loudly
  and let a person decide whether to re-trigger, given the real cost of a
  retry here.
- Requires `../.env` (the project root's, alongside `.env.example`) to
  contain real `COPERNICUSMARINE_SERVICE_USERNAME`/`_PASSWORD` —
  `docker-compose.yml`'s `env_file: ../.env` on `x-airflow-common` is what
  gets those into this container's environment; without a real `.env`
  there, `docker compose up` itself fails at the env_file step, before
  this DAG is even reached.

NOT verified end-to-end from the sandbox that wrote this (no Docker, no
route to CMEMS, no Airflow instance there) — read the actual Airflow task
log from a real run before trusting this DAG is fully wired correctly;
the unit tests in `tests/test_pareto_sweep_task.py` cover the function
this DAG calls, not Airflow's own scheduling/execution machinery.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task

from navi_opt.optimization.pareto import DEFAULT_SEVERE_WAVE_THRESHOLD_M
from navi_opt.orchestration.pareto_sweep_task import SweepConfig, run_pareto_sweep_task

# Paths as they resolve INSIDE the Airflow containers — see
# docker-compose.yml's x-airflow-common volumes comment for why
# /opt/airflow/{src,config,data} mirror the repo-root-relative layout
# `graph_builder.py`/`port_anchors.py` already assume when run locally.
DATA_DIR = Path("/opt/airflow/data/processed")
DIGRAPH_PATH = DATA_DIR / "ocean_digraph_rotterdam_ceyhan.pkl"
ANCHORS_PATH = DATA_DIR / "port_h3_anchors_rotterdam_ceyhan.parquet"

# Matches scripts/build_real_ocean_graph.py / run_weather_aware_speed_profile.py
# / run_pareto_sweep.py's own constants — same corridor, same weather window.
BBOX = dict(west=-15.0, south=30.0, east=40.0, north=56.0)
VOYAGE_START = datetime(2024, 6, 1, 0, 0)
FETCH_WINDOW = timedelta(days=16)

V_MAX_KNOTS = 16.0
SWEEP_N_POINTS = 5
SWEEP_MAX_MARGIN = 1.6
SEVERE_WEATHER_THRESHOLD_M = DEFAULT_SEVERE_WAVE_THRESHOLD_M

log = logging.getLogger(__name__)


@dag(
    dag_id="pareto_sweep_rotterdam_ceyhan",
    description=(
        "Phase 4/5: nightly fuel-vs-duration-vs-weather-risk Pareto sweep "
        "over the Rotterdam<->Ceyhan corridor, real CMEMS weather."
    ),
    schedule="@daily",
    start_date=datetime(2026, 1, 1),
    catchup=False,  # a missed day should never trigger a burst of ~45-min backfill runs
    default_args={
        "retries": 0,  # see module docstring for why this isn't 1
        "execution_timeout": timedelta(minutes=60),  # safety net above the ~45min worst case
    },
    tags=["navi-opt", "pareto-sweep", "phase5"],
)
def pareto_sweep_rotterdam_ceyhan():
    @task
    def run_sweep(ds: str | None = None) -> str:
        """`ds` is Airflow's own templated execution-date string
        (YYYY-MM-DD), injected automatically for a task that declares it
        as a parameter — used here to give each scheduled run's CSV a
        distinct filename rather than overwriting the same well-known
        path every day the way the interactive script does (that's fine
        for a human re-running it by hand; a batch job's whole point is
        keeping every run's output around to compare)."""
        output_csv = DATA_DIR / f"pareto_frontier_rotterdam_ceyhan_{ds}.csv"

        config = SweepConfig(
            origin_port="rotterdam",
            destination_port="ceyhan",
            digraph_path=DIGRAPH_PATH,
            anchors_path=ANCHORS_PATH,
            output_csv=output_csv,
            voyage_start=VOYAGE_START,
            fetch_window=FETCH_WINDOW,
            bbox=BBOX,
            v_max_knots=V_MAX_KNOTS,
            sweep_n_points=SWEEP_N_POINTS,
            sweep_max_margin=SWEEP_MAX_MARGIN,
            severe_weather_threshold_m=SEVERE_WEATHER_THRESHOLD_M,
        )

        def _on_corridor_start(idx: int, total: int) -> None:
            log.info("Corridor %d/%d: starting epsilon sweep", idx + 1, total)

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
            log.info(
                "epsilon %d/%d = %.1fh -> %s in %.1fs%s",
                idx + 1, SWEEP_N_POINTS, eps, status, elapsed, fuel,
            )

        result = run_pareto_sweep_task(
            config, on_progress=_on_progress, on_corridor_start=_on_corridor_start
        )

        log.info(
            "Pooled %d points, %d on the non-dominated frontier, written to %s",
            len(result.all_points), len(result.frontier), result.output_csv,
        )
        return str(result.output_csv)

    run_sweep()


pareto_sweep_rotterdam_ceyhan()
