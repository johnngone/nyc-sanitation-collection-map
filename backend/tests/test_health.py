import logging
from pathlib import Path

from fastapi.testclient import TestClient

from app import api
from app import main
from app.main import app


def test_runtime_seo_metadata_is_injected_and_escaped() -> None:
    rendered = main.render_frontend_index(
        """<title id="runtime-browser-title">Default</title>
        <meta id="runtime-apple-mobile-web-app-title" name="apple-mobile-web-app-title" content="Default" />
        <meta id="runtime-meta-description" name="description" content="Default" />
        <!-- runtime-seo -->""",
        app_title="Pickup <Map> & More",
        app_subtitle="Schedules by street > block",
        browser_title='Pickup <Map>',
        app_short_name='Pickup <Map> & More',
        meta_description='Schedules for "everyone" & neighbors.',
        public_url="https://map.example.com",
    )

    assert "<title id=\"runtime-browser-title\">Pickup &lt;Map&gt;</title>" in rendered
    assert (
        'name="apple-mobile-web-app-title" '
        'content="Pickup &lt;Map&gt; &amp; More"'
    ) in rendered
    assert "Schedules for &quot;everyone&quot; &amp; neighbors." in rendered
    assert '<link rel="canonical" href="https://map.example.com" />' in rendered
    assert '<meta property="og:image" content="https://map.example.com/logo.png" />' in rendered
    assert '"url": "https://map.example.com"' in rendered
    assert '"operatingSystem": "Any operating system with a modern web browser"' in rendered
    assert 'id="runtime-app-config"' in rendered
    assert '"title": "Pickup \\u003cMap\\u003e \\u0026 More"' in rendered
    assert '"subtitle": "Schedules by street \\u003e block"' in rendered
    assert "runtime-seo" not in rendered


def test_frontend_index_declares_icon_and_install_metadata() -> None:
    index = (
        Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    ).read_text(encoding="utf-8")

    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg" />' in index
    assert 'sizes="96x96" href="/favicon-96x96.png"' in index
    assert '<link rel="shortcut icon" href="/favicon.ico" />' in index
    assert 'sizes="180x180" href="/apple-touch-icon.png"' in index
    assert '<link rel="manifest" href="/site.webmanifest" />' in index
    assert '<meta name="theme-color" content="#eef2f5" />' in index
    assert '<meta name="apple-mobile-web-app-capable" content="yes" />' in index
    assert 'name="apple-mobile-web-app-title" content="Trash Map"' in index


def test_site_manifest_uses_runtime_branding(monkeypatch) -> None:
    monkeypatch.setattr(main, "APP_BROWSER_TITLE", "Neighborhood Collection Map")
    monkeypatch.setattr(main, "APP_SHORT_NAME", "Pickup Map")
    monkeypatch.setattr(main, "APP_META_DESCRIPTION", "Find collection schedules.")

    response = TestClient(app).get("/site.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/manifest+json"
    assert response.headers["cache-control"] == "no-cache"
    assert response.json() == {
        "name": "Neighborhood Collection Map",
        "short_name": "Pickup Map",
        "description": "Find collection schedules.",
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
    }


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

