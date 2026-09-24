import os


APP_ENV = os.getenv("APP_ENV", "development")
APP_TITLE = os.getenv("APP_TITLE", "NYC Trash Map").strip() or "NYC Trash Map"
APP_SUBTITLE = (
    os.getenv("APP_SUBTITLE", "See collection schedules by street and day.").strip()
    or "See collection schedules by street and day."
)
APP_BROWSER_TITLE = (
    os.getenv("APP_BROWSER_TITLE", "NYC Trash Map").strip()
    or "NYC Trash Map"
)
APP_SHORT_NAME = os.getenv("APP_SHORT_NAME", "NYC Trash Map").strip() or "NYC Trash Map"
APP_META_DESCRIPTION = (
    os.getenv(
        "APP_META_DESCRIPTION",
        "Explore NYC sanitation schedules by street and day. View trash, recycling, "
        "organics, and bulk collection near your live location on an interactive map.",
    ).strip()
    or (
        "Explore NYC sanitation schedules by street and day. View trash, recycling, "
        "organics, and bulk collection near your live location on an interactive map."
    )
)
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "").strip().rstrip("/")
APP_ROBOTS_TXT = os.getenv("APP_ROBOTS_TXT", "").replace("\\n", "\n").strip()
DATA_MANIFEST_PATH = os.getenv("DATA_MANIFEST_PATH", "data/data_manifest.json")
HEALTH_SYNC_HASH_MAX_BYTES = max(
    0, int(os.getenv("HEALTH_SYNC_HASH_MAX_BYTES", str(16 * 1024 * 1024)))
)
