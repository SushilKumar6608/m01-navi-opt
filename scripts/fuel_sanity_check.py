"""
Standalone sanity-check script for the fuel-consumption model.

Not a test — no assertions, no pass/fail. It just calls the real
fuel_rate_tonnes_per_day() from src/navi_opt/weather/resistance_model.py
across a small matrix of realistic speed / wave / wind / cargo-condition
combinations and prints the numbers in a readable table, so you can
eyeball them against intuition instead of trusting a "39 passed" summary.

Run it from the project root (with the m01-navi-opt conda env active):

    python scripts/fuel_sanity_check.py

Or, to check a different vessel:

    python scripts/fuel_sanity_check.py aframax_tanker
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `src/` importable when run directly (python scripts/fuel_sanity_check.py)
# without needing the package installed in editable mode.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navi_opt.weather.resistance_model import fuel_rate_tonnes_per_day  # noqa: E402
from navi_opt.weather.vessel_profiles import load_vessel_profile  # noqa: E402

SPEEDS_KNOTS = [10.0, 12.0, 14.0, 16.0]
WAVE_HEIGHTS_M = [0.0, 2.0, 4.0]
HEADWINDS_KNOTS = [0.0, 10.0, 20.0]
CARGO_CONDITIONS = ["laden", "ballast"]


def main() -> None:
    vessel_key = sys.argv[1] if len(sys.argv) > 1 else "mr2_product_tanker"
    vessel = load_vessel_profile(vessel_key)
    lo, hi = vessel.speed_bounds_knots

    print(f"Vessel: {vessel.key}  (DWT {vessel.dwt_tonnes:,.0f} t, "
          f"design speed {vessel.design_speed_knots} kn, "
          f"speed bounds [{lo}, {hi}] kn)")
    print(f"fuel_curve: a={vessel.fuel_curve.a} b={vessel.fuel_curve.b} "
          f"c={vessel.fuel_curve.c} d={vessel.fuel_curve.d}")
    print(f"cargo_condition_factor: {vessel.cargo_condition_factor}")
    print()

    header = f"{'speed(kn)':>10} {'wave(m)':>8} {'headwind(kn)':>13} {'cargo':>8} {'fuel(t/day)':>13}"
    print(header)
    print("-" * len(header))

    for cargo in CARGO_CONDITIONS:
        for speed in SPEEDS_KNOTS:
            if not (lo <= speed <= hi):
                continue
            for wave in WAVE_HEIGHTS_M:
                for headwind in HEADWINDS_KNOTS:
                    rate = fuel_rate_tonnes_per_day(speed, wave, headwind, vessel, cargo)
                    print(
                        f"{speed:>10.1f} {wave:>8.1f} {headwind:>13.1f} "
                        f"{cargo:>8} {rate:>13.2f}"
                    )
        print()


if __name__ == "__main__":
    main()
