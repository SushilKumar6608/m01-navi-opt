"""Phase 7b: pure conversions from the dashboard's data layer to API models,
and a small TTL cache in front of the Kafka reader. No FastAPI in here, so it
is unit-testable on its own."""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from navi_opt.api import schemas as m
from navi_opt.dashboard.data import (
    FrontierSummary,
    ReoptimizationStep,
    VoyageView,
    demo_clock_note,
    profile_leg_frame,
)

ReadTopic = Callable[[str, str], Tuple[List[Dict[str, Any]], Optional[str]]]


class EventStore:
    """Reads topics through `read_topic(bootstrap, topic)` and caches each for
    `ttl_seconds`, so a burst of API requests costs one broker read, not one
    per request. `clock` is injectable for tests."""

    def __init__(self, read_topic: ReadTopic, bootstrap: str, ttl_seconds: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self._read, self._bootstrap, self._ttl, self._clock = read_topic, bootstrap, ttl_seconds, clock
        self._cache: Dict[str, Tuple[float, Tuple[List[Dict[str, Any]], Optional[str]]]] = {}

    def get(self, topic: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        now = self._clock()
        hit = self._cache.get(topic)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        result = self._read(self._bootstrap, topic)
        self._cache[topic] = (now, result)
        return result


def _finite_or_none(x: Any) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) or pd.isna(x) else float(x)


def frontier_points(df: pd.DataFrame) -> List[m.FrontierPoint]:
    return [
        m.FrontierPoint(
            corridor=int(r.corridor), epsilon_hours=float(r.epsilon_hours), transit_hours=float(r.transit_hours),
            fuel_tonnes=float(r.fuel_tonnes), weather_risk_m_hours=float(r.weather_risk_m_hours),
            severe_weather_hours=_finite_or_none(r.severe_weather_hours),
            success=bool(r.success), on_frontier=bool(r.on_frontier),
        )
        for r in df.itertuples(index=False)
    ]


def frontier_summary(s: FrontierSummary) -> m.FrontierSummaryModel:
    return m.FrontierSummaryModel(
        n_points=s.n_points, n_frontier=s.n_frontier, n_unproven=s.n_unproven, n_corridors=s.n_corridors,
        fuel_range_t=s.fuel_range_t, duration_range_h=s.duration_range_h,
    )


def step_model(step: ReoptimizationStep) -> m.ReoptimizationStepModel:
    c = step.comparison
    return m.ReoptimizationStepModel(
        reoptimized_at=str(step.reoptimized_at), trigger_u=step.trigger_u, trigger_v=step.trigger_v,
        legs_resolved=len(step.profile.legs), total_fuel_tonnes=step.profile.total_fuel_tonnes,
        total_transit_hours=step.profile.total_transit_hours, solved_to_optimum=bool(step.profile.success),
        injected=step.injected,
        comparison=None if c is None else m.Comparison(
            n_legs=c.n_legs, old_fuel_tonnes=c.old_fuel_tonnes, new_fuel_tonnes=c.new_fuel_tonnes,
            fuel_delta_tonnes=c.fuel_delta_tonnes, old_transit_hours=c.old_transit_hours,
            new_transit_hours=c.new_transit_hours, transit_delta_hours=c.transit_delta_hours,
        ),
    )


def voyage_summary(v: VoyageView) -> m.VoyageSummary:
    return m.VoyageSummary(
        voyage_id=v.voyage_id, status=v.status, vessel_key=v.vessel_key, cargo_condition=v.cargo_condition,
        origin=v.origin, destination=v.destination, departed_at=v.demo_departed_at,
        deadline_hours=v.deadline_hours, elapsed_hours=v.elapsed_hours,
        legs_sailed=v.total_legs - v.legs_remaining, total_legs=v.total_legs, progress=v.progress,
        reoptimizations=len(v.steps),
    )


def voyage_detail(v: VoyageView) -> m.VoyageDetail:
    return m.VoyageDetail(
        **voyage_summary(v).model_dump(),
        original_total_fuel_tonnes=v.original_profile.total_fuel_tonnes,
        original_total_transit_hours=v.original_profile.total_transit_hours,
        latest_plan_legs=len(v.current_profile.legs),
        latest_plan_is_partial=len(v.current_profile.legs) < v.total_legs,
        latest_plan_fuel_tonnes=v.current_profile.total_fuel_tonnes,
        latest_plan_transit_hours=v.current_profile.total_transit_hours,
        steps=[step_model(s) for s in v.steps], demo_clock_note=demo_clock_note(),
    )


def profile_legs(profile) -> List[m.LegModel]:
    df = profile_leg_frame(profile)
    return [m.LegModel(**{k: (int(r[k]) if k == "leg" else r[k] if k in ("u", "v") else float(r[k]))
                          for k in df.columns}) for r in df.to_dict("records")]
