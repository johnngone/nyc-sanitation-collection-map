import logging
import html
import json
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .api import router
from .config import (
    APP_BROWSER_TITLE,
    APP_ENV,
    APP_META_DESCRIPTION,
    APP_PUBLIC_URL,
    APP_ROBOTS_TXT,
    APP_SHORT_NAME,
    APP_SUBTITLE,
    APP_TITLE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
SENSOR_PERMISSIONS_POLICY = (
    "geolocation=(self), accelerometer=(self), gyroscope=(self), magnetometer=(self)"
)
RUNTIME_SEO_MARKER = "<!-- runtime-seo -->"
IS_PRODUCTION = APP_ENV.strip().casefold() == "production"
REQUEST_LOG_MIN_STATUS = 400 if IS_PRODUCTION else 0


def render_frontend_index(
    template: str,
    *,
    app_title: str = APP_TITLE,
    app_subtitle: str = APP_SUBTITLE,
    browser_title: str = APP_BROWSER_TITLE,
    app_short_name: str = APP_SHORT_NAME,
    meta_description: str = APP_META_DESCRIPTION,
    public_url: str = APP_PUBLIC_URL,
) -> str:
    escaped_title = html.escape(browser_title, quote=True)
    escaped_short_name = html.escape(app_short_name, quote=True)
    escaped_description = html.escape(meta_description, quote=True)
    rendered = re.sub(
        r'<title id="runtime-browser-title">.*?</title>',
        f'<title id="runtime-browser-title">{escaped_title}</title>',
        template,
        count=1,
        flags=re.DOTALL,
    )
    rendered = re.sub(
        r'<meta id="runtime-apple-mobile-web-app-title" '
        r'name="apple-mobile-web-app-title" content=".*?"\s*/?>',
        (
            '<meta id="runtime-apple-mobile-web-app-title" '
            'name="apple-mobile-web-app-title" '
            f'content="{escaped_short_name}" />'
        ),
        rendered,
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
        "operatingSystem": "Any operating system with a modern web browser",
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
    runtime_app_config = json.dumps(
        {
            "title": app_title,
            "subtitle": app_subtitle,
        },
        ensure_ascii=False,
    )
    runtime_app_config = (
        runtime_app_config
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    metadata.append(
        '<script id="runtime-app-config" type="application/json">'
        f"{runtime_app_config}</script>"
    )
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


app = FastAPI(
    title="NYC Sanitation Map API",
    version="2.0.0",
    license_info={"name": "MIT License", "identifier": "MIT"},
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)


@app.middleware("http")
async def finalize_response(request, call_next):
    response = await call_next(request)
    response.headers["Permissions-Policy"] = SENSOR_PERMISSIONS_POLICY
    if response.status_code >= REQUEST_LOG_MIN_STATUS:
        client = request.client
        client_address = f"{client.host}:{client.port}" if client is not None else "unknown"
        logger.log(
            logging.WARNING if response.status_code >= 400 else logging.INFO,
            "HTTP request method=%s path=%s status=%s client=%s",
            request.method,
            request.url.path,
            response.status_code,
            client_address,
        )
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


@app.get("/site.webmanifest", include_in_schema=False)
def site_manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": APP_BROWSER_TITLE,
            "short_name": APP_SHORT_NAME,
            "description": APP_META_DESCRIPTION,
            "id": "/",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#eef2f5",
            "theme_color": "#eef2f5",
            "icons": [
                {
                    "src": "/web-app-manifest-192x192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": "/web-app-manifest-512x512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
            ],
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )

frontend_directory = Path(__file__).resolve().parents[2] / "frontend-dist"
if frontend_directory.is_dir():
    # Keep the frontend mount last: it is a catch-all and must never intercept /api routes.
    app.mount("/", FrontendStaticFiles(directory=frontend_directory, html=True), name="frontend")


