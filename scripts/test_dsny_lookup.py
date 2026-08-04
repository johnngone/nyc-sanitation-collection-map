"""Probe a documented DSNY lookup endpoint once it has been identified.

The official lookup page is client-rendered. The endpoint is intentionally
required instead of guessed. This prevents accidental requests to an
incorrect service or silently fabricated schedule results.
"""

import argparse
import json
import logging

import httpx

LOGGER = logging.getLogger("test_dsny_lookup")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("address", help="One test address, exactly as entered in the official lookup")
    parser.add_argument("--url", required=True, help="Public JSON endpoint observed in browser network tools")
    parser.add_argument("--method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--address-param", default="address")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    headers = {
        "User-Agent": "nyc-sanitation-map-research/0.1 (public endpoint inspection)",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        LOGGER.info("Requesting method=%s url=%s", args.method, args.url)
        if args.method == "GET":
            response = client.get(args.url, params={args.address_param: args.address}, headers=headers)
        else:
            response = client.post(args.url, json={args.address_param: args.address}, headers=headers)
        LOGGER.info("Response status=%s content_type=%s", response.status_code, response.headers.get("content-type"))
        response.raise_for_status()
        try:
            payload = response.json()
        except json.JSONDecodeError:
            LOGGER.error("Endpoint returned non-JSON response; first 400 characters follow")
            print(response.text[:400])
            raise
        print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

