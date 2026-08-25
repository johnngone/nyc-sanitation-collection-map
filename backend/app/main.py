import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from .api import router
from .config import DATABASE_PATH, DATA_MANIFEST_PATH
from .database import initialize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
SENSOR_PERMISSIONS_POLICY = (
    "geolocation=(self), accelerometer=(self), gyroscope=(self), magnetometer=(self)"
)


class FrontendStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, object]) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            if path.replace("\\", "/").startswith("assets/"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Existing root databases are legacy snapshots and must remain readable as
    # deployed while the background refresh builds the first immutable v2 release.
    # Only bootstrap storage when neither form of data exists.
    if not Path(DATABASE_PATH).exists() and not Path(DATA_MANIFEST_PATH).exists():
        initialize(DATABASE_PATH)
    yield


app = FastAPI(title="NYC Sanitation Map API", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def add_sensor_permissions_policy(request, call_next):
    response = await call_next(request)
    response.headers["Permissions-Policy"] = SENSOR_PERMISSIONS_POLICY
    return response


app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
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
    app.mount("/", FrontendStaticFiles(directory=frontend_directory, html=True), name="frontend")


