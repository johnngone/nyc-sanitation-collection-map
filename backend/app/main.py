import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import DATABASE_PATH
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
    # Keep the frontend mount last: it is a catch-all and must never intercept /api routes.
    app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="frontend")


@app.on_event("startup")
def startup() -> None:
    initialize(DATABASE_PATH)


