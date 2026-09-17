"""Loads the curated real-port list from config/ports.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import yaml

PORTS_YAML = Path(__file__).resolve().parents[3] / "config" / "ports.yaml"


class Port(NamedTuple):
    key: str
    name: str
    country: str
    lon: float
    lat: float


def load_ports(path: Path = PORTS_YAML) -> dict[str, Port]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    ports: dict[str, Port] = {}
    for key, p in data["ports"].items():
        ports[key] = Port(key=key, name=p["name"], country=p["country"], lon=p["lon"], lat=p["lat"])
    return ports
