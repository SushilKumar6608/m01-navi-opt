"""
Phase 7: the dashboard's data layer — everything the Streamlit app shows,
computed by plain functions with no Streamlit, no Kafka client and no
plotting in them, so it is unit-testable offline (same split every earlier
phase uses: the thin network/UI glue stays out of the tested core).

Three jobs:

1. Load the Phase 4/5 Pareto sweep CSV, tolerating the older column layout
   that predates the severe-weather-hours column.
2. Turn the raw JSON events read off the Kafka topics (`voyage-events`,
   `reoptimization-results`, `weather-updates`) into one `VoyageView` per
   voyage — current position, latest committed profile, reoptimization
   history — reusing `voyage_registry`'s tested position logic rather than
   reimplementing it.
3. Derive the numbers a reader will actually compare. The important one is
   `compare_remaining()`: the first real Phase 6 run printed "fuel 369.7 ->
   235.5t" for a voyage that was merely 37% sailed (whole voyage vs. only the
   legs still ahead), which read like a 36% saving and was nothing of the
   kind. The dashboard therefore never compares a whole profile against a
   remaining one: it DERIVES the like-for-like baseline itself from the
   previous profile instead of trusting a field on the event, so it is also
   correct for results published before `old_remaining_*` existed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import networkx as nx
import pandas as pd

from navi_opt.optimization.speed_profile import SpeedProfileResult
from navi_opt.streaming.serialization import speed_profile_result_from_dict
from navi_opt.streaming.voyage_registry import (
    DEMO_TIME_SCALE,
    Voyage,
    current_node_and_remaining_path,
    elapsed_hours,
)

REQUIRED_FRONTIER_COLUMNS = (
    "corridor_index", "epsilon_hours", "transit_hours", "fuel_tonnes",
    "weather_risk_m_hours", "success", "on_frontier",
)
_SEVERE_PREFIX = "severe_weather_hours"


# --------------------------------------------------------------------------
# 1. Pareto sweep CSV
# --------------------------------------------------------------------------


def _as_bool(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series
    return series.astype(str).str.strip().str.lower().isin({"true", "1", "yes"})


def load_frontier(path: Path) -> pd.DataFrame:
    """Reads the sweep CSV `pareto_sweep_task.run_pareto_sweep_task()` writes.

    Returns a frame with normalised columns: `corridor` (1-based, for
    display), `epsilon_hours`, `transit_hours`, `fuel_tonnes`,
    `weather_risk_m_hours`, `severe_weather_hours` (NaN when the file predates
    that column — never silently zero, which would claim "no severe weather"),
    `success`, `on_frontier`, plus `severe_threshold_m` parsed from the
    column name when present.

    Raises `FileNotFoundError` for a missing file and `ValueError` naming the
    missing columns for a malformed one — a dashboard silently rendering an
    empty chart for a wrong file would be worse than an error.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run scripts/run_pareto_sweep.py first.")
    raw = pd.read_csv(path)
    missing = [c for c in REQUIRED_FRONTIER_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(f"{path.name} is missing required column(s) {missing}; got {list(raw.columns)}")

    df = pd.DataFrame({
        "corridor": raw["corridor_index"].astype(int) + 1,
        "epsilon_hours": raw["epsilon_hours"].astype(float),
        "transit_hours": raw["transit_hours"].astype(float),
        "fuel_tonnes": raw["fuel_tonnes"].astype(float),
        "weather_risk_m_hours": raw["weather_risk_m_hours"].astype(float),
        "success": _as_bool(raw["success"]),
        "on_frontier": _as_bool(raw["on_frontier"]),
    })
    severe_cols = [c for c in raw.columns if c.startswith(_SEVERE_PREFIX)]
    if severe_cols:
        df["severe_weather_hours"] = raw[severe_cols[0]].astype(float)
        threshold = severe_cols[0].replace(_SEVERE_PREFIX + "_ge_", "").rstrip("m")
        try:
            df.attrs["severe_threshold_m"] = float(threshold)
        except ValueError:
            df.attrs["severe_threshold_m"] = None
    else:
        df["severe_weather_hours"] = float("nan")
        df.attrs["severe_threshold_m"] = None
    return df


@dataclass(frozen=True)
class FrontierSummary:
    n_points: int
    n_frontier: int
    n_unproven: int  # solves that stopped at a time/iteration cap (success=False)
    fuel_range_t: Tuple[float, float]  # over the frontier
    duration_range_h: Tuple[float, float]  # over the frontier
    n_corridors: int


def summarize_frontier(df: pd.DataFrame) -> FrontierSummary:
    front = df[df["on_frontier"]]
    basis = front if len(front) else df
    return FrontierSummary(
        n_points=len(df),
        n_frontier=int(df["on_frontier"].sum()),
        n_unproven=int((~df["success"]).sum()),
        fuel_range_t=(float(basis["fuel_tonnes"].min()), float(basis["fuel_tonnes"].max())) if len(df) else (0.0, 0.0),
        duration_range_h=(float(basis["transit_hours"].min()), float(basis["transit_hours"].max())) if len(df) else (0.0, 0.0),
        n_corridors=int(df["corridor"].nunique()),
    )


# --------------------------------------------------------------------------
# 2. Profiles -> frames / geometry
# --------------------------------------------------------------------------


def profile_leg_frame(profile: SpeedProfileResult) -> pd.DataFrame:
    """One row per leg, with the absolute voyage-timeline start/end hours the
    optimizer assigned (`start_time_hours` carries through reoptimizations —
    see `voyage_registry`'s module docstring), for the speed-profile chart."""
    rows = []
    for i, leg in enumerate(profile.legs):
        rows.append({
            "leg": i,
            "u": leg.u, "v": leg.v,
            "start_hours": leg.departure_time_hours,
            "end_hours": leg.departure_time_hours + leg.transit_hours,
            "speed_knots": leg.speed_knots,
            "wave_height_m": leg.wave_height_m,
            "headwind_knots": leg.headwind_knots,
            "fuel_tonnes": leg.fuel_tonnes,
        })
    return pd.DataFrame(rows, columns=[
        "leg", "u", "v", "start_hours", "end_hours", "speed_knots",
        "wave_height_m", "headwind_knots", "fuel_tonnes",
    ])


def path_coordinates(digraph: nx.DiGraph, path: Sequence[str]) -> Tuple[List[float], List[float]]:
    """(lats, lons) for `path`. Raises `KeyError` naming the node if the graph
    doesn't contain it — a dashboard pointed at the wrong cached graph for a
    voyage should say so, not draw a partial route."""
    lats, lons = [], []
    for node in path:
        if node not in digraph:
            raise KeyError(f"node {node!r} from the voyage path is not in the loaded graph")
        lats.append(float(digraph.nodes[node]["lat"]))
        lons.append(float(digraph.nodes[node]["lon"]))
    return lats, lons


# --------------------------------------------------------------------------
# 3. Like-for-like comparison
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RemainingComparison:
    """The same remaining legs, previous plan vs. new plan."""

    n_legs: int
    old_fuel_tonnes: float
    new_fuel_tonnes: float
    old_transit_hours: float
    new_transit_hours: float

    @property
    def fuel_delta_tonnes(self) -> float:
        return self.new_fuel_tonnes - self.old_fuel_tonnes

    @property
    def transit_delta_hours(self) -> float:
        return self.new_transit_hours - self.old_transit_hours


def compare_remaining(previous: SpeedProfileResult, new: SpeedProfileResult) -> RemainingComparison:
    """Compares `new` (a profile for a SUFFIX of the voyage) against exactly the
    matching tail of `previous` (see module docstring for why). Verifies the
    tail really is the same legs by node ids and raises `ValueError` if not —
    comparing against a mismatched baseline is the exact bug this exists to
    prevent, so it must not pass quietly."""
    n = len(new.legs)
    if n == 0 or n > len(previous.legs):
        raise ValueError(f"cannot compare {n} new leg(s) against {len(previous.legs)} previous leg(s)")
    tail = previous.legs[len(previous.legs) - n:]
    if [(l.u, l.v) for l in tail] != [(l.u, l.v) for l in new.legs]:
        raise ValueError("new profile is not a suffix of the previous profile — refusing to compare")
    return RemainingComparison(
        n_legs=n,
        old_fuel_tonnes=sum(l.fuel_tonnes for l in tail),
        new_fuel_tonnes=sum(l.fuel_tonnes for l in new.legs),
        old_transit_hours=sum(l.transit_hours for l in tail),
        new_transit_hours=sum(l.transit_hours for l in new.legs),
    )


# --------------------------------------------------------------------------
# 4. Voyage views from Kafka events
# --------------------------------------------------------------------------


@dataclass
class ReoptimizationStep:
    """One reoptimization in a voyage's history, in the order it happened."""

    reoptimized_at: str
    trigger_u: str
    trigger_v: str
    profile: SpeedProfileResult
    comparison: Optional[RemainingComparison]  # None if the baseline didn't line up
    injected: bool = False  # True if the matching weather event was a labelled test injection


@dataclass
class VoyageView:
    voyage_id: str
    vessel_key: str
    cargo_condition: str
    voyage_start_time: datetime
    demo_departed_at: datetime
    deadline_hours: float
    original_profile: SpeedProfileResult
    current_profile: SpeedProfileResult
    steps: List[ReoptimizationStep] = field(default_factory=list)
    elapsed_hours: float = 0.0
    current_node: str = ""
    remaining_path: List[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return self.original_profile.path[0]

    @property
    def destination(self) -> str:
        return self.original_profile.path[-1]

    @property
    def status(self) -> str:
        return "arrived" if len(self.remaining_path) < 2 else "sailing"

    @property
    def legs_remaining(self) -> int:
        return max(0, len(self.remaining_path) - 1)

    @property
    def total_legs(self) -> int:
        return len(self.original_profile.path) - 1

    @property
    def progress(self) -> float:
        """Fraction of legs sailed (by leg count — this project's routing is
        node-to-node, so no sub-leg precision is claimed; see
        `voyage_registry.current_node_and_remaining_path`)."""
        if self.total_legs == 0:
            return 1.0
        return 1.0 - self.legs_remaining / self.total_legs


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _injected_lookup(weather_events: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], bool]:
    """(u, v) -> whether the most recent event on that edge was a labelled
    injection (`scripts/inject_weather_event.py`). Matched by edge, not by
    timestamp: a result event doesn't carry its trigger's `sampled_at`, and
    inventing a time-window match would be false precision."""
    out: Dict[Tuple[str, str], bool] = {}
    for ev in sorted(weather_events, key=lambda e: e.get("sampled_at", "")):
        out[(ev["u"], ev["v"])] = bool(ev.get("injected", False))
    return out


def build_voyage_views(
    departures: Iterable[Dict[str, Any]],
    results: Iterable[Dict[str, Any]],
    now: datetime,
    weather_events: Iterable[Dict[str, Any]] = (),
) -> List[VoyageView]:
    """One `VoyageView` per departed voyage, newest departure first.

    Duplicate events (a replayed topic) are collapsed: departures by
    voyage_id, results by (voyage_id, reoptimized_at). A result for a voyage
    that never departed (e.g. the departure topic was trimmed) is ignored —
    there is no baseline to show it against. `now` is a parameter, never read
    internally, for the same reason `voyage_registry.elapsed_hours` takes it.
    """
    injected = _injected_lookup(weather_events)

    departed: Dict[str, Dict[str, Any]] = {}
    for dep in departures:
        departed[dep["voyage_id"]] = dep  # last write wins on a replay

    by_voyage: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for res in results:
        if res["voyage_id"] not in departed:
            continue
        by_voyage.setdefault(res["voyage_id"], {})[res["reoptimized_at"]] = res

    views: List[VoyageView] = []
    for voyage_id, dep in departed.items():
        original = speed_profile_result_from_dict(dep["committed_profile"])
        steps: List[ReoptimizationStep] = []
        previous = original
        for reopt_at in sorted(by_voyage.get(voyage_id, {})):
            res = by_voyage[voyage_id][reopt_at]
            profile = speed_profile_result_from_dict(res["committed_profile"])
            try:
                comparison: Optional[RemainingComparison] = compare_remaining(previous, profile)
            except ValueError:
                comparison = None
            steps.append(ReoptimizationStep(
                reoptimized_at=reopt_at, trigger_u=res["trigger_u"], trigger_v=res["trigger_v"],
                profile=profile, comparison=comparison,
                injected=injected.get((res["trigger_u"], res["trigger_v"]), False),
            ))
            previous = profile

        current = previous
        voyage = Voyage(
            voyage_id=voyage_id, vessel_key=dep["vessel_key"], cargo_condition=dep["cargo_condition"],
            voyage_start_time=_parse_dt(dep["voyage_start_time"]),
            deadline_hours=float(dep["deadline_hours"]),
            demo_departed_at=_parse_dt(dep["demo_departed_at"]),
            committed_profile=current,
        )
        node, remaining = current_node_and_remaining_path(voyage, now)
        views.append(VoyageView(
            voyage_id=voyage_id, vessel_key=voyage.vessel_key, cargo_condition=voyage.cargo_condition,
            voyage_start_time=voyage.voyage_start_time, demo_departed_at=voyage.demo_departed_at,
            deadline_hours=voyage.deadline_hours, original_profile=original, current_profile=current,
            steps=steps, elapsed_hours=elapsed_hours(voyage, now),
            current_node=node, remaining_path=remaining,
        ))

    views.sort(key=lambda v: v.demo_departed_at, reverse=True)
    return views


def demo_clock_note() -> str:
    """The one sentence the UI must show next to any elapsed-time figure, so the
    accelerated demo clock is never mistaken for real voyage tracking."""
    return (
        f"Demo clock: 1 real minute = {DEMO_TIME_SCALE / 60:.0f} simulated voyage hours. "
        f"Positions are simulated from the planned speed profile, not real vessel tracking."
    )
