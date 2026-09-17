"""
Downloads Natural Earth's 1:50m land polygons (physical vectors) — the real,
public coastline data used to build the ocean/land mask.

Source: https://www.naturalearthdata.com/downloads/50m-physical-vectors/
Official CDN mirror used below: https://naciscdn.org/naturalearth/50m/physical/ne_50m_land.zip

Run once (from the project root, with the conda env active):
    python -m navi_opt.grid.download_natural_earth
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import requests

NE_LAND_URL = "https://naciscdn.org/naturalearth/50m/physical/ne_50m_land.zip"

# data/raw/natural_earth/  (grid -> navi_opt -> src -> project root, 3 levels up)
RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "natural_earth"
SHP_PATH = RAW_DIR / "ne_50m_land.shp"


def download_land_polygons(dest_dir: Path = RAW_DIR, force: bool = False) -> Path:
    """Download and extract the Natural Earth land shapefile if not already present."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    if SHP_PATH.exists() and not force:
        print(f"Already downloaded: {SHP_PATH}")
        return SHP_PATH

    print(f"Downloading {NE_LAND_URL} ...")
    resp = requests.get(NE_LAND_URL, timeout=120)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(dest_dir)

    if not SHP_PATH.exists():
        raise RuntimeError(
            f"Extraction finished but {SHP_PATH} was not found — "
            "the zip layout may have changed upstream. Check "
            f"{dest_dir} for the actual extracted filename."
        )

    print(f"Extracted to {dest_dir}")
    return SHP_PATH


if __name__ == "__main__":
    download_land_polygons()
