"""
Snaps each port's raw (lon, lat) coordinate to the nearest NAVIGABLE H3
ocean cell in a given ocean-cell set.

Why this exists: a port terminal/jetty sits right at the coastline, so its
direct H3 cell (h3.latlng_to_cell) is very often classified as land by the
grid's centroid-in-ocean filter (see h3_ocean_grid.py). Routing needs to
start/end at a cell that's actually IN the ocean graph, so every port used
as a voyage origin/destination must be snapped to its nearest ocean cell
before Phase 2's pathfinder can use it.

Distance is geodesic (pyproj.Geod), consistent with the rest of the
project's distance calculations — not a raw haversine approximation.
"""
from __future__ import annotations

from pathlib import Path

import h3
import pandas as pd
from pyproj import Geod

from navi_opt.grid.h3_ocean_grid import DEFAULT_RESOLUTION, cell_centroid
from navi_opt.grid.ports import Port, load_ports

GEOD = Geod(ellps="WGS84")

CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "processed" / "port_h3_anchors.parquet"

# Give up and raise past this many grid rings. A port needing a wider search
# than this almost certainly means the ocean-cell set doesn't actually cover
# that port's region (wrong bbox passed to generate_ocean_cells_in_bbox, or
# a resolution mismatch) — not that the nearest water is genuinely this far.
MAX_RING = 6


class PortAnchorError(RuntimeError):
    """Raised when a port cannot be snapped to any ocean cell within MAX_RING."""


def _nearest_ocean_cell(port: Port, ocean_cells: set[str], resolution: int) -> str:
    direct_cell = h3.latlng_to_cell(port.lat, port.lon, resolution)
    if direct_cell in ocean_cells:
        return direct_cell

    for k in range(2, MAX_RING + 1):
        candidates = [c for c in h3.grid_disk(direct_cell, k) if c in ocean_cells]
        if not candidates:
            continue

        best_cell = None
        best_dist_m = float("inf")
        for cell in candidates:
            c_lon, c_lat = cell_centroid(cell)
            _, _, dist_m = GEOD.inv(port.lon, port.lat, c_lon, c_lat)
            if dist_m < best_dist_m:
                best_dist_m = dist_m
                best_cell = cell
        return best_cell

    raise PortAnchorError(
        f"Port '{port.key}' ({port.name}) has no ocean cell within "
        f"{MAX_RING} grid rings at resolution {resolution}. This usually "
        "means the ocean-cell set doesn't cover this port's region — check "
        "the bbox passed to generate_ocean_cells_in_bbox(), or that "
        "`resolution` here matches the resolution the ocean_cells set was "
        "actually built with."
    )


def snap_ports_to_ocean_cells(
    ports: dict[str, Port],
    ocean_cells: set[str],
    resolution: int = DEFAULT_RESOLUTION,
) -> dict[str, str]:
    """Return {port_key: h3_cell} for every port, snapped to the nearest
    navigable ocean cell.

    Raises PortAnchorError for any port that can't be snapped within
    MAX_RING — deliberately not skipped or silenced, since a silently
    missing port anchor produces an unreachable graph origin/destination
    that is much harder to debug once it's buried inside Phase 2's
    pathfinder than it is right here.
    """
    return {key: _nearest_ocean_cell(port, ocean_cells, resolution) for key, port in ports.items()}


def save_anchors(anchors: dict[str, str], path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([{"port_key": k, "h3_cell": v} for k, v in anchors.items()])
    df.to_parquet(path, index=False)


def load_anchors(path: Path = CACHE_PATH) -> dict[str, str]:
    df = pd.read_parquet(path)
    return dict(zip(df["port_key"], df["h3_cell"]))


def build_and_cache_anchors(
    ocean_cells: set[str],
    ports: dict[str, Port] | None = None,
    resolution: int = DEFAULT_RESOLUTION,
    path: Path = CACHE_PATH,
) -> dict[str, str]:
    """Convenience entry point: load ports if not given, snap them all to
    `ocean_cells`, cache the result to disk, and return the mapping.

    `ocean_cells` has no default on purpose — it must be built once via
    generate_ocean_cells_in_bbox() over a region that actually covers every
    port you intend to route between, and the caller should be deliberate
    about that region rather than this function silently assuming one.
    """
    if ports is None:
        ports = load_ports()

    anchors = snap_ports_to_ocean_cells(ports, ocean_cells, resolution)
    save_anchors(anchors, path)
    return anchors
