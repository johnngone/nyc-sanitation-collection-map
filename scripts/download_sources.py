"""Download bounded official samples, or explicitly opt into the LION archive."""

import argparse
import logging
from pathlib import Path

import httpx

LOGGER = logging.getLogger("download_sources")
SOURCES = {
    "dsny-sample": "https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/DSNY_Frequencies_OFFICIAL/FeatureServer/0/query?where=1%3D1&outFields=*&returnGeometry=true&resultRecordCount=100&f=geojson",
    "address-sample": "https://data.cityofnewyork.us/resource/uf93-f8nk.geojson?$limit=100",
    "lion": "https://data.cityofnewyork.us/download/2v4z-66xt/application/zip",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=tuple(SOURCES))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-large-download", action="store_true", help="Required for the full LION archive")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    if args.source == "lion" and not args.allow_large_download:
        raise SystemExit("Refusing full LION download without --allow-large-download")
    output = args.output or Path("data/raw") / {"dsny-sample": "dsny_frequencies.geojson", "address-sample": "address_points.geojson", "lion": "lion.zip"}[args.source]
    output.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Downloading source=%s output=%s", args.source, output)
    with httpx.stream("GET", SOURCES[args.source], headers={"User-Agent": "nyc-sanitation-map-research/0.1"}, timeout=args.timeout, follow_redirects=True) as response:
        response.raise_for_status()
        with output.open("wb") as output_file:
            for chunk in response.iter_bytes():
                output_file.write(chunk)
    LOGGER.info("Download complete output=%s bytes=%s", output, output.stat().st_size)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
