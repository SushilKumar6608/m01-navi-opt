"""
Partitions a region of interest (e.g. North Atlantic + Mediterranean trade
lanes, or the whole globe if you really want that) into an H3 hexagonal
grid, keeping only the cells whose centroid falls in open ocean.

Region-scoped rather than whole-globe by default: a tanker-routing use case
only ever needs the operating area around its actual trade lanes, and
scoping the grid keeps cell counts (and therefore graph size in Phase 2)
tractable at finer resolutions. h3.polygon_to_cells() does the region walk
natively in H3's own indexing, so it is antimeridian-safe without any extra
handling on our side (see project findings.md for why that matters for
trans-Pacific lanes).

NOTE — H3 coordinate convention: h3-py takes and returns (lat, lng), NOT
(lon, lat). Everywhere else in this project (shapely, GeoJSON, pyproj)
uses (lon, lat). This module is the boundary between the two conventions:
functions here accept/return (lon, lat) like the rest of the codebase, and
do the (lat, lng) <-> (lon, lat) swap internally so that mistake can't leak
downstream.
"""
from __future__ import annotations

from typing import Iterable

import h3
from shapely.geometry.base import BaseGeometry

from navi_opt.grid.land_mask import build_land_union, is_ocean_point, prepared_land

DEFAULT_RESOLUTION = 3  # ~ 105 km average hex edge length at res 3 (see H3 docs / Phase 1 findings)


def _bbox_to_latlngpoly(west: float, south: float, east: float, north: float) -> h3.LatLngPoly:
    """Build an h3.LatLngPoly from a simple (west, south, east, north) bbox,
    in the (lat, lng) vertex order h3-py expects."""
    return h3.LatLngPoly(
        [
            (south, west),
            (south, east),
            (north, east),
            (north, west),
        ]
    )


def generate_ocean_cells_in_bbox(
    west: float,
    south: float,
    east: float,
    north: float,
    resolution: int = DEFAULT_RESOLUTION,
    land_union: BaseGeometry | None = None,
) -> set[str]:
    """Return H3 cell indexes within the bbox whose centroid is NOT on land.

    lon/lat args are in degrees, standard (west, south, east, north) order.
    A bbox crossing the antimeridian (west > east, e.g. west=170, east=-170
    for a trans-Pacific box) is NOT handled by this convenience wrapper —
    split it into two bbox calls and union the results, since a single
    h3.LatLngPoly with west > east would just describe the wrong (inverted)
    region rather than wrapping around.
    """
    if west > east:
        raise ValueError(
            "west > east: this looks like an antimeridian-crossing bbox "
            "(e.g. west=170, east=-170). Split it into two bbox calls "
            "(west..180 and -180..east) and union the results."
        )

    if land_union is None:
        land_union = build_land_union()
    prepared = prepared_land(land_union)

    poly = _bbox_to_latlngpoly(west, south, east, north)
    candidate_cells = h3.polygon_to_cells(poly, resolution)

    ocean_cells: set[str] = set()
    for cell in candidate_cells:
        lat, lon = h3.cell_to_latlng(cell)
        if is_ocean_point(lon, lat, prepared):
            ocean_cells.add(cell)

    return ocean_cells


def cell_centroid(cell: str) -> tuple[float, float]:
    """Return (lon, lat) in degrees for an H3 cell index."""
    lat, lon = h3.cell_to_latlng(cell)
    return lon, lat


def neighbors(cell: str) -> Iterable[str]:
    """H3 grid-disk neighbors (k=1) of `cell`, excluding the cell itself.

    This is unfiltered — it returns ALL grid neighbors, including any that
    may be on land or across a land-crossing strait. Phase 2's graph_builder
    is responsible for filtering this down to the ocean cell set and running
    the land-bridge intersects() check before adding an edge.
    """
    return (c for c in h3.grid_disk(cell, 1) if c != cell)
