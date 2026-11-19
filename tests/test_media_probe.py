# ruff: noqa: E402
"""ffprobe facts for the web player's playback path decision."""

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_media_probe.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.database import get_db_dependency
from app.main import app
from app.media_probe import (
    MediaInspector,
    MediaProbeError,
    MediaProbeTimeout,
    audio_codec_string,
    classify_ffprobe_error,
    h264_codec_string,
    hevc_codec_string,
    media_inspector,
    parse_ffprobe,
)

WEB_DL_MKV = {
    "format": {
        "format_name": "matroska,webm",
        "duration": "2701.312000",
        "size": "3221225472",
        "bit_rate": "9538000",
        "start_time": "0.000000",
    },
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "hevc",
            "profile": "Main 10",
            "level": 153,
            "pix_fmt": "yuv420p10le",
            "width": 3840,
            "height": 2160,
            "avg_frame_rate": "24000/1001",
            "color_transfer": "smpte2084",
            "side_data_list": [
                {"side_data_type": "DOVI configuration record", "dv_profile": 8}
            ],
            "disposition": {"default": 1},
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "eac3",
            "profile": "Dolby Digital Plus + Dolby Atmos",
            "channels": 6,
            "tags": {"language": "eng", "title": "DDP 5.1 Atmos"},
            "disposition": {"default": 1},
        },
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "aac",
            "profile": "LC",
            "channels": 2,
            "tags": {"language": "jpn"},
        },
        {
            "index": 3,
            "codec_type": "subtitle",
            "codec_name": "subrip",
            "tags": {"language": "eng", "title": "English (Forced)"},
            "disposition": {"forced": 0},
        },
        {
            "index": 4,
            "codec_type": "subtitle",
            "codec_name": "hdmv_pgs_subtitle",
            "tags": {"language": "eng"},
        },
        {
            "index": 5,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "disposition": {"attached_pic": 1},
        },
    ],
}


def test_web_dl_facts_name_real_codecs_not_file_name_guesses():
    facts = parse_ffprobe(WEB_DL_MKV)
    assert facts.container == "matroska"
    assert facts.duration == pytest.approx(2701.312)
    assert facts.size == 3221225472
    assert facts.video is not None
    assert facts.video.codec == "hevc"
    assert facts.video.bit_depth == 10
    assert facts.video.codec_string == "hvc1.2.4.L153.B0"
    assert facts.video.dynamic_range == "dv"
    assert facts.video.dv_profile == 8
    assert facts.video.frame_rate == pytest.approx(23.976)
    # "Atmos" is E-AC-3 here, not TrueHD.
    assert [a.codec for a in facts.audio] == ["eac3", "aac"]
    assert facts.audio[0].codec_string == "ec-3"
    assert facts.audio[0].channels == 6
    assert facts.audio[1].codec_string == "mp4a.40.2"
    assert [(s.codec, s.text, s.forced) for s in facts.subtitles] == [
        ("subrip", True, True),
        ("hdmv_pgs_subtitle", False, False),
    ]
    assert facts.to_json()["video"]["codec_string"] == "hvc1.2.4.L153.B0"


def test_cover_art_is_not_mistaken_for_the_video_track():
    data = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "60"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "png",
                "disposition": {"attached_pic": 1},
            },
            {
                "index": 1,
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "level": 40,
                "pix_fmt": "yuv420p",
                "color_transfer": "bt709",
            },
        ],
    }
    facts = parse_ffprobe(data)
    assert facts.container == "mp4"
    assert facts.video.index == 1
    assert facts.video.codec_string == "avc1.640028"
    assert facts.video.dynamic_range == "sdr"


def test_codec_strings():
    assert h264_codec_string("Constrained Baseline", 30) == "avc1.42401E"
    assert h264_codec_string("Main", 31) == "avc1.4D401F"
    assert h264_codec_string("High 10", 51) == "avc1.6E0033"
    assert h264_codec_string("High", None) is None
    assert hevc_codec_string("Main", 120) == "hvc1.1.6.L120.B0"
    assert hevc_codec_string("Unknown", 120) is None
    assert audio_codec_string("truehd") == "mlpa"
    assert audio_codec_string("dts") == "dtsc"
    assert audio_codec_string("aac", "HE-AAC") == "mp4a.40.5"
    assert audio_codec_string("pcm_s16le") is None


def test_ffprobe_errors_map_to_recovery_categories():
    assert (
        classify_ffprobe_error("Server returned 403 Forbidden (access denied)")
        == "expired"
    )
    assert (
        classify_ffprobe_error(
            "Server returned 4XX Client Error, but not one of 40{0,1,3,4}"
        )
        == "expired"
    )
    assert classify_ffprobe_error("Server returned 404 Not Found") == "gone"
    assert classify_ffprobe_error("Server returned 5XX Server Error reply") == "network"
    assert (
        classify_ffprobe_error("Invalid data found when processing input")
        == "not_media"
    )
    assert classify_ffprobe_error("something else") == "probe_failed"


def test_empty_probe_is_not_media():
    with pytest.raises(MediaProbeError) as raised:
        parse_ffprobe({"format": {}, "streams": []})
    assert raised.value.reason == "not_media"


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, delay=0.0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._delay = delay

    async def communicate(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._stdout, self._stderr

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_inspector_caches_success_shares_in_flight_work_and_reads_via_proxy():
    inspector = MediaInspector()
    spawn = AsyncMock(
        return_value=FakeProcess(stdout=json.dumps(WEB_DL_MKV).encode(), delay=0.01)
    )
    with patch("app.media_probe.asyncio.create_subprocess_exec", new=spawn):
        first, second = await asyncio.gather(
            inspector.inspect(
                "https://cdn/a.mkv", "http://127.0.0.1/proxy", proxy_token="t"
            ),
            inspector.inspect(
                "https://cdn/a.mkv", "http://127.0.0.1/proxy", proxy_token="t"
            ),
        )
        third = await inspector.inspect(
            "https://cdn/a.mkv", "http://127.0.0.1/proxy", proxy_token="t"
        )
    assert spawn.await_count == 1
    assert first == second == third
    command = list(spawn.await_args.args)
    assert command[0] == "ffprobe"
    assert "https://cdn/a.mkv" not in command
    assert command[-1] == "http://127.0.0.1/proxy"
    assert command[command.index("-headers") + 1] == "X-Curatio-Remux-Token: t\r\n"
    assert inspector.cached("https://cdn/a.mkv") is first


@pytest.mark.asyncio
async def test_failures_are_cached_briefly_and_timeouts_not_at_all():
    inspector = MediaInspector(timeout_seconds=1)
    failing = AsyncMock(
        return_value=FakeProcess(stderr=b"Server returned 404 Not Found", returncode=1)
    )
    with patch("app.media_probe.asyncio.create_subprocess_exec", new=failing):
        for _ in range(2):
            with pytest.raises(MediaProbeError) as raised:
                await inspector.inspect("https://cdn/gone", "http://p", proxy_token="t")
            assert raised.value.reason == "gone"
    assert failing.await_count == 1

    slow = AsyncMock(return_value=FakeProcess(delay=5))
    with patch("app.media_probe.asyncio.create_subprocess_exec", new=slow):
        with pytest.raises(MediaProbeTimeout):
            await inspector.inspect("https://cdn/slow", "http://p", proxy_token="t")
    assert "https://cdn/slow" not in inspector._cache


def _client():
    client = TestClient(app)
    app.dependency_overrides[get_db_dependency] = lambda: SimpleNamespace(
        close=lambda: None
    )
    return client


def test_inspect_endpoint_returns_facts_and_categories_per_url():
    client = _client()
    facts = parse_ffprobe(WEB_DL_MKV)

    async def safe(url):
        return "internal" not in url

    async def fake_inspect(url, proxy_url, *, proxy_token):
        assert proxy_url.startswith("http://127.0.0.1:")
        if url.endswith("ok.mkv"):
            return facts
        if url.endswith("slow.mkv"):
            raise MediaProbeTimeout()
        raise MediaProbeError("expired")

    urls = [
        "https://cdn.example/ok.mkv",
        "https://cdn.example/slow.mkv",
        "https://cdn.example/old.mkv",
        "http://internal/secret",
        "https://cdn.example/ok.mkv",
    ]
    try:
        with (
            patch(
                "app.main._get_active_profile",
                return_value=SimpleNamespace(is_active=True, id=1),
            ),
            patch("app.main._is_safe_source_url_async", new=safe),
            patch.object(media_inspector, "inspect", new=fake_inspect),
        ):
            response = client.post("/account/api/playback/inspect", json={"urls": urls})
        assert response.status_code == 200
        results = response.json()["results"]
        assert set(results) == set(urls[:4])
        assert results[urls[0]]["state"] == "ok"
        assert results[urls[0]]["media"]["video"]["codec_string"] == "hvc1.2.4.L153.B0"
        assert results[urls[1]] == {"state": "indeterminate", "reason": "timeout"}
        assert results[urls[2]] == {"state": "failed", "reason": "expired"}
        assert results[urls[3]] == {"state": "failed", "reason": "unsafe_url"}
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_probe_rejects_an_html_error_page_served_with_200(monkeypatch):
    import httpx
    import app.main as main

    class Page:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8", "content-length": "512"}

        async def aclose(self):
            return None

    class Media:
        status_code = 206
        headers = {
            "content-type": "video/x-matroska",
            "content-range": "bytes 0-1023/987654321",
        }

        async def aclose(self):
            return None

    monkeypatch.setattr(main, "_is_safe_source_url_async", AsyncMock(return_value=True))
    async with httpx.AsyncClient() as client:
        monkeypatch.setattr(
            main, "_send_with_validated_redirects", AsyncMock(return_value=Page())
        )
        html = await main._probe_one(client, "https://cdn.example/expired")
        monkeypatch.setattr(
            main, "_send_with_validated_redirects", AsyncMock(return_value=Media())
        )
        media = await main._probe_one(client, "https://cdn.example/movie.mkv")
    assert html == {"state": "failed", "reason": "not_media", "status": 200}
    assert media["state"] == "passed"
    assert media["size"] == 987654321
    assert media["range"] is True
    assert media["content_type"] == "video/x-matroska"


@pytest.mark.asyncio
async def test_probe_blames_an_unavailable_provider(monkeypatch):
    """A text answer saying the provider is down excludes the provider (native parity)."""
    import httpx
    import app.main as main

    class Down:
        status_code = 503
        headers = {"content-type": "application/json"}

        async def aiter_bytes(self):
            yield b'{"error": "Provider is unavailable right now"}'

        async def aclose(self):
            return None

    class OtherError:
        status_code = 503
        headers = {"content-type": "text/plain"}

        async def aiter_bytes(self):
            yield b"upstream timed out"

        async def aclose(self):
            return None

    monkeypatch.setattr(main, "_is_safe_source_url_async", AsyncMock(return_value=True))
    async with httpx.AsyncClient() as client:
        monkeypatch.setattr(
            main, "_send_with_validated_redirects", AsyncMock(return_value=Down())
        )
        down = await main._probe_one(client, "https://cdn.example/a")
        monkeypatch.setattr(
            main, "_send_with_validated_redirects", AsyncMock(return_value=OtherError())
        )
        other = await main._probe_one(client, "https://cdn.example/b")
    assert down == {"state": "failed", "reason": "provider_unavailable", "status": 503}
    assert other == {"state": "failed", "reason": "http_error", "status": 503}
