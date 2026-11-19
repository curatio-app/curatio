# ruff: noqa: E402
"""Serving and coverage guards for the compiled React admin center."""

import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_admin_page_js.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

ROOT = Path(__file__).resolve().parents[1]


def _react_admin_source() -> str:
    return "\n".join(
        path.read_text()
        for path in sorted((ROOT / "src" / "admin").rglob("*"))
        if path.suffix in {".ts", ".tsx"} and ".test." not in path.name
    )


def test_admin_entry_serves_compact_login_shell():
    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/admin")
    assert response.status_code == 200
    assert "Curatio Admin" in response.text
    assert "/admin/api/login" in response.text
    assert "Start Re-tag" not in response.text


def test_admin_spa_fallback_does_not_swallow_unknown_api_routes():
    from app.main import app

    response = TestClient(app, base_url="https://testserver").get(
        "/admin/api/not-a-real-endpoint"
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_legacy_admin_bundle_and_links_are_retired():
    backend = (ROOT / "app" / "admin.py").read_text()
    frontend = _react_admin_source()

    assert "def _admin_html" not in backend
    assert 'RedirectResponse("/admin/overview", status_code=308)' in backend
    assert "/admin/legacy" not in frontend
    assert "Legacy specialist" not in frontend


def test_new_admin_reaches_every_required_operational_surface():
    source = _react_admin_source()
    required_paths = (
        "/build/start",
        "/build/daily",
        "/build/pause",
        "/build/resume",
        "/build/stop",
        "/sync-users",
        "/catalog/rows",
        "/catalog/quality/preview",
        "/categories/synth/preview",
        "/categories/synth/config",
        "/relevance/refresh",
        "/tagging/upgrade/preflight",
        "/tagging/upgrade/keys",
        "/tagging/upgrade/start",
        "/tagging/upgrade/status",
        "/tagging/upgrade/stop",
        "/tagging/repair/status",
        "/tagging/repair/retag-stale",
        "/tagging/rebuild/status",
        "/tagging/rebuild/start",
        "/tags/offvocab",
        "/tags/alias",
        "/tags/alias/sample-ids",
        "/tags/dead",
        "/tags/list",
        "/users",
        "/sessions",
        "/fingerprint-status",
        "/fingerprint-series",
        "/fingerprint/relearn-all",
        "/settings",
        "/debug",
    )
    missing = [path for path in required_paths if path not in source]
    assert not missing, f"New admin does not expose required operations: {missing}"


def test_dead_settings_controls_are_not_reintroduced():
    source = _react_admin_source()
    assert "MASTER_PASSWORD" not in source


def test_catalog_feature_toggles_are_real_settings():
    from app.config import Settings

    assert Settings.model_fields["enable_universal_catalogs"].default is True
    assert Settings.model_fields["enable_personalized_catalogs"].default is True
