"""
Ingests real wave and wind fields from the Copernicus Marine Service (CMEMS)
for a given bounding box and time window, using the free `copernicusmarine`
Python toolbox. Requires a free CMEMS account — credentials are read from
the environment (COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD in .env),
never hardcoded or committed.

Dataset choices (confirmed against the CMEMS catalogue, Sep 2026):

  Wave height (VHM0), 3-hourly instantaneous, global:
    dataset_id = "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i"
    product:    GLOBAL_ANALYSISFORECAST_WAV_001_027
    variable:   VHM0  (significant height of combined wind waves and swell)

  Wind (eastward/northward components), hourly, global, multi-year
  reprocessed (use this rather than the near-real-time product so the
  historical window matches whatever wave-hindcast period we pull):
    dataset_id = "cmems_obs-wind_glo_phy_my_l4_0.125deg_PT1H"
    product:    WIND_GLO_PHY_L4_MY_012_006

  Variable short names — confirmed 2026-09-17 against the live CMEMS
  catalogue via scripts/describe_cmems_datasets.py (not guessed):
    wave: VHM0  (sea_surface_wave_significant_height)
    wind: eastward_wind, northward_wind  (CF standard names, as expected)

Two ways to get the data, below:

`fetch_wave_field()`/`fetch_wind_field()` call `copernicusmarine.subset()`,
which downloads a local `.nc` file — then something downstream (this
project: `weather_field.open_weather_field()`) has to open it via
netCDF4/h5netcdf, which requires a working local HDF5 install. On a first
real run (Sep 2026) this hit an unresolvable Windows conda HDF5/netCDF4 DLL
conflict — reinstalling netCDF4, then h5netcdf, then force-rebuilding the
whole HDF5 stack from conda-forge each moved the failure to a different
package without fixing it (findings.md has the full sequence). Kept here
for anyone who wants the file cached locally and has a working HDF5 stack.

`open_wave_field()`/`open_wind_field()` call `copernicusmarine.open_dataset()`
instead, which streams the subset directly into memory via zarr/fsspec — a
pure-Python path with NO local netCDF4/HDF5 dependency at all. This is what
`weather_field.open_weather_field_remote()` actually uses, specifically to
sidestep the DLL conflict above rather than trying to fix a machine-specific
Windows conda environment problem. Prefer these unless you specifically need
the `.nc` files persisted to disk and have already confirmed your local
netCDF4/h5netcdf install actually works.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import copernicusmarine
import xarray as xr

WAVE_DATASET_ID = "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i"
WIND_DATASET_ID = "cmems_obs-wind_glo_phy_my_l4_0.125deg_PT1H"

WAVE_VARIABLES = ["VHM0"]
WIND_VARIABLES = ["eastward_wind", "northward_wind"]

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "cmems"


def _credentials_from_env() -> tuple[str | None, str | None]:
    """Read CMEMS credentials from environment variables (populated from
    .env via python-dotenv at the application entry point — this module
    does not call load_dotenv() itself, to avoid surprising side effects
    on import; call it once at startup instead)."""
    return (
        os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME"),
        os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD"),
    )


def describe_dataset(dataset_id: str) -> dict:
    """Fetch the dataset's catalogue entry (variables, coordinate ranges,
    time extent). Run this once per dataset before hardcoding a variable
    list elsewhere — see module docstring."""
    return copernicusmarine.describe(dataset_id=dataset_id, disable_progress_bar=True)


def fetch_wave_field(
    west: float,
    south: float,
    east: float,
    north: float,
    start: datetime,
    end: datetime,
    variables: list[str] | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    output_filename: str = "wave_field.nc",
) -> Path:
    """Download a wave-height subset over the given bbox/time window.
    Returns the local NetCDF file path."""
    username, password = _credentials_from_env()
    output_dir.mkdir(parents=True, exist_ok=True)

    copernicusmarine.subset(
        dataset_id=WAVE_DATASET_ID,
        username=username,
        password=password,
        variables=variables or WAVE_VARIABLES,
        minimum_longitude=west,
        maximum_longitude=east,
        minimum_latitude=south,
        maximum_latitude=north,
        start_datetime=start,
        end_datetime=end,
        output_directory=str(output_dir),
        output_filename=output_filename,
        overwrite=True,
    )
    return output_dir / output_filename


def open_wave_field(
    west: float,
    south: float,
    east: float,
    north: float,
    start: datetime,
    end: datetime,
    variables: list[str] | None = None,
) -> xr.Dataset:
    """Reads a wave-height subset directly from CMEMS's remote store into
    an in-memory xarray.Dataset via `copernicusmarine.open_dataset()` (zarr/
    fsspec) — no local `.nc` file is ever written or read, so this has no
    local netCDF4/HDF5 dependency at all. See module docstring for why this
    is the recommended path over `fetch_wave_field()`.

    `.load()` pulls the whole (bbox x time-window) subset into memory in
    one transfer, so repeated point sampling downstream (`weather_field.py`'s
    `WeatherField.sample()`, called once per leg per solver iteration) never
    triggers a repeat network round-trip.
    """
    username, password = _credentials_from_env()
    ds = copernicusmarine.open_dataset(
        dataset_id=WAVE_DATASET_ID,
        username=username,
        password=password,
        variables=variables or WAVE_VARIABLES,
        minimum_longitude=west,
        maximum_longitude=east,
        minimum_latitude=south,
        maximum_latitude=north,
        start_datetime=start,
        end_datetime=end,
    )
    return ds.load()


def open_wind_field(
    west: float,
    south: float,
    east: float,
    north: float,
    start: datetime,
    end: datetime,
    variables: list[str] | None = None,
) -> xr.Dataset:
    """Wind-component equivalent of `open_wave_field()` — see its docstring."""
    username, password = _credentials_from_env()
    ds = copernicusmarine.open_dataset(
        dataset_id=WIND_DATASET_ID,
        username=username,
        password=password,
        variables=variables or WIND_VARIABLES,
        minimum_longitude=west,
        maximum_longitude=east,
        minimum_latitude=south,
        maximum_latitude=north,
        start_datetime=start,
        end_datetime=end,
    )
    return ds.load()


def fetch_wind_field(
    west: float,
    south: float,
    east: float,
    north: float,
    start: datetime,
    end: datetime,
    variables: list[str] | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    output_filename: str = "wind_field.nc",
) -> Path:
    """Download a wind-component subset over the given bbox/time window.
    Defaults to WIND_VARIABLES (eastward_wind, northward_wind), confirmed
    against the live CMEMS catalogue — see module docstring."""
    username, password = _credentials_from_env()
    output_dir.mkdir(parents=True, exist_ok=True)

    copernicusmarine.subset(
        dataset_id=WIND_DATASET_ID,
        username=username,
        password=password,
        variables=variables or WIND_VARIABLES,
        minimum_longitude=west,
        maximum_longitude=east,
        minimum_latitude=south,
        maximum_latitude=north,
        start_datetime=start,
        end_datetime=end,
        output_directory=str(output_dir),
        output_filename=output_filename,
        overwrite=True,
    )
    return output_dir / output_filename
