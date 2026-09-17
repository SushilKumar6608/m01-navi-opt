import pytest
from shapely.geometry import Polygon

from navi_opt.grid.h3_ocean_grid import DEFAULT_RESOLUTION, generate_ocean_cells_in_bbox
from navi_opt.grid.port_anchors import (
    PortAnchorError,
    load_anchors,
    save_anchors,
    snap_ports_to_ocean_cells,
)
from navi_opt.grid.ports import Port

# A "coastline": land fills the western half of the test bbox (lon 8-10.5),
# ocean is the eastern half (lon 10.5-13). Ports are placed ON the coastline
# so their direct H3 cell is guaranteed to land on land.
COASTLINE_LAND = Polygon([(8, 48), (10.5, 48), (10.5, 53), (8, 53)])
BBOX = dict(west=8, south=48, east=13, north=53)


@pytest.fixture(scope="module")
def ocean_cells():
    return generate_ocean_cells_in_bbox(land_union=COASTLINE_LAND, resolution=4, **BBOX)


def test_port_right_on_coastline_snaps_to_an_ocean_cell(ocean_cells):
    # Sits exactly on the land/ocean boundary — its direct H3 cell should be
    # land, forcing the ring-expansion path to actually run, not just the
    # direct-hit happy path.
    port = Port(key="test_port", name="Test Port", country="XX", lon=10.5, lat=50.5)
    anchors = snap_ports_to_ocean_cells({"test_port": port}, ocean_cells, resolution=4)

    assert "test_port" in anchors
    assert anchors["test_port"] in ocean_cells


def test_port_far_outside_covered_region_raises_port_anchor_error(ocean_cells):
    # Nowhere near the test bbox at all (ocean_cells only covers lon 8-13,
    # lat 48-53) — no amount of ring expansion within MAX_RING will ever
    # reach it, and it should fail loudly rather than silently return
    # nothing. This is also the realistic trigger for this error in
    # practice: a port outside the region the ocean-cell set was built for.
    port = Port(key="uncovered", name="Uncovered", country="XX", lon=-30.0, lat=0.0)
    with pytest.raises(PortAnchorError):
        snap_ports_to_ocean_cells({"uncovered": port}, ocean_cells, resolution=4)


def test_port_already_in_open_ocean_snaps_to_its_own_direct_cell(ocean_cells):
    # Clearly out in open water — the direct cell should already be in
    # ocean_cells, so this exercises the no-expansion-needed path.
    port = Port(key="open_water", name="Open Water", country="XX", lon=12.0, lat=50.5)
    anchors = snap_ports_to_ocean_cells({"open_water": port}, ocean_cells, resolution=4)
    assert anchors["open_water"] in ocean_cells


def test_save_and_load_anchors_roundtrip(tmp_path, ocean_cells):
    port = Port(key="open_water", name="Open Water", country="XX", lon=12.0, lat=50.5)
    anchors = snap_ports_to_ocean_cells({"open_water": port}, ocean_cells, resolution=4)

    cache_file = tmp_path / "anchors.parquet"
    save_anchors(anchors, path=cache_file)
    loaded = load_anchors(path=cache_file)

    assert loaded == anchors
