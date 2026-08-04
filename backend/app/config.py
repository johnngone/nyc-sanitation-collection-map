import os


APP_ENV = os.getenv("APP_ENV", "development")
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/app.sqlite3")
DATA_REFRESH_ENABLED = os.getenv("DATA_REFRESH_ENABLED", "true").lower() == "true"
DATA_REFRESH_INTERVAL_DAYS = int(os.getenv("DATA_REFRESH_INTERVAL_DAYS", "14"))
DATA_REFRESH_ON_STARTUP = os.getenv("DATA_REFRESH_ON_STARTUP", "false").lower() == "true"
DATA_MANIFEST_PATH = os.getenv("DATA_MANIFEST_PATH", "data/data_manifest.json")
