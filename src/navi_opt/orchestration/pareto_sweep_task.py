"""
Phase 5: Airflow-orchestrated batch entry point for the Phase 4 Pareto sweep.

Extracted from `scripts/run_pareto_sweep.py`'s `main()` into an importable,
parameterized function so the exact same sweep logic can be invoked two
ways: interactively (`scripts/run_pareto_sweep.py`, now a thin CLI wrapper
around this) and on a schedule (`infra/airflow/dags/pareto_sweep_dag.py`,
via a PythonOperator). Nothing about the sweep algorithm itself changes
here — every call into `navi_opt.optimization.pareto` /
`navi_opt.routing.k_shortest` / etc. is identical to what the script
already did. This module adds only the wiring and I/O choices a scheduled
batch run needs that an interactive script didn't:

- Raises (rather than prints-and-returns) on a missing prerequisite cache
  or an unreachable corridor, since a scheduled run has no one watching
  stdout to notice a silent early return — an Airflow task needs a real
  exception to mark itself failed and alert on it. The script wraps this
  function in its own try/except to keep printing the same friendly
  message it always did for interactive use.
- Takes origin/destination ports and every sweep parameter as fields on a
  `SweepConfig` dataclass rather than module-level constants, so a caller
  (the DAG, later a fleet loop) can build and pass one of these instead of
  editing constants in a script. `SweepConfig` bundles them into one
  loggable value deliberately — Airflow's UI shows an operator's kwargs,
  and a dataclass repr is far more readable there than a long argument
  list would be.

Explicitly NOT done here: looping this over more than one origin/
destination pair for a genuine multi-route fleet batch. This project has
exactly one cached real-weather digraph, built by
`scripts/build_real_ocean_graph.py` for the Rotterdam<->Ceyhan bbox
specifically — a real fleet batch needs either a much larger shared ocean
grid or a per-route graph-building step first, which is a real scope
decision for a later phase, not a detail to quietly backfill by looping
this function over hardcoded port pairs that share a graph they weren't
built to route across.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from navi_opt.grid.graph_builder import load_graph
from navi_opt.grid.port_anchors import load_anchors
from navi_opt.optimization.pareto import (
    DEFAULT_SEVERE_WAVE_THRESHOLD_M,
    ParetoPoint,
    ProgressCallback,
    default_epsilon_sweep,
    pareto_frontier,
    sweep_epsilon_constraint,
)
from navi_opt.routing.k_shortest import YenKShortestPaths
from navi_opt.weather.vessel_profiles import load_vessel_profile
from navi_opt.weather.weather_field import make_weather_lookup, open_weather_field_remote

#: Called as `(corridor_index, num_corridors)` immediately before that
#: corridor's epsilon sweep starts — corridor-level narration, a level
#: above `ProgressCallback`'s per-epsilon granularity. Optional and
#: side-effect-only, same contract as `ProgressCallback`.
CorridorStartCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class SweepConfig:
    """Every parameter `run_pareto_sweep_task()` needs for one origin/
    destination pair's sweep. See module docstring for why this is a
    dataclass rather than a long argument list."""

    origin_port: str
    destination_port: str
    digraph_path: Path
    anchors_path: Path
    output_csv: Path
    voyage_start: datetime
    fetch_window: timedelta
    bbox: Dict[str, float]
    v_max_knots: float = 16.0
    vessel_key: str = "mr2_product_tanker"
    cargo_condition: str = "laden"
    k_corridors: int = 3
    sweep_n_points: int = 5
    sweep_max_margin: float = 1.6
    severe_weather_threshold_m: float = DEFAULT_SEVERE_WAVE_THRESHOLD_M


@dataclass(frozen=True)
class SweepResult:
    """Everything `run_pareto_sweep_task()` produced for one origin/
    destination pair: the CSV path it wrote (same file `all_points` and
    `frontier` are a copy of), plus the in-memory points themselves so a
    caller — the interactive script's frontier-table printout, a future
    DAG task that wants to push results into Postgres instead of/as well
    as a CSV — doesn't have to re-read its own just-written file to get
    them back."""

    output_csv: Path
    all_points: List[ParetoPoint]
    frontier: List[ParetoPoint]


def run_pareto_sweep_task(
    config: SweepConfig,
    on_progress: Optional[ProgressCallback] = None,
    on_corridor_start: Optional[CorridorStartCallback] = None,
) -> SweepResult:
    """Runs the full Phase 4 sweep-and-frontier pipeline for ONE origin/
    destination pair, writes the result CSV (same columns
    `run_pareto_sweep.py` always wrote, including the Phase 4 addendum's
    `severe_weather_hours_ge_<threshold>m` column), and returns a
    `SweepResult` carrying the CSV's path alongside the points themselves.

    Raises `FileNotFoundError` if `config.digraph_path` / `config.anchors_path`
    don't exist (`scripts/build_real_ocean_graph.py` must run first) and
    `ValueError` if no path is found between the two ports on the cached
    digraph. Both propagate to the caller rather than being swallowed —
    see module docstring for why that matters for a scheduled run.
    """
    if not config.digraph_path.exists() or not config.anchors_path.exists():
        raise FileNotFoundError(
            f"Missing {config.digraph_path} or {config.anchors_path} — run "
            f"scripts/build_real_ocean_graph.py first, this task reuses its cache."
        )

    digraph = load_graph(path=config.digraph_path)
    anchors = load_anchors(path=config.anchors_path)

    weather_field = open_weather_field_remote(
        start=config.voyage_start,
        end=config.voyage_start + config.fetch_window,
        **config.bbox,
    )
    weather_lookup = make_weather_lookup(digraph, weather_field, config.voyage_start)

    def _calm_water_cost_evaluator(u, v, current_t):
        hours = digraph[u][v]["dist_nm"] / config.v_max_knots
        return hours, hours

    if config.origin_port not in anchors or config.destination_port not in anchors:
        raise ValueError(
            f"Port anchor missing for {config.origin_port!r} or "
            f"{config.destination_port!r} — check {config.anchors_path}."
        )
    origin_cell = anchors[config.origin_port]
    destination_cell = anchors[config.destination_port]

    yen = YenKShortestPaths(
        digraph, _calm_water_cost_evaluator, v_max_knots=config.v_max_knots, enforce_diversity=True
    )
    corridors = yen.find_k_paths(
        origin_cell, destination_cell, k=config.k_corridors, start_time_hours=0.0
    )
    if not corridors:
        raise ValueError(
            f"No path found between {config.origin_port!r} and "
            f"{config.destination_port!r} on the cached digraph — investigate "
            f"before scheduling this regularly."
        )

    vessel = load_vessel_profile(config.vessel_key)

    all_points = []
    for i, corridor in enumerate(corridors):
        if on_corridor_start is not None:
            on_corridor_start(i, len(corridors))
        path = corridor["path"]
        epsilons = default_epsilon_sweep(
            path, digraph, v_max_knots=config.v_max_knots,
            n_points=config.sweep_n_points, max_margin=config.sweep_max_margin,
        )
        points = sweep_epsilon_constraint(
            i, path, digraph, vessel, config.cargo_condition, weather_lookup, epsilons,
            on_progress=on_progress, severe_weather_threshold_m=config.severe_weather_threshold_m,
        )
        all_points.extend(points)

    frontier = pareto_frontier(all_points)
    frontier_set = set(id(p) for p in frontier)

    config.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(config.output_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "corridor_index", "epsilon_hours", "transit_hours", "fuel_tonnes",
            "weather_risk_m_hours",
            f"severe_weather_hours_ge_{config.severe_weather_threshold_m:g}m",
            "success", "on_frontier",
        ])
        for p in all_points:
            writer.writerow([
                p.corridor_index, f"{p.epsilon_hours:.3f}", f"{p.transit_hours:.3f}",
                f"{p.fuel_tonnes:.3f}", f"{p.weather_risk_m_hours:.3f}",
                f"{p.severe_weather_hours:.3f}", p.success,
                id(p) in frontier_set,
            ])
    return SweepResult(output_csv=config.output_csv, all_points=all_points, frontier=frontier)
