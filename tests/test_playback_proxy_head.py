# ruff: noqa: E402
"""Tests for the byte proxy's HEAD handling.

Some stream proxies reject HEAD with 405 while serving range GETs
fine. The proxy must synthesize the HEAD answer from a 1-byte range GET in that
case — otherwise a size-probing client (libav's block reader) sees the 405 and
abandons a perfectly streamable source.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_playback_proxy_head.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from fastapi.testclient import TestClient

from app.main import app


class FakeUpstream:
    """Minimal stand-in for a streamed httpx.Response."""

    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    async def aclose(self):
        return None

    async def aiter_raw(self, _chunk_size):
        yield b"0123456789"


def _proxy_head(fake_send):
    async def fake_safe(url):
        return True

    with (
        patch(
            "app.main._get_active_profile", return_value=SimpleNamespace(is_active=True)
        ),
        patch("app.main._is_safe_source_url_async", new=fake_safe),
        patch("app.main._send_with_validated_redirects", new=fake_send),
    ):
        client = TestClient(app)
        return client.head(
            "/account/api/playback/proxy", params={"url": "https://host/file.mkv"}
        )


def test_head_synthesized_from_range_get_when_upstream_rejects_head():
    calls = []

    async def fake_send(client, method, url, headers, **kwargs):
        calls.append(method)
        if method == "HEAD":
            return FakeUpstream(405)
        assert method == "GET"
        assert headers["Range"] == "bytes=0-0"
        return FakeUpstream(
            206,
            {
                "content-type": "video/x-matroska",
                "content-range": "bytes 0-0/123456789",
                "content-length": "1",
            },
        )

    resp = _proxy_head(fake_send)
    assert calls == ["HEAD", "GET"]
    assert resp.status_code == 200
    assert resp.headers["content-length"] == "123456789"
    assert resp.headers["content-type"] == "video/x-matroska"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["cache-control"] == "no-store"


def test_head_relayed_directly_when_upstream_supports_it():
    calls = []

    async def fake_send(client, method, url, headers, **kwargs):
        calls.append(method)
        assert method == "HEAD"
        return FakeUpstream(200, {"content-type": "video/mp4", "content-length": "42"})

    resp = _proxy_head(fake_send)
    assert calls == ["HEAD"]
    assert resp.status_code == 200
    assert resp.headers["content-length"] == "42"


def test_head_405_still_relayed_when_range_fallback_also_fails():
    async def fake_send(client, method, url, headers, **kwargs):
        if method == "HEAD":
            return FakeUpstream(405)
        # Range fallback hits a host that can't serve ranges either.
        return FakeUpstream(200, {"content-type": "video/mp4"})

    resp = _proxy_head(fake_send)
    # A 200 without a Content-Range gives no size — keep the honest 405.
    assert resp.status_code == 405


def test_gzip_never_transforms_a_ranged_media_response():
    async def fake_safe(_url):
        return True

    async def fake_send(_client, method, _url, headers, **_kwargs):
        assert method == "GET"
        assert headers["Range"] == "bytes=10-19"
        return FakeUpstream(
            206,
            {
                "content-type": "video/mp4",
                "content-range": "bytes 10-19/100",
                "content-length": "10",
                "accept-ranges": "bytes",
            },
        )

    with (
        patch(
            "app.main._get_active_profile", return_value=SimpleNamespace(is_active=True)
        ),
        patch("app.main._is_safe_source_url_async", new=fake_safe),
        patch("app.main._send_with_validated_redirects", new=fake_send),
    ):
        response = TestClient(app).get(
            "/account/api/playback/proxy",
            params={"url": "https://host/file.mp4"},
            headers={"Range": "bytes=10-19", "Accept-Encoding": "gzip"},
        )

    assert response.status_code == 206
    assert response.content == b"0123456789"
    assert response.headers["content-range"] == "bytes 10-19/100"
    assert response.headers["content-length"] == "10"
    assert "content-encoding" not in response.headers


def test_ordinary_static_documents_still_compress():
    response = TestClient(app).get(
        "/static/auth.css", headers={"Accept-Encoding": "gzip"}
    )
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
