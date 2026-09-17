import pytest
from shapely.geometry import Polygon

from navi_opt.grid.h3_ocean_grid import (
    cell_centroid,
    generate_ocean_cells_in_bbox,
    neighbors,
)

# Synthetic "land" square strictly inside the test bbox below.
SQUARE_LAND = Polygon([(10, 50), (11, 50), (11, 51), (10, 51)])


def test_no_returned_cell_centroid_falls_inside_land():
    cells = generate_ocean_cells_in_bbox(
        west=8, south=48, east=13, north=53, resolution=4, land_union=SQUARE_LAND
    )
    assert len(cells) > 0
    for cell in cells:
        lon, lat = cell_centroid(cell)
        assert not (10 <= lon <= 11 and 50 <= lat <= 51)


def test_antimeridian_bbox_is_rejected_not_silently_wrong():
    with pytest.raises(ValueError):
        generate_ocean_cells_in_bbox(
            west=170, south=-10, east=-170, north=10, land_union=SQUARE_LAND
        )


def test_neighbors_excludes_self_and_returns_six_for_interior_cell():
    cells = generate_ocean_cells_in_bbox(
        west=8, south=48, east=13, north=53, resolution=4, land_union=SQUARE_LAND
    )
    any_cell = next(iter(cells))
    nbrs = list(neighbors(any_cell))
    assert any_cell not in nbrs
    # Most H3 cells have 6 neighbors; the 12 pentagon cells (globally fixed,
    # rare, and unlikely to land in a small test bbox) have 5 — allow both
    # so this test isn't flaky if a pentagon ever does show up.
    assert len(nbrs) in (5, 6)


def test_cell_centroid_returns_lon_lat_order():
    cells = generate_ocean_cells_in_bbox(
        west=8, south=48, east=13, north=53, resolution=4, land_union=SQUARE_LAND
    )
    any_cell = next(iter(cells))
    lon, lat = cell_centroid(any_cell)
    # bbox was lon 8-13, lat 48-53 — a lon/lat mixup would fail this
    assert 8 <= lon <= 13
    assert 48 <= lat <= 53
