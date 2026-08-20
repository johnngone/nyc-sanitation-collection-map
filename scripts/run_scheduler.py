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
    failure_retry = max(60, int(os.getenv("DATA_REFRESH_FAILURE_RETRY_MINUTES", "30")) * 60)
    run_on_startup = os.getenv("DATA_REFRESH_ON_STARTUP", "false").lower() == "true"
    next_delay = 0 if run_on_startup else interval
    while True:
        if next_delay:
            LOGGER.info("Next data refresh in seconds=%s", next_delay)
            time.sleep(next_delay)
        command = [sys.executable, "scripts/run_refresh.py", "--allow-large-run"]
        started = time.monotonic()
        LOGGER.info("Starting scheduled data refresh")
        result = subprocess.run(command, check=False)
        if result.returncode:
            LOGGER.error(
                "Data refresh failed exit_code=%s; retaining live database and retrying in seconds=%s",
                result.returncode,
                failure_retry,
            )
            next_delay = failure_retry
        else:
            LOGGER.info("Completed scheduled data refresh elapsed_s=%.1f", time.monotonic() - started)
            next_delay = interval


if __name__ == "__main__":
    main()
