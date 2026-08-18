"""Layer 2 — app-level HTTP behavior: cache-revalidation headers.

A new build's CSS/JS was being masked by the browser's heuristic cache (the
assets carry an ETag but no Cache-Control). The app now sends `Cache-Control:
no-cache` on the page and its static assets so browsers revalidate (cheap 304s
when unchanged) and pick up a deploy immediately.
"""

from fastapi.testclient import TestClient
from main import app


def test_static_assets_send_no_cache():
    with TestClient(app) as client:
        css = client.get("/static/style.css")
        assert css.status_code == 200
        assert css.headers.get("cache-control") == "no-cache"
        js = client.get("/static/tooltip.js")
        assert js.status_code == 200
        assert js.headers.get("cache-control") == "no-cache"


def test_non_user_facing_paths_are_untouched():
    # The middleware is scoped — health/metrics endpoints keep default caching.
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.headers.get("cache-control") is None
