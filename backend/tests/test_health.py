import logging
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import api
from app import main
from app.main import app


def test_runtime_seo_metadata_is_injected_and_escaped() -> None:
    rendered = main.render_frontend_index(
        """<title id="runtime-browser-title">Default</title>
        <meta id="runtime-meta-description" name="description" content="Default" />
        <!-- runtime-seo -->""",
        app_title="Pickup <Map> & More",
        app_subtitle="Schedules by street > block",
        browser_title='Pickup <Map>',
        meta_description='Schedules for "everyone" & neighbors.',
        public_url="https://map.example.com",
    )

    assert "<title id=\"runtime-browser-title\">Pickup &lt;Map&gt;</title>" in rendered
    assert "Schedules for &quot;everyone&quot; &amp; neighbors." in rendered
    assert '<link rel="canonical" href="https://map.example.com" />' in rendered
    assert '<meta property="og:image" content="https://map.example.com/logo.png" />' in rendered
    assert '"url": "https://map.example.com"' in rendered
    assert 'id="runtime-app-config"' in rendered
    assert '"title": "Pickup \\u003cMap\\u003e \\u0026 More"' in rendered
    assert '"subtitle": "Schedules by street \\u003e block"' in rendered
    assert "runtime-seo" not in rendered


def test_robots_default_to_private_and_sitemap_uses_public_url(monkeypatch) -> None:
    monkeypatch.setattr(main, "APP_PUBLIC_URL", "https://map.example.com")
    monkeypatch.setattr(main, "APP_ROBOTS_TXT", "")
    client = TestClient(app)

    assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "<loc>https://map.example.com</loc>" in sitemap.text


def test_custom_public_robots_text_overrides_private_default(monkeypatch) -> None:
    monkeypatch.setattr(main, "APP_PUBLIC_URL", "https://map.example.com")
    monkeypatch.setattr(
        main,
        "APP_ROBOTS_TXT",
        "User-agent: *\nAllow: /\nSitemap: https://map.example.com/sitemap.xml",
    )

    response = TestClient(app).get("/robots.txt")

    assert response.status_code == 200
    assert response.text == (
        "User-agent: *\nAllow: /\nSitemap: https://map.example.com/sitemap.xml\n"
    )


def test_live_is_independent_of_release_readiness(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api, "DATA_MANIFEST_PATH", str(tmp_path / "missing.json"))
    response = TestClient(app).get("/api/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["permissions-policy"] == (
        "geolocation=(self), accelerometer=(self), gyroscope=(self), magnetometer=(self)"
    )


def test_production_logs_failed_requests_but_not_successes(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(main, "REQUEST_LOG_MIN_STATUS", 400)
    monkeypatch.setattr(api, "DATA_MANIFEST_PATH", str(tmp_path / "missing.json"))
    client = TestClient(app)

    with caplog.at_level(logging.WARNING):
        assert client.get("/api/live").status_code == 200
        assert client.get("/api/health").status_code == 503

    messages = [record.getMessage() for record in caplog.records]
    assert not any("path=/api/live" in message for message in messages)
    assert any("path=/api/health status=503" in message for message in messages)


def test_api_routes_are_before_frontend_catch_all() -> None:
    routes = [getattr(route, "path", None) for route in app.routes]
    if "/" in routes:
        assert routes.index("/api/health") < routes.index("/")


def test_api_documentation_is_development_only() -> None:
    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200

    repository = Path(__file__).resolve().parents[2]
    command = (
        "from fastapi.testclient import TestClient; "
        "from backend.app.main import app; "
        "c=TestClient(app); "
        "assert [c.get(p).status_code for p in ('/docs','/redoc','/openapi.json')] == [404,404,404]"
    )
    environment = {**os.environ, "APP_ENV": "production"}
    subprocess.run(
        [sys.executable, "-c", command],
        cwd=repository,
        env=environment,
        check=True,
    )

