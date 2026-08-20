"""Run the configurable data refresh on a fixed interval."""

import logging
import os
import subprocess
import sys
import time

LOGGER = logging.getLogger("run_scheduler")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    enabled = os.getenv("DATA_REFRESH_ENABLED", "true").lower() == "true"
    if not enabled:
        LOGGER.info("Data refresh disabled")
        return
    interval = int(os.getenv("DATA_REFRESH_INTERVAL_DAYS", "14")) * 86400
    run_on_startup = os.getenv("DATA_REFRESH_ON_STARTUP", "false").lower() == "true"
    while True:
        if run_on_startup:
            run_on_startup = False
        else:
            LOGGER.info("Next data refresh in seconds=%s", interval)
            time.sleep(interval)
        command = [sys.executable, "scripts/run_refresh.py", "--allow-large-run"]
        started = time.monotonic()
        LOGGER.info("Starting scheduled data refresh")
        result = subprocess.run(command, check=False)
        if result.returncode:
            LOGGER.error("Data refresh failed exit_code=%s; retaining live database", result.returncode)
        else:
            LOGGER.info("Completed scheduled data refresh elapsed_s=%.1f", time.monotonic() - started)


if __name__ == "__main__":
    main()
