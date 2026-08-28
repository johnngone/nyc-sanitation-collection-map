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
    assert '"url": "https://map.example.com"' in rendered
    assert '"operatingSystem": "Any operating system with a modern web browser"' in rendered
    assert 'id="runtime-app-config"' in rendered
    assert '"title": "Pickup \\u003cMap\\u003e \\u0026 More"' in rendered
    assert '"subtitle": "Schedules by street \\u003e block"' in rendered
    assert "runtime-seo" not in rendered


def test_site_manifest_uses_runtime_branding(monkeypatch) -> None:
    monkeypatch.setattr(main, "APP_BROWSER_TITLE", "Neighborhood Collection Map")
    monkeypatch.setattr(main, "APP_SHORT_NAME", "Pickup Map")
    monkeypatch.setattr(main, "APP_META_DESCRIPTION", "Find collection schedules.")

    response = TestClient(app).get("/site.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/manifest+json"
    assert response.headers["cache-control"] == "no-cache"
    payload = response.json()
    assert {key: payload[key] for key in (
        "name",
        "short_name",
        "description",
        "id",
        "start_url",
        "scope",
        "display",
        "background_color",
        "theme_color",
    )} == {
        "name": "Neighborhood Collection Map",
        "short_name": "Pickup Map",
        "description": "Find collection schedules.",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#eef2f5",
        "theme_color": "#eef2f5",
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



