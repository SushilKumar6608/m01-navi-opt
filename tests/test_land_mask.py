"""
Tests land_mask against a small synthetic land polygon rather than the full
Natural Earth dataset, so the suite runs fast and without requiring the
~10MB shapefile download first. The real Natural Earth data is exercised
manually via download_natural_earth.py / build_land_union() during actual
development, not in CI.
"""
from shapely.geometry import Polygon

from navi_opt.grid.land_mask import is_ocean_point, prepared_land

SQUARE_LAND = Polygon([(10, 50), (11, 50), (11, 51), (10, 51)])


def test_point_inside_land_is_not_ocean():
    prep = prepared_land(SQUARE_LAND)
    assert is_ocean_point(10.5, 50.5, prep) is False


def test_point_outside_land_is_ocean():
    prep = prepared_land(SQUARE_LAND)
    assert is_ocean_point(5.0, 5.0, prep) is True


def test_point_on_boundary_behaves_consistently():
    # shapely's contains() excludes the boundary itself, so a point exactly
    # on the edge counts as ocean. Documented here so the behavior is a
    # known, tested contract rather than an accidental edge case.
    prep = prepared_land(SQUARE_LAND)
    assert is_ocean_point(10.0, 50.5, prep) is True
