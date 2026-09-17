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
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import copernicusmarine

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
