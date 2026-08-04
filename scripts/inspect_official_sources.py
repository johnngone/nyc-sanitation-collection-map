"""Fetch tiny official samples and print response/schema information.

This script intentionally limits each request to three records. It is a
read-only inspection tool and does not download citywide data.
"""

import argparse
import json
import logging
from typing import Any

import httpx

LOGGER = logging.getLogger("inspect_official_sources")
SOURCES = {
    "dsny_socrata": "https://data.cityofnewyork.us/resource/rv63-53db.json?$limit=3",
    "address_points": "https://data.cityofnewyork.us/resource/uf93-f8nk.json?$limit=3",
    "dsny_arcgis": (
        "https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/"
        "DSNY_Frequencies_OFFICIAL/FeatureServer/0/query"
        "?where=1%3D1&outFields=*&returnGeometry=true&resultRecordCount=3&f=json"
    ),
}


def print_schema(name: str, payload: Any) -> None:
    print(f"\n=== {name} ===")
    if isinstance(payload, list):
        print(f"records: {len(payload)}")
        if payload:
            print("columns:", sorted(payload[0].keys()))
            print(json.dumps(payload[0], indent=2, default=str)[:4000])
        return
    if isinstance(payload, dict):
        print("top-level keys:", sorted(payload.keys()))
        fields = payload.get("fields")
        features = payload.get("features")
        if isinstance(fields, list):
            print("fields:", [field.get("name") for field in fields])
        if isinstance(features, list):
            print(f"features: {len(features)}")
            if features:
                print(json.dumps(features[0], indent=2, default=str)[:4000])
        else:
            print(json.dumps(payload, indent=2, default=str)[:4000])
        return
    raise TypeError(f"Unsupported response type for {name}: {type(payload).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        for name, url in SOURCES.items():
            LOGGER.info("Fetching small sample source=%s url=%s", name, url)
            response = client.get(url, headers={"User-Agent": "nyc-sanitation-map-research/0.1"})
            response.raise_for_status()
            print_schema(name, response.json())


if __name__ == "__main__":
    main()

