"""Phase 7b: response models for the read-only API.

Every figure here is a model output on synthetic vessel data and a simulated
demo clock — the models carry that context (`positions_simulated`, `injected`,
`basis`) so a client can't show a number without it.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field


class Health(BaseModel):
    status: str = "ok"


class FrontierPoint(BaseModel):
    corridor: int = Field(description="1-based corridor number")
    epsilon_hours: float
    transit_hours: float
    fuel_tonnes: float
    weather_risk_m_hours: float
    severe_weather_hours: Optional[float] = Field(
        None, description="null when the sweep file predates this column (NOT zero)")
    success: bool = Field(description="false = solve stopped at its iteration/time cap: usable, not proven optimal")
    on_frontier: bool


class FrontierSummaryModel(BaseModel):
    n_points: int
    n_frontier: int
    n_unproven: int
    n_corridors: int
    fuel_range_t: Optional[Tuple[float, float]] = Field(None, description="over frontier points")
    duration_range_h: Optional[Tuple[float, float]] = None


class FrontierResponse(BaseModel):
    source_file: str
    severe_threshold_m: Optional[float] = None
    summary: FrontierSummaryModel
    points: List[FrontierPoint]


class Comparison(BaseModel):
    basis: str = Field("remaining_legs", description="always like-for-like: the same remaining legs, old plan vs new")
    n_legs: int
    old_fuel_tonnes: float
    new_fuel_tonnes: float
    fuel_delta_tonnes: float
    old_transit_hours: float
    new_transit_hours: float
    transit_delta_hours: float


class ReoptimizationStepModel(BaseModel):
    reoptimized_at: str
    trigger_u: str
    trigger_v: str
    legs_resolved: int
    total_fuel_tonnes: float
    total_transit_hours: float
    solved_to_optimum: bool
    injected: bool = Field(description="true = the triggering weather event was a labelled test injection")
    comparison: Optional[Comparison] = Field(None, description="null if the previous plan's tail could not be matched")


class VoyageSummary(BaseModel):
    voyage_id: str
    status: str = Field(description="sailing | arrived")
    vessel_key: str
    cargo_condition: str
    origin: str
    destination: str
    departed_at: datetime
    deadline_hours: float
    elapsed_hours: float
    legs_sailed: int
    total_legs: int
    progress: float
    reoptimizations: int
    positions_simulated: bool = True


class VoyageDetail(VoyageSummary):
    original_total_fuel_tonnes: float
    original_total_transit_hours: float
    latest_plan_legs: int = Field(description="legs covered by the latest committed plan")
    latest_plan_is_partial: bool = Field(
        description="true after a reoptimization: the latest plan covers only the legs that were still ahead "
                    "then, so its totals must NOT be compared with original_total_* (use steps[].comparison)")
    latest_plan_fuel_tonnes: float
    latest_plan_transit_hours: float
    steps: List[ReoptimizationStepModel]
    demo_clock_note: str


class VoyagesResponse(BaseModel):
    voyages: List[VoyageSummary]
    kafka_notes: List[str] = Field(default_factory=list, description="partial/failed read warnings, if any")


class LegModel(BaseModel):
    leg: int
    u: str
    v: str
    start_hours: float
    end_hours: float
    speed_knots: float
    wave_height_m: float
    headwind_knots: float
    fuel_tonnes: float


class ProfileResponse(BaseModel):
    voyage_id: str
    which: str
    legs: List[LegModel]


class RouteResponse(BaseModel):
    voyage_id: str
    lats: List[float]
    lons: List[float]
    current_index: int
    trigger_edge: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None
    positions_simulated: bool = True


class WeatherEvent(BaseModel):
    u: str
    v: str
    sampled_at: Optional[str] = None
    previous_wave_height_m: Optional[float] = None
    new_wave_height_m: Optional[float] = None
    injected: bool = False


class WeatherEventsResponse(BaseModel):
    events: List[WeatherEvent]
