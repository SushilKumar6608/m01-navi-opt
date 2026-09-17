"""Loads vessel fuel-curve profiles from config/vessel_profiles.yaml.

Kept separate from resistance_model.py (which does the actual fuel-cost
math) the same way ports.py is kept separate from port_anchors.py — a
pure config loader, single responsibility.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, NamedTuple

import yaml

VESSEL_PROFILES_YAML = Path(__file__).resolve().parents[3] / "config" / "vessel_profiles.yaml"

CargoCondition = Literal["laden", "ballast"]


class FuelCurve(NamedTuple):
    a: float
    b: float
    c: float
    d: float


class VesselProfile(NamedTuple):
    key: str
    dwt_tonnes: float
    design_speed_knots: float
    fuel_curve: FuelCurve
    cargo_condition_factor: dict[CargoCondition, float]
    speed_bounds_knots: tuple[float, float]


def load_units(path: Path = VESSEL_PROFILES_YAML) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["units"]


def load_hourly_conversion_factor(path: Path = VESSEL_PROFILES_YAML) -> float:
    """The single source of truth for tonnes/day -> tonnes/hour conversion
    (= 1/24). Read from the YAML, never hardcoded — see the YAML's own
    `units:` block comment and findings.md for why this matters."""
    return load_units(path)["hourly_conversion_factor"]


def load_eca_price_premium(path: Path = VESSEL_PROFILES_YAML) -> float:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["eca_compliance"]["price_premium_usd_per_tonne"]


def load_vessel_profiles(path: Path = VESSEL_PROFILES_YAML) -> dict[str, VesselProfile]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    profiles: dict[str, VesselProfile] = {}
    for key, v in data["vessels"].items():
        profiles[key] = VesselProfile(
            key=key,
            dwt_tonnes=v["dwt_tonnes"],
            design_speed_knots=v["design_speed_knots"],
            fuel_curve=FuelCurve(**v["fuel_curve"]),
            cargo_condition_factor=dict(v["cargo_condition_factor"]),
            speed_bounds_knots=tuple(v["speed_bounds_knots"]),
        )
    return profiles


def load_vessel_profile(key: str, path: Path = VESSEL_PROFILES_YAML) -> VesselProfile:
    profiles = load_vessel_profiles(path)
    if key not in profiles:
        raise KeyError(f"Unknown vessel profile '{key}'. Available: {sorted(profiles)}")
    return profiles[key]
