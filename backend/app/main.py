import logging
import html
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import HTMLResponse, PlainTextResponse, Response

from .api import router
from .config import (
    APP_BROWSER_TITLE,
    APP_META_DESCRIPTION,
    APP_PUBLIC_URL,
    APP_ROBOTS_TXT,
    DATABASE_PATH,
    DATA_MANIFEST_PATH,
)
from .database import initialize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
SENSOR_PERMISSIONS_POLICY = (
    "geolocation=(self), accelerometer=(self), gyroscope=(self), magnetometer=(self)"
)
RUNTIME_SEO_MARKER = "<!-- runtime-seo -->"


def render_frontend_index(
    template: str,
    *,
    browser_title: str = APP_BROWSER_TITLE,
    meta_description: str = APP_META_DESCRIPTION,
    public_url: str = APP_PUBLIC_URL,
) -> str:
    escaped_title = html.escape(browser_title, quote=True)
    escaped_description = html.escape(meta_description, quote=True)
    rendered = re.sub(
        r'<title id="runtime-browser-title">.*?</title>',
        f'<title id="runtime-browser-title">{escaped_title}</title>',
        template,
        count=1,
        flags=re.DOTALL,
    )
    rendered = re.sub(
        r'<meta id="runtime-meta-description" name="description" content=".*?"\s*/?>',
        (
            '<meta id="runtime-meta-description" name="description" '
            f'content="{escaped_description}" />'
        ),
        rendered,
        count=1,
        flags=re.DOTALL,
    )
    metadata = [
        f'<meta property="og:title" content="{escaped_title}" />',
        f'<meta property="og:description" content="{escaped_description}" />',
        '<meta property="og:type" content="website" />',
        '<meta name="twitter:card" content="summary" />',
        f'<meta name="twitter:title" content="{escaped_title}" />',
        f'<meta name="twitter:description" content="{escaped_description}" />',
    ]
    structured_data: dict[str, str] = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": browser_title,
        "description": meta_description,
        "applicationCategory": "UtilitiesApplication",
    }
    if public_url:
        escaped_url = html.escape(public_url, quote=True)
        escaped_image_url = html.escape(f"{public_url}/logo.png", quote=True)
        metadata.extend(
            [
                f'<link rel="canonical" href="{escaped_url}" />',
                f'<meta property="og:url" content="{escaped_url}" />',
                f'<meta property="og:image" content="{escaped_image_url}" />',
                f'<meta name="twitter:image" content="{escaped_image_url}" />',
            ]
        )
        structured_data["url"] = public_url
    json_ld = json.dumps(structured_data, ensure_ascii=False).replace("<", "\\u003c")
    metadata.append(f'<script type="application/ld+json">{json_ld}</script>')
    return rendered.replace(RUNTIME_SEO_MARKER, "\n    ".join(metadata), 1)


class FrontendStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, object]) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response_path = Path(getattr(response, "path", ""))
            if response_path.name == "index.html":
                return HTMLResponse(
                    render_frontend_index(response_path.read_text(encoding="utf-8")),
                    headers={"Cache-Control": "no-cache"},
                )
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


app = FastAPI(
    title="NYC Sanitation Map API",
    version="0.1.0",
    license_info={"name": "MIT License", "identifier": "MIT"},
    lifespan=lifespan,
)


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


@app.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    if APP_ROBOTS_TXT:
        return PlainTextResponse(
            APP_ROBOTS_TXT + "\n",
            headers={"Cache-Control": "no-cache"},
        )
    return PlainTextResponse(
        "User-agent: *\nDisallow: /\n",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> Response:
    url = html.escape(APP_PUBLIC_URL, quote=True)
    entry = f"<url><loc>{url}</loc></url>" if url else ""
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entry}</urlset>"
    )
    return Response(content, media_type="application/xml", headers={"Cache-Control": "no-cache"})

frontend_directory = Path(__file__).resolve().parents[2] / "frontend-dist"
if frontend_directory.is_dir():
    # Keep the frontend mount last: it is a catch-all and must never intercept /api routes.
    app.mount("/", FrontendStaticFiles(directory=frontend_directory, html=True), name="frontend")


