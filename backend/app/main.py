import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import APP_ENV, DATABASE_PATH, DATA_MANIFEST_PATH
from .database import initialize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="NYC Sanitation Map API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_origin_regex=r"https?://([a-zA-Z0-9-]+\.)?localhost:5173",
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(router)

frontend_directory = Path(__file__).resolve().parents[2] / "frontend-dist"
if frontend_directory.is_dir():
    app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="frontend")


@app.on_event("startup")
def startup() -> None:
    initialize(DATABASE_PATH)


@app.get("/api/health")
def health() -> dict[str, object]:
    logger.info("Health check requested")
    try:
        import sqlite3

        with sqlite3.connect(DATABASE_PATH) as connection:
            count = connection.execute("SELECT COUNT(*) FROM block_faces").fetchone()[0]
            schedule_counts = {row[0]: row[1] for row in connection.execute("SELECT collection_type, COUNT(*) FROM collection_schedules GROUP BY collection_type")}
    except sqlite3.Error:
        logger.exception("Health check could not inspect the local database")
        raise
    metadata = {}
    try:
        import json
        from pathlib import Path
        manifest = Path(DATA_MANIFEST_PATH)
        if manifest.exists():
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Could not read dataset manifest path=%s", DATA_MANIFEST_PATH)
    return {"status": "ok", "environment": APP_ENV, "processed_records": count, "schedule_counts": schedule_counts, "data_updated": metadata.get("processed_at"), "data_manifest": metadata.get("manifest_version")}
