"""
One-off diagnostic: fetches the CMEMS catalogue entries for the wave and
wind datasets used by cmems_client.py, and prints the variable short names
for each. This is how we pin down the exact wind variable names (see the
open item in findings.md) rather than guessing them.

Run once from the project root, with the conda env active and CMEMS
credentials in .env:

    python scripts/describe_cmems_datasets.py

Also writes the full catalogue JSON to data/raw/cmems/*_catalogue.json in
case the variable list needs deeper inspection than the printed summary.
"""
from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # populates COPERNICUSMARINE_SERVICE_USERNAME/_PASSWORD from .env

import copernicusmarine  # noqa: E402  (import after load_dotenv on purpose)

from navi_opt.weather.cmems_client import WAVE_DATASET_ID, WIND_DATASET_ID  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "raw" / "cmems"


def describe_and_print(dataset_id: str, label: str) -> None:
    print(f"\n=== {label}: {dataset_id} ===")
    catalogue = copernicusmarine.describe(dataset_id=dataset_id, disable_progress_bar=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{dataset_id}_catalogue.json"
    out_path.write_text(catalogue.model_dump_json(indent=2), encoding="utf-8")
    print(f"Full catalogue JSON written to: {out_path}")

    # Best-effort walk to print just the variable short names. If the
    # object structure differs from what's assumed here, the full JSON
    # file above is the fallback — open it and search for "short_name".
    found_any = False
    try:
        for product in catalogue.products:
            for dataset in product.datasets:
                if dataset.dataset_id != dataset_id:
                    continue
                for version in dataset.versions:
                    for part in version.parts:
                        for service in part.services:
                            for var in service.variables:
                                print(f"  variable: short_name={var.short_name!r} standard_name={getattr(var, 'standard_name', None)!r}")
                                found_any = True
    except AttributeError as e:
        print(f"  (walk hit an unexpected attribute path: {e} — check the JSON file instead)")

    if not found_any:
        print("  No variables printed via the walk — open the JSON file above and search for \"short_name\".")


if __name__ == "__main__":
    describe_and_print(WAVE_DATASET_ID, "WAVE")
    describe_and_print(WIND_DATASET_ID, "WIND")
