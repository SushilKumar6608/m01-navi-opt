"""
Phase 3: fetches real CMEMS wave/wind fields over the Rotterdam <-> Ceyhan
corridor and re-runs the Phase 2 smoke test's speed optimization against
them, instead of the `calm_weather_lookup()` placeholder — the head-to-head
comparison (calm-water baseline vs. real-weather-aware) is the actual point,
since a single weather-aware number alone doesn't tell you whether wiring
this in mattered.

Run this AFTER `scripts/build_real_ocean_graph.py` has been run at least
once (this script loads its cached digraph/anchors rather than rebuilding
them) and with COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD set in .env
(the same .env from Phase 1 — never typed at a prompt, never hardcoded):
    python scripts/run_weather_aware_speed_profile.py

Requires internet access to CMEMS's servers for the wave/wind subset pulls
(cached to data/raw/cmems/ afterward, same pattern as Natural Earth). Can't
be verified from the sandbox that built this script (no route out to
CMEMS from there — see findings.md), so read its printed output before
trusting it, same discipline as every other real-data phase.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # populates COPERNICUSMARINE_SERVICE_USERNAME/_PASSWORD from .env,
# same pattern as scripts/describe_cmems_datasets.py — cmems_client.py deliberately
# does NOT call this itself (see its module docstring), so the entry point must.

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from navi_opt.grid.graph_builder import load_graph  # noqa: E402
from navi_opt.grid.port_anchors import load_anchors  # noqa: E402
from navi_opt.optimization.speed_profile import (  # noqa: E402
    calm_weather_lookup,
    optimize_speed_profile,
)
from navi_opt.routing.k_shortest import YenKShortestPaths  # noqa: E402
from navi_opt.weather.vessel_profiles import load_vessel_profile  # noqa: E402
from navi_opt.weather.weather_field import (  # noqa: E402
    make_weather_lookup,
    open_weather_field_remote,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "processed"
DIGRAPH_PATH = DATA_DIR / "ocean_digraph_rotterdam_ceyhan.pkl"
ANCHORS_PATH = DATA_DIR / "port_h3_anchors_rotterdam_ceyhan.parquet"

# Matches scripts/build_real_ocean_graph.py's BBOX — the corridor's actual
# navigable extent, not just a box around the two port coordinates.
BBOX = dict(west=-15.0, south=30.0, east=40.0, north=56.0)

# The wind dataset (cmems_obs-wind_glo_phy_my_l4_0.125deg_PT1H) is the
# multi-year REPROCESSED product, not near-real-time forecast (see
# cmems_client.py's module docstring for why that dataset was chosen) — so
# "now" is very likely NOT covered. Use a fixed historical window instead,
# picked well inside that dataset's typical multi-year reprocessed coverage.
# If this specific window turns out to be outside either dataset's actual
# extent, WeatherField.sample()'s NaN check will say so explicitly (not
# silently substitute a default) — adjust VOYAGE_START below and re-run.
VOYAGE_START = datetime(2024, 6, 1, 0, 0)
# Wide margin over the ~230h/~10-day transit seen in the Phase 2 smoke test
# run, since the exact per-corridor transit time isn't known until after
# routing — cheap to over-fetch a few extra days, expensive to under-fetch
# and hit a NaN partway through a corridor.
FETCH_WINDOW = timedelta(days=16)

V_MAX_KNOTS = 16.0


def calm_water_cost_evaluator(digraph):
    def _evaluator(u, v, current_t):
        hours = digraph[u][v]["dist_nm"] / V_MAX_KNOTS
        return hours, hours

    return _evaluator


def main() -> None:
    if not DIGRAPH_PATH.exists() or not ANCHORS_PATH.exists():
        print(f"Missing {DIGRAPH_PATH} or {ANCHORS_PATH} — run "
              f"scripts/build_real_ocean_graph.py first, this script reuses its cache.")
        return

    print("1. Loading cached digraph and port anchors...")
    digraph = load_graph(path=DIGRAPH_PATH)
    anchors = load_anchors(path=ANCHORS_PATH)
    print(f"   {digraph.number_of_nodes()} nodes, {digraph.number_of_edges()} edges")

    print(f"2. Streaming CMEMS wave/wind fields over {BBOX}, "
          f"{VOYAGE_START.isoformat()} + {FETCH_WINDOW} directly into memory "
          f"(no local .nc file — see weather_field.open_weather_field_remote()'s "
          f"docstring for why)...")
    weather_field = open_weather_field_remote(
        start=VOYAGE_START, end=VOYAGE_START + FETCH_WINDOW, **BBOX
    )
    print("   loaded.")

    print("3. Building the weather-aware lookup...")
    weather_lookup = make_weather_lookup(digraph, weather_field, VOYAGE_START)

    print("4. Finding diverse corridors (same as Phase 2's smoke test)...")
    origin_cell = anchors["rotterdam"]
    destination_cell = anchors["ceyhan"]
    yen = YenKShortestPaths(
        digraph, calm_water_cost_evaluator(digraph), v_max_knots=V_MAX_KNOTS, enforce_diversity=True
    )
    corridors = yen.find_k_paths(origin_cell, destination_cell, k=3, start_time_hours=0.0)
    if not corridors:
        print("   NO PATH FOUND — investigate before continuing.")
        return

    vessel = load_vessel_profile("mr2_product_tanker")
    print("\n5. Speed-optimizing each corridor: calm-water baseline vs. real-weather-aware.")
    print("   (This is the actual point of this script — a single weather-aware number alone "
          "doesn't tell you whether wiring real CMEMS data in changed anything.)")
    for i, corridor in enumerate(corridors, start=1):
        calm = optimize_speed_profile(
            corridor["path"], digraph, vessel, "laden", weather_lookup=calm_weather_lookup
        )
        try:
            real = optimize_speed_profile(
                corridor["path"], digraph, vessel, "laden", weather_lookup=weather_lookup
            )
        except ValueError as exc:
            print(f"\n   Corridor {i}: weather-aware optimization FAILED — {exc}")
            continue

        delta_fuel = real.total_fuel_tonnes - calm.total_fuel_tonnes
        pct = 100.0 * delta_fuel / calm.total_fuel_tonnes
        print(f"\n   Corridor {i}: {len(corridor['path'])} nodes")
        print(f"     calm-water baseline:  {calm.total_fuel_tonnes:.1f}t fuel, "
              f"{calm.total_transit_hours:.1f}h transit")
        print(f"     real-weather-aware:   {real.total_fuel_tonnes:.1f}t fuel, "
              f"{real.total_transit_hours:.1f}h transit  "
              f"({delta_fuel:+.1f}t / {pct:+.1f}% vs. calm-water)")


if __name__ == "__main__":
    main()
