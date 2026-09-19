"""
Phase 3: turns Phase 1's raw CMEMS pulls (`cmems_client.fetch_wave_field()` /
`fetch_wind_field()`, which just download NetCDF subsets to disk) into the
`WeatherLookup` callable Phase 2's `optimize_speed_profile()` already expects:
`(u, v, arrival_time_hours) -> (wave_height_m, headwind_knots)`.

This is the module `speed_profile.py`'s docstring flagged as "still pending" —
`calm_weather_lookup()` there is a placeholder that returns zero for every leg,
explicitly documented as "no weather data available", not a forecast. This
module is how a real one gets built.

Two pieces:

1. `WeatherField` — wraps the opened wave-height and wind-component
   `xarray.Dataset` objects and samples them at a given (lat, lon, time).
   Nearest-neighbor in space and time, not interpolated: CMEMS's ~0.08-0.125
   degree grids are fine enough relative to this project's H3 resolution
   (resolution 5 ~= 0.08 degrees) that nearest-neighbor is a reasonable
   choice, and it sidesteps NaN propagation that linear interpolation would
   risk near any land/coastline mask edge in the source data.

   A first real run (Sep 2026, Rotterdam<->Ceyhan corridor) hit real NaNs at
   every corridor's first leg — not a bug, but a genuine mismatch between two
   different coastline sources: this project's H3 ocean grid is built from
   Natural Earth polygons (graph_adapter.py/land_mask.py), while CMEMS's wave
   model has its own, differently-shaped land mask at its own resolution. An
   H3 cell can be legitimately "ocean" by Natural Earth's coastline and still
   fall in CMEMS's masked-out near-shore band, especially right at port
   approaches (river deltas, narrow estuaries — the same class of place that
   caused Antwerp's connectivity issue in Phase 2's real graph). `sample()`
   handles this the same way real oceanographic pipelines do: falls back to
   the nearest grid point that actually HAS valid data (found via a KD-tree
   built once per field, not searched fresh each call), re-queried at the
   real requested time rather than assumed constant. Still raises loudly —
   never silently substitutes zero — if even the nearest known-valid point
   turns out NaN at that specific time, or if no valid point exists in the
   field at all (see `sample()`'s docstring for the exact conditions).

2. `make_weather_lookup()` — the adapter itself. Samples weather at a leg's
   departure node and departure time (mirroring `optimize_speed_profile()`'s
   own documented per-leg-at-departure-time simplification, not
   independently reinventing it), then projects the sampled wind vector onto
   that leg's travel bearing via `resistance_model.relative_headwind_component()`
   to get the headwind component the fuel model actually needs.

CMEMS units, confirmed against `cmems_client.py`'s dataset docstrings:
wave height (VHM0) in meters (no conversion needed); wind components
(eastward_wind/northward_wind) in m/s, while this project's fuel model and
vessel speeds are in knots throughout (vessel_profiles.yaml, resistance_model.py)
— `WeatherField.sample()` converts m/s -> knots before returning, so nothing
downstream ever has to remember to.

Cannot be exercised against real CMEMS data from this sandbox (no route to
Copernicus Marine's servers here, same limitation as the Natural Earth CDN
noted in graph_adapter.py/findings.md) — tests use small in-memory
`xarray.Dataset` fixtures with the same variable/coordinate names and units
CMEMS actually uses (confirmed in `cmems_client.py`), not a live pull. Read
the printed diagnostics from the first real run before trusting the output,
same discipline as every other phase.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Tuple

import networkx as nx
import numpy as np
import xarray as xr
from scipy.spatial import cKDTree

from navi_opt.weather.cmems_client import open_wave_field, open_wind_field
from navi_opt.weather.resistance_model import (
    relative_headwind_component,
    wind_uv_to_speed_direction,
)

WeatherLookup = Callable[[str, str, float], Tuple[float, float]]

MS_TO_KNOTS = 1.9438452


class _FieldIndex:
    """Precomputed fast (lat, lon, time) -> raw-value lookup for ONE CMEMS
    field (wave or wind), built once per `WeatherField` construction.

    This exists to replace repeated `xarray.Dataset.sel(method="nearest")`
    calls, which is what `WeatherField.sample()` used before. That's fine
    occasionally, but `optimize_speed_profile()`'s SLSQP solve calls the
    weather lookup on the order of (legs x solver iterations) times per
    corridor — for a real ~387-leg corridor that's easily tens of millions
    of calls, and `.sel()`'s per-call overhead (label alignment, Dataset
    wrapping/unwrapping) at that volume was the actual cause of a real run
    taking multiple hours per corridor (see findings.md). This index does
    the equivalent lookup — nearest space via a KD-tree over the full grid,
    nearest time via binary search on the sorted time coordinate — directly
    against plain numpy arrays, which is the same nearest-neighbor result,
    just without re-paying xarray's per-call overhead on every single leg.
    """

    def __init__(
        self,
        ds: xr.Dataset,
        var_names: list,
        lat_name: str,
        lon_name: str,
        time_name: str,
    ):
        self.var_names = var_names
        lat_vals = np.asarray(ds[lat_name].values, dtype=float)
        lon_vals = np.asarray(ds[lon_name].values, dtype=float)
        self.time_vals = ds[time_name].values  # assumed sorted ascending, as CMEMS provides

        self.arrays = {
            name: ds[name].transpose(time_name, lat_name, lon_name).values for name in var_names
        }

        lat_grid, lon_grid = np.meshgrid(lat_vals, lon_vals, indexing="ij")
        self.grid_shape = lat_grid.shape
        self.spatial_tree = cKDTree(np.column_stack([lat_grid.ravel(), lon_grid.ravel()]))

        # Nearest-VALID-point tree for the NaN fallback, from a reference
        # timestep (t=0) — see WeatherField's docstring for why one
        # reference slice is representative of CMEMS's (time-invariant)
        # land mask.
        valid = np.ones(self.grid_shape, dtype=bool)
        for name in var_names:
            valid &= ~np.isnan(self.arrays[name][0])
        if valid.any():
            valid_lats = lat_grid[valid]
            valid_lons = lon_grid[valid]
            self.valid_tree = cKDTree(np.column_stack([valid_lats, valid_lons]))
            self.valid_points = np.column_stack([valid_lats, valid_lons])
        else:
            self.valid_tree = None
            self.valid_points = None

    def nearest_time_idx(self, time64) -> int:
        idx = int(np.searchsorted(self.time_vals, time64))
        if idx <= 0:
            return 0
        if idx >= len(self.time_vals):
            return len(self.time_vals) - 1
        before, after = self.time_vals[idx - 1], self.time_vals[idx]
        return idx - 1 if (time64 - before) <= (after - time64) else idx

    def nearest_spatial_idx(self, lat: float, lon: float):
        _, flat_idx = self.spatial_tree.query([lat, lon])
        return np.unravel_index(int(flat_idx), self.grid_shape)

    def values_at(self, lat_idx: int, lon_idx: int, t_idx: int) -> dict:
        return {name: float(self.arrays[name][t_idx, lat_idx, lon_idx]) for name in self.var_names}


class WeatherField:
    """Samples significant wave height and headwind-relevant wind vectors
    from opened CMEMS `xarray.Dataset` objects, at a given (lat, lon, time).

    Construct directly with already-opened Datasets (what the tests do, and
    what lets a caller reuse a Dataset opened some other way — e.g. already
    lazily opened via dask), or via `open_weather_field()` for the common
    case of "I have two NetCDF file paths from `cmems_client.py`".
    """

    def __init__(
        self,
        wave_ds: xr.Dataset,
        wind_ds: xr.Dataset,
        wave_var: str = "VHM0",
        eastward_var: str = "eastward_wind",
        northward_var: str = "northward_wind",
        lat_name: str = "latitude",
        lon_name: str = "longitude",
        time_name: str = "time",
    ):
        self.wave_ds = wave_ds
        self.wind_ds = wind_ds
        self.wave_var = wave_var
        self.eastward_var = eastward_var
        self.northward_var = northward_var
        self.lat_name = lat_name
        self.lon_name = lon_name
        self.time_name = time_name
        self._validate()
        self._wave_index = _FieldIndex(
            self.wave_ds, [self.wave_var], self.lat_name, self.lon_name, self.time_name
        )
        self._wind_index = _FieldIndex(
            self.wind_ds,
            [self.eastward_var, self.northward_var],
            self.lat_name,
            self.lon_name,
            self.time_name,
        )
        self._warned_fallback_locations: set = set()

    def _validate(self) -> None:
        checks = [
            ("wave", self.wave_ds, self.wave_var),
            ("wind eastward", self.wind_ds, self.eastward_var),
            ("wind northward", self.wind_ds, self.northward_var),
        ]
        for label, ds, var in checks:
            if var not in ds.variables:
                raise KeyError(
                    f"{label} dataset is missing expected variable {var!r} — "
                    f"got {sorted(ds.variables)}. If CMEMS renamed/changed this "
                    f"dataset's variables, re-run "
                    f"navi_opt.weather.cmems_client.describe_dataset() and update "
                    f"the variable name passed to WeatherField(), don't guess."
                )
        for label, ds in [("wave", self.wave_ds), ("wind", self.wind_ds)]:
            for dim_name in (self.lat_name, self.lon_name, self.time_name):
                if dim_name not in ds.coords and dim_name not in ds.dims:
                    raise KeyError(
                        f"{label} dataset has no {dim_name!r} coordinate — got "
                        f"{sorted(ds.coords)}. Pass the actual coordinate names "
                        f"(lat_name=/lon_name=/time_name=) if this CMEMS dataset "
                        f"uses different ones."
                    )

    def _select_with_fallback(self, index: "_FieldIndex", lat: float, lon: float, time64, label: str) -> dict:
        t_idx = index.nearest_time_idx(time64)
        lat_idx, lon_idx = index.nearest_spatial_idx(lat, lon)
        values = index.values_at(lat_idx, lon_idx, t_idx)
        if not any(math.isnan(v) for v in values.values()):
            return values

        if index.valid_tree is None:
            raise ValueError(self._nan_message(label, lat, lon, time64, values))

        # Nearest grid point that had real data at the REFERENCE timestep
        # used to build the tree — re-query it (via the same fast index,
        # not xarray) at the actual requested time, since the fallback
        # point must itself have real data NOW, not just whenever the mask
        # was built.
        _, idx = index.valid_tree.query([lat, lon])
        fb_lat, fb_lon = index.valid_points[idx]
        fb_lat_idx, fb_lon_idx = index.nearest_spatial_idx(fb_lat, fb_lon)
        fallback_values = index.values_at(fb_lat_idx, fb_lon_idx, t_idx)
        if any(math.isnan(v) for v in fallback_values.values()):
            raise ValueError(
                self._nan_message(label, lat, lon, time64, values)
                + f" The nearest known-ocean grid point ({fb_lat:.3f}, {fb_lon:.3f}) was ALSO "
                f"NaN at this specific time — this isn't just a coastline-resolution mismatch "
                f"(the usual cause, see WeatherField's docstring), investigate the fetch "
                f"window/dataset coverage directly."
            )

        key = (label, round(fb_lat, 3), round(fb_lon, 3))
        if key not in self._warned_fallback_locations:
            self._warned_fallback_locations.add(key)
            print(
                f"   [weather_field] note: ({lat:.3f}, {lon:.3f}) is NaN in the {label} field "
                f"(H3 cell finer/differently-shaped than CMEMS's own coastline) — using nearest "
                f"valid ocean grid point ({fb_lat:.3f}, {fb_lon:.3f}) instead. Printed once per "
                f"fallback location, not once per call."
            )
        return fallback_values

    @staticmethod
    def _nan_message(label: str, lat: float, lon: float, time64, values: dict) -> str:
        values_str = ", ".join(f"{k}={v}" for k, v in values.items())
        return (
            f"{label.capitalize()} sample at lat={lat}, lon={lon}, time={time64} is NaN "
            f"({values_str}) — this point is likely outside the fetched field's actual "
            f"coverage (a fetch window/bbox that didn't cover this corridor, or — if a "
            f"nearest-valid-point fallback was attempted and still failed — a genuine gap "
            f"in the source data at this time). Investigate before trusting anything "
            f"downstream of this lookup, don't substitute a default silently."
        )

    def sample(self, lat: float, lon: float, time: datetime) -> Tuple[float, float, float]:
        """Returns (wave_height_m, wind_speed_knots, wind_blowing_toward_deg)
        at the nearest available grid point/timestep to (lat, lon, time).

        If the exact nearest point is NaN (see module docstring — usually a
        coastline mismatch between this project's H3 grid and CMEMS's own
        land mask, not a real data gap), falls back to the nearest grid
        point that actually has valid data, re-queried at the real
        requested time. Still raises ValueError — never silently
        substitutes a default — if no valid point exists in the field at
        all, or if even the fallback point is NaN at this specific time.
        """
        time64 = np.datetime64(time)

        wave_values = self._select_with_fallback(self._wave_index, lat, lon, time64, "wave")
        wind_values = self._select_with_fallback(self._wind_index, lat, lon, time64, "wind")

        wave_val = wave_values[self.wave_var]
        eastward = wind_values[self.eastward_var]
        northward = wind_values[self.northward_var]

        wind_speed_ms, wind_dir_deg = wind_uv_to_speed_direction(eastward, northward)
        return wave_val, wind_speed_ms * MS_TO_KNOTS, wind_dir_deg


def _open_dataset_with_fallback(path: Path) -> xr.Dataset:
    """Opens a NetCDF file via xarray's default engine (netCDF4), falling
    back to h5netcdf on the specific failure mode this project actually hit
    on Windows: a `netCDF4`-package DLL load error (`ImportError: DLL load
    failed while importing _netCDF4`), caused by an HDF5 DLL conflict
    between the `netcdf4` and `geopandas` conda packages (geopandas pulls
    in its own GDAL/HDF5 build) rather than anything wrong with the
    downloaded file itself. h5netcdf links HDF5 differently and reliably
    sidesteps this exact conflict, so it's a legitimate fallback here, not
    a band-aid over a real data problem — re-raises anything else (a
    genuinely corrupt/incomplete download, say) rather than masking it.
    """
    try:
        return xr.open_dataset(path)
    except ImportError as exc:
        try:
            return xr.open_dataset(path, engine="h5netcdf")
        except ImportError:
            raise ImportError(
                f"Failed to open {path} via xarray's default (netCDF4) engine "
                f"({exc}), and the h5netcdf fallback engine isn't installed "
                f"either. This is almost always a Windows DLL conflict between "
                f"the netcdf4 and geopandas conda packages, not a problem with "
                f"the downloaded file. Fix: `conda install -c conda-forge "
                f"netcdf4 --force-reinstall -y`, or as a fallback, "
                f"`conda install -c conda-forge h5netcdf -y`."
            ) from exc


def open_weather_field(
    wave_path: Path,
    wind_path: Path,
    **kwargs,
) -> WeatherField:
    """Opens the two NetCDF files `cmems_client.fetch_wave_field()` /
    `fetch_wind_field()` produce and wraps them in a WeatherField. Extra
    kwargs (wave_var=, lat_name=, etc.) are forwarded to WeatherField() for
    the rare case CMEMS's variable/coordinate names differ from the
    confirmed defaults in cmems_client.py's module docstring.
    """
    wave_ds = _open_dataset_with_fallback(wave_path)
    wind_ds = _open_dataset_with_fallback(wind_path)
    return WeatherField(wave_ds, wind_ds, **kwargs)


def open_weather_field_remote(
    west: float,
    south: float,
    east: float,
    north: float,
    start: datetime,
    end: datetime,
    **kwargs,
) -> WeatherField:
    """Builds a WeatherField by streaming the wave/wind subset directly from
    CMEMS's remote store (`cmems_client.open_wave_field()`/`open_wind_field()`,
    zarr/fsspec) straight into memory — no local `.nc` file is ever written
    or read, so this needs no local netCDF4/HDF5 install at all.

    Prefer this over `open_weather_field()` (which reads local files
    produced by `cmems_client.fetch_wave_field()`/`fetch_wind_field()`)
    unless you specifically want the files cached to disk AND have already
    confirmed your local netCDF4/h5netcdf install actually works — on
    Windows this project hit an unresolvable HDF5 DLL conflict taking that
    path (three different conda-forge reinstalls each moved the failure to
    a different package rather than fixing it; see findings.md), which is
    exactly what this function exists to avoid.
    """
    wave_ds = open_wave_field(west=west, south=south, east=east, north=north, start=start, end=end)
    wind_ds = open_wind_field(west=west, south=south, east=east, north=north, start=start, end=end)
    return WeatherField(wave_ds, wind_ds, **kwargs)


def make_weather_lookup(
    digraph: nx.DiGraph,
    weather_field: WeatherField,
    voyage_start_time: datetime,
) -> WeatherLookup:
    """Builds a `WeatherLookup` usable directly as
    `optimize_speed_profile()`'s `weather_lookup=` argument.

    Samples weather at leg (u, v)'s departure node `u` and its absolute
    departure time (`voyage_start_time + arrival_time_hours`), then converts
    the sampled wind vector into a headwind component for `v` — `bearing_deg`
    comes straight off the edge (set by `graph_adapter.to_routable_digraph()`
    as the true direction-of-travel bearing for that specific directed edge,
    not a naive copy from the undirected source graph — see graph_adapter's
    own docstring for why that distinction mattered).
    """

    def _lookup(u: str, v: str, arrival_time_hours: float) -> Tuple[float, float]:
        if not digraph.has_edge(u, v):
            raise ValueError(f"No edge ({u!r} -> {v!r}) in graph — invalid leg")
        if "lat" not in digraph.nodes[u] or "lon" not in digraph.nodes[u]:
            raise KeyError(
                f"Node {u!r} has no lat/lon — expected a graph produced by "
                f"navi_opt.routing.graph_adapter.to_routable_digraph()"
            )
        lat = digraph.nodes[u]["lat"]
        lon = digraph.nodes[u]["lon"]
        bearing_deg = digraph[u][v]["bearing_deg"]

        sample_time = voyage_start_time + timedelta(hours=arrival_time_hours)
        wave_height_m, wind_speed_kn, wind_dir_deg = weather_field.sample(lat, lon, sample_time)
        headwind_kn = relative_headwind_component(bearing_deg, wind_dir_deg, wind_speed_kn)
        return wave_height_m, headwind_kn

    return _lookup
