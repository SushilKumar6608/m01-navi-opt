"""
Vessel fuel-consumption model: cubic speed-fuel curve, augmented by wave
height and headwind resistance terms, scaled by cargo condition
(laden/ballast). This is the cost function Phase 3's speed-profile solver
will optimize over; here it's just the physics, decoupled from any
particular edge/graph/weather-lookup mechanism so it's independently
testable.

    F(v, H, W_headwind) = a*v^3 + b*v^2*H + c*v*max(W_headwind, 0) + d   [tonnes/day]

    v            = vessel speed, knots
    H            = significant wave height, meters
    W_headwind   = the component of wind speed directly opposing the
                   vessel's direction of travel, knots (see
                   relative_headwind_component() for why tailwind is
                   clipped to zero rather than modeled as reducing
                   resistance)

Output is scaled by cargo_condition_factor (laden/ballast, from
vessel_profiles.yaml) and converted tonnes/day -> tonnes/hour via
hourly_conversion_factor — read from that same YAML, never hardcoded as a
bare /24 (see the YAML's own units: block and findings.md for why this
matters: a silent day/hour mismatch is exactly the kind of bug that
produces a plausible-looking but wrong cost function).
"""
from __future__ import annotations

import math

from navi_opt.weather.vessel_profiles import (
    CargoCondition,
    VesselProfile,
    load_hourly_conversion_factor,
)


def wind_uv_to_speed_direction(eastward: float, northward: float) -> tuple[float, float]:
    """Convert a (eastward_wind, northward_wind) vector — CMEMS's native
    component form — to (speed, direction_blowing_toward_deg).

    Direction is a compass bearing (0=North, 90=East, clockwise), chosen
    to match pyproj.Geod's forward-azimuth convention (confirmed: due
    east -> 90.0, due north -> 0.0) so it can be compared directly against
    an edge's travel bearing without a second conversion.
    """
    speed = math.hypot(eastward, northward)
    direction = math.degrees(math.atan2(eastward, northward)) % 360.0
    return speed, direction


def relative_headwind_component(
    vessel_heading_deg: float, wind_blowing_toward_deg: float, wind_speed: float
) -> float:
    """Component of wind speed directly opposing the vessel's travel
    direction. Positive = headwind (adds resistance). Zero for a pure
    crosswind or any net tailwind — NOT negative.

    This clip-at-zero is a deliberate simplification, not an oversight:
    the fuel curve's calm-water cubic term (a*v^3) already represents
    baseline resistance, and a genuine tailwind-reduces-drag effect is a
    second-order correction this synthetic-but-literature-grounded model
    doesn't attempt. Modeling it would need real added-resistance-in-waves
    curves broken out by relative wind angle, which no public source
    provides at the fidelity this project can honestly claim. Documented
    here rather than silently baked into the sign of a subtraction.
    """
    vessel_dx = math.sin(math.radians(vessel_heading_deg))
    vessel_dy = math.cos(math.radians(vessel_heading_deg))
    wind_dx = wind_speed * math.sin(math.radians(wind_blowing_toward_deg))
    wind_dy = wind_speed * math.cos(math.radians(wind_blowing_toward_deg))

    # Component of the wind vector along the vessel's direction of travel.
    # Negative means the wind opposes travel, i.e. a headwind.
    along_travel = wind_dx * vessel_dx + wind_dy * vessel_dy
    headwind = -along_travel
    return max(headwind, 0.0)


def fuel_rate_tonnes_per_day(
    speed_knots: float,
    wave_height_m: float,
    headwind_knots: float,
    vessel: VesselProfile,
    cargo_condition: CargoCondition,
) -> float:
    """F(v, H, W_headwind), scaled by cargo condition.

    Raises ValueError for a negative wave height or headwind (use
    relative_headwind_component() to clip a raw wind vector first — a
    negative value reaching here is a caller bug, not a valid input) and
    for a speed outside the vessel's speed_bounds_knots.
    """
    if wave_height_m < 0:
        raise ValueError(f"wave_height_m must be >= 0, got {wave_height_m}")
    if headwind_knots < 0:
        raise ValueError(
            f"headwind_knots must be >= 0 (use relative_headwind_component() "
            f"to clip a raw tailwind/crosswind to zero first), got {headwind_knots}"
        )

    lo, hi = vessel.speed_bounds_knots
    if not (lo <= speed_knots <= hi):
        raise ValueError(f"speed_knots={speed_knots} outside vessel bounds [{lo}, {hi}]")

    if cargo_condition not in vessel.cargo_condition_factor:
        raise KeyError(f"Unknown cargo_condition '{cargo_condition}' for vessel '{vessel.key}'")

    a, b, c, d = vessel.fuel_curve
    base = (
        a * speed_knots**3
        + b * speed_knots**2 * wave_height_m
        + c * speed_knots * headwind_knots
        + d
    )
    return base * vessel.cargo_condition_factor[cargo_condition]


def fuel_tonnes_for_leg(
    distance_nm: float,
    speed_knots: float,
    wave_height_m: float,
    headwind_knots: float,
    vessel: VesselProfile,
    cargo_condition: CargoCondition,
    hourly_conversion_factor: float | None = None,
) -> tuple[float, float]:
    """Returns (fuel_tonnes, transit_hours) for one graph edge.

    `hourly_conversion_factor` defaults to reading it from
    vessel_profiles.yaml on every call if not supplied — fine for
    one-off use, but pass it explicitly (read once) in a hot loop such as
    Phase 2's A* search, to avoid re-parsing the YAML per edge evaluation.
    """
    if hourly_conversion_factor is None:
        hourly_conversion_factor = load_hourly_conversion_factor()

    transit_hours = distance_nm / speed_knots
    rate_per_day = fuel_rate_tonnes_per_day(
        speed_knots, wave_height_m, headwind_knots, vessel, cargo_condition
    )
    rate_per_hour = rate_per_day * hourly_conversion_factor
    fuel_tonnes = rate_per_hour * transit_hours
    return fuel_tonnes, transit_hours
