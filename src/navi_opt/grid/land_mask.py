"""
Builds a single unified land geometry ("land_union") from Natural Earth's
1:50m land polygons. This is the ground truth used to:

  1. filter H3 ocean cells (centroid-in-ocean test) in h3_ocean_grid.py, and
  2. prune graph edges whose great-circle segment crosses land — narrow
     straits and peninsulas (Dover Strait, Skagerrak/Kattegat, Bosporus,
     Gibraltar) where two ocean-centroid cells can still have an
     illegal line between them. That edge-level check happens in
     Phase 2's graph_builder.py; this module only supplies the geometry.

The unary_union is expensive to compute (tens of thousands of coastline
vertices) so it is cached to disk after the first build.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.prepared import prep

from navi_opt.grid.download_natural_earth import SHP_PATH

CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "processed" / "land_union.pkl"


def build_land_union(shp_path: Path = SHP_PATH, use_cache: bool = True) -> BaseGeometry:
    """Return the unified (Multi)Polygon of all land area.

    Raises FileNotFoundError with a clear instruction if the Natural Earth
    shapefile hasn't been downloaded yet (run download_natural_earth.py first).
    """
    if use_cache and CACHE_PATH.exists():
        with open(CACHE_PATH, "rb") as f:
            return pickle.load(f)

    if not shp_path.exists():
        raise FileNotFoundError(
            f"Natural Earth land shapefile not found at {shp_path}.\n"
            "Run `python -m navi_opt.grid.download_natural_earth` first."
        )

    gdf = gpd.read_file(shp_path)
    land_union = unary_union(gdf.geometry.values)

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(land_union, f)

    return land_union


def prepared_land(land_union: BaseGeometry):
    """Wrap land_union in a shapely `prepared` geometry for fast repeated
    contains()/intersects() checks — this matters a lot here, since both
    the H3 ocean-cell filter (tens of thousands of point checks) and the
    Phase 2 edge-pruning step (one intersects() check per candidate edge)
    call this in a tight loop."""
    return prep(land_union)


def is_ocean_point(lon: float, lat: float, prepared_land_union) -> bool:
    """True if (lon, lat) does NOT fall inside the (prepared) land union.

    `prepared_land_union` should be the result of prepared_land(), not the
    raw geometry — passing the raw geometry still works (shapely dispatches
    .contains the same way) but is much slower under repeated calls.
    """
    return not prepared_land_union.contains(Point(lon, lat))
