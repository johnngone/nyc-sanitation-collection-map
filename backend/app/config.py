import os


APP_ENV = os.getenv("APP_ENV", "development")
APP_TITLE = os.getenv("APP_TITLE", "NYC Sanitation – Collection Map").strip() or "NYC Sanitation – Collection Map"
APP_SUBTITLE = (
    os.getenv("APP_SUBTITLE", "See collection schedules by street and day.").strip()
    or "See collection schedules by street and day."
)
APP_BROWSER_TITLE = (
    os.getenv("APP_BROWSER_TITLE", "NYC Sanitation Collection Map").strip()
    or "NYC Sanitation Collection Map"
)
APP_META_DESCRIPTION = (
    os.getenv(
        "APP_META_DESCRIPTION",
        "Map NYC sanitation collection schedules by street and day.",
    ).strip()
    or "Map NYC sanitation collection schedules by street and day."
)
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "").strip().rstrip("/")
APP_ROBOTS_TXT = os.getenv("APP_ROBOTS_TXT", "").replace("\\n", "\n").strip()
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/app.sqlite3")
DATA_REFRESH_ENABLED = os.getenv("DATA_REFRESH_ENABLED", "true").lower() == "true"
DATA_REFRESH_INTERVAL_DAYS = int(os.getenv("DATA_REFRESH_INTERVAL_DAYS", "14"))
DATA_REFRESH_ON_STARTUP = os.getenv("DATA_REFRESH_ON_STARTUP", "false").lower() == "true"
DATA_MANIFEST_PATH = os.getenv("DATA_MANIFEST_PATH", "data/data_manifest.json")
DATA_RELEASE_RETENTION = max(2, int(os.getenv("DATA_RELEASE_RETENTION", "2")))
HEALTH_SYNC_HASH_MAX_BYTES = max(
    0, int(os.getenv("HEALTH_SYNC_HASH_MAX_BYTES", str(16 * 1024 * 1024)))
)
TILESET_PATH = os.getenv("TILESET_PATH", "data/collection_streets.mbtiles")
