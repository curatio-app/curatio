# ruff: noqa: E402
"""Phase B browser-playback hardening: ffmpeg exit watching, proxy expiry,
mid-body resume, resolved-URL caching and the server-transcode policy."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_playback_proxy_hardening.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

import app.main as main
import app.remux as remux
from app.main import app
from app.media_probe import AudioFacts, MediaFacts, VideoFacts
from app.remux import (
    DESKTOP_APP_HINT,
    FfmpegCapabilities,
    MediaProbe,
    RemuxCapacityError,
    RemuxManager,
    RemuxSession,
    RemuxSessionFailed,
    UnsupportedRemuxVideoError,
    remux_manager,
)

PROFILE = SimpleNamespace(id=42, is_active=True)


async def _safe(_url):
    return True


@pytest.fixture(autouse=True)
def _clean_caches():
    main._resolved_url_cache.clear()
    main._proxy_auth_cache.clear()
    yield
    main._resolved_url_cache.clear()
    main._proxy_auth_cache.clear()


# --- helpers -----------------------------------------------------------------


class LineStderr:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class ExitingProcess:
    """An ffmpeg stand-in that has already exited with ``code``."""

    def __init__(self, code, stderr_lines=()):
        self.returncode = code
        self.stderr = LineStderr(stderr_lines)
        self.pid = 4343

    async def wait(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class Upstream:
    """A streamed httpx.Response stand-in."""

    def __init__(
        self, status_code, headers=None, chunks=(b"",), fail_after=None, url=None
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self._fail_after = fail_after
        self.closed = False
        if url is not None:
            self.url = url

    async def aiter_raw(self, _size):
        for index, chunk in enumerate(self._chunks):
            yield chunk
            if self._fail_after is not None and index + 1 >= self._fail_after:
                raise httpx.ReadError("connection reset by peer")

    async def aclose(self):
        self.closed = True


def _proxy_get(fake_send, *, method="GET", headers=None, url="https://host/file.mkv"):
    with (
        patch("app.main._get_active_profile", return_value=PROFILE),
        patch("app.main._is_safe_source_url_async", new=_safe),
        patch("app.main._send_with_validated_redirects", new=fake_send),
    ):
        client = TestClient(app)
        return client.request(
            method,
            "/account/api/playback/proxy",
            params={"url": url},
            headers=headers or {},
        )


async def _spawn_with(manager, session, process, **kwargs):
    spawn = AsyncMock(return_value=process)
    with patch("app.remux.asyncio.create_subprocess_exec", new=spawn):
        await manager._spawn_ffmpeg(
            session,
            "http://127.0.0.1/proxy?url=source",
            proxy_token="signed-token",
            audio_stream_index=1,
            **kwargs,
        )
    # Let the stderr reader and exit watcher run.
    for _ in range(5):
        await asyncio.sleep(0)
    if manager._watchers:
        await asyncio.gather(*list(manager._watchers))
    return list(spawn.await_args.args)


# --- ffmpeg exit watching -------------------------------------------------------


@pytest.mark.asyncio
async def test_ffmpeg_failure_marks_session_failed_and_assets_error_at_once(tmp_path):
    manager = RemuxManager()
    session = RemuxSession("dead", 7, "https://cdn/movie.mkv", tmp_path)
    manager._sessions[session.session_id] = session
    (tmp_path / "index.m3u8").write_text("#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n")
    (tmp_path / "seg_00000.m4s").write_bytes(b"segment")
    process = ExitingProcess(
        1, [b"[http] HTTP error 410 Gone\n", b"Conversion failed!\n"]
    )
    await _spawn_with(manager, session, process)

    assert session.failed is True
    assert session.exit_code == 1
    assert "Conversion failed" in session.failure
    with pytest.raises(RemuxSessionFailed):
        manager.file_path("dead", "index.m3u8", profile_id=7)
    with pytest.raises(RemuxSessionFailed):
        manager.file_path("dead", "seg_00001.m4s", profile_id=7)
    # What was already produced still plays.
    assert manager.file_path("dead", "seg_00000.m4s", profile_id=7) is not None


def test_failed_session_playlist_endpoint_returns_502_immediately(tmp_path):
    session = RemuxSession(
        "dead-endpoint", PROFILE.id, "https://cdn/movie.mkv", tmp_path
    )
    session.failed = True
    session.failure = "Playback preparation failed (ffmpeg exit 1)"
    remux_manager._sessions[session.session_id] = session
    try:
        with patch("app.main._get_active_profile", return_value=PROFILE):
            response = TestClient(app).get(
                "/account/api/playback/remux/dead-endpoint/index.m3u8"
            )
        assert response.status_code == 502
        assert response.headers["x-curatio-error"] == "session-failed"
        assert "ffmpeg exit 1" in response.json()["detail"]
    finally:
        remux_manager._sessions.pop(session.session_id, None)


@pytest.mark.asyncio
async def test_ffmpeg_success_appends_endlist_once(tmp_path):
    manager = RemuxManager()
    session = RemuxSession("done", 7, "https://cdn/movie.mkv", tmp_path)
    manager._sessions[session.session_id] = session
    playlist = tmp_path / "index.m3u8"
    playlist.write_text(
        "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:4.0,\nseg_00000.m4s"
    )
    await _spawn_with(manager, session, ExitingProcess(0))

    assert session.failed is False
    text = playlist.read_text()
    assert text.rstrip().endswith("#EXT-X-ENDLIST")
    assert "seg_00000.m4s\n#EXT-X-ENDLIST" in text
    manager.note_ffmpeg_exit(session, 0)
    assert playlist.read_text().count("#EXT-X-ENDLIST") == 1


@pytest.mark.asyncio
async def test_exit_after_a_deliberate_stop_is_ignored(tmp_path):
    manager = RemuxManager()
    session = RemuxSession("stopped", 7, "https://cdn/movie.mkv", tmp_path)
    # Not registered: stop() already removed it.
    await _spawn_with(manager, session, ExitingProcess(255))
    assert session.failed is False


# --- ffmpeg command ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_remux_adds_av_sync_flags_and_paces_only_when_supported(tmp_path):
    manager = RemuxManager()
    session = RemuxSession("flags", 7, "https://cdn/movie.mkv", tmp_path)
    manager.capabilities = FfmpegCapabilities(readrate=False, zscale=True)
    command = await _spawn_with(manager, session, ExitingProcess(None))
    assert command[command.index("-af") + 1] == "aresample=async=1"
    assert command[command.index("-avoid_negative_ts") + 1] == "make_zero"
    assert command[command.index("-max_muxing_queue_size") + 1] == "1024"
    assert "-readrate" not in command

    manager.capabilities = FfmpegCapabilities(readrate=True, zscale=True)
    seeking = RemuxSession(
        "paced", 7, "https://cdn/movie.mkv", tmp_path, start_offset=60
    )
    command = await _spawn_with(manager, seeking, ExitingProcess(None))
    assert command[command.index("-readrate") + 1] == "1.5"
    assert command[command.index("-readrate_initial_burst") + 1] == "30"
    assert command.index("-readrate") < command.index("-i")
    # Positive copyts start times are kept for the media_start measurement.
    assert command[command.index("-avoid_negative_ts") + 1] == "make_non_negative"


@pytest.mark.asyncio
async def test_transcode_is_light_and_tonemaps_hdr_with_a_zscale_fallback(tmp_path):
    manager = RemuxManager()
    session = RemuxSession("tx", 7, "https://cdn/movie.mkv", tmp_path)
    manager.capabilities = FfmpegCapabilities(readrate=False, zscale=True)
    command = await _spawn_with(
        manager,
        session,
        ExitingProcess(None),
        video_mode="transcode",
        target_height=2160,
        dynamic_range="hdr10",
    )
    assert command[command.index("-preset") + 1] == "superfast"
    assert command[command.index("-threads") + 1] == "2"
    vf = command[command.index("-vf") + 1]
    assert "min(1080,ih)" in vf
    assert (
        "zscale=tin=smpte2084" in vf
        and "t=linear" in vf
        and "tonemap=tonemap=hable" in vf
    )
    assert vf.endswith("format=yuv420p")

    command = await _spawn_with(
        manager,
        session,
        ExitingProcess(None),
        video_mode="transcode",
        dynamic_range="hlg",
    )
    assert "zscale=tin=arib-std-b67" in command[command.index("-vf") + 1]

    manager.capabilities = FfmpegCapabilities(readrate=False, zscale=False)
    command = await _spawn_with(
        manager,
        session,
        ExitingProcess(None),
        video_mode="transcode",
        dynamic_range="hdr10",
    )
    vf = command[command.index("-vf") + 1]
    assert "zscale" not in vf
    assert "in_color_matrix=bt2020" in vf

    command = await _spawn_with(
        manager, session, ExitingProcess(None), video_mode="transcode"
    )
    assert "tonemap" not in command[command.index("-vf") + 1]


# --- transcode policy ---------------------------------------------------------------


HEVC_HDR = MediaProbe(100, "hevc", "main 10", "yuv420p10le", 1, dynamic_range="hdr10")


@pytest.mark.asyncio
async def test_transcode_disabled_refuses_with_desktop_hint():
    manager = RemuxManager(allow_transcode=False)
    with (
        patch.object(manager, "_probe_media", new=AsyncMock(return_value=HEVC_HDR)),
        patch.object(manager, "_spawn_ffmpeg", new=AsyncMock()) as spawn,
    ):
        with pytest.raises(UnsupportedRemuxVideoError) as caught:
            await manager.start(
                "https://cdn/hdr.mkv",
                "http://proxy/hdr",
                proxy_token="t",
                profile_id=1,
                allow_transcode=True,
            )
    assert caught.value.hint == DESKTOP_APP_HINT
    assert "desktop app" in str(caught.value)
    spawn.assert_not_awaited()
    assert not manager._sessions  # reservation released


def test_transcode_disabled_maps_to_422_with_desktop_hint():
    error = UnsupportedRemuxVideoError("Needs transcoding", hint=DESKTOP_APP_HINT)
    with (
        patch("app.main._get_active_profile", return_value=PROFILE),
        patch("app.main._is_safe_source_url_async", new=_safe),
        patch.object(remux_manager, "start", new=AsyncMock(side_effect=error)),
    ):
        response = TestClient(app).post(
            "/account/api/playback/remux/start",
            json={"url": "https://cdn.example/movie.mkv", "allow_transcode": True},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "message": "Needs transcoding",
        "hint": "desktop_app",
    }


def test_busy_server_sends_retry_after():
    with (
        patch("app.main._get_active_profile", return_value=PROFILE),
        patch("app.main._is_safe_source_url_async", new=_safe),
        patch.object(
            remux_manager,
            "start",
            new=AsyncMock(side_effect=RemuxCapacityError("busy")),
        ),
    ):
        response = TestClient(app).post(
            "/account/api/playback/remux/start",
            json={"url": "https://cdn.example/movie.mkv"},
        )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "3"


@pytest.mark.asyncio
async def test_transcode_enabled_allows_one_concurrent_transcode_only():
    manager = RemuxManager(allow_transcode=True, max_transcodes=1)
    copyable = MediaProbe(100, "h264", "high", "yuv420p", 1)
    probes = AsyncMock(side_effect=[HEVC_HDR, HEVC_HDR, copyable])
    with (
        patch.object(manager, "_probe_media", new=probes),
        patch.object(manager, "_spawn_ffmpeg", new=AsyncMock()) as spawn,
        patch.object(manager, "_wait_for_playlist", new=AsyncMock()),
        patch(
            "app.remux.detect_ffmpeg_capabilities",
            new=AsyncMock(return_value=FfmpegCapabilities()),
        ),
    ):
        first = await manager.start(
            "https://cdn/a.mkv",
            "http://proxy/a",
            proxy_token="t",
            profile_id=1,
            allow_transcode=True,
        )
        assert first.video_mode == "transcode"
        assert spawn.await_args.kwargs["dynamic_range"] == "hdr10"
        with pytest.raises(RemuxCapacityError):
            await manager.start(
                "https://cdn/b.mkv",
                "http://proxy/b",
                proxy_token="t",
                profile_id=1,
                allow_transcode=True,
            )
        # Copy sessions keep their own (larger) cap.
        copied = await manager.start(
            "https://cdn/c.mkv", "http://proxy/c", proxy_token="t", profile_id=1
        )
        assert copied.video_mode == "copy"
    await manager.shutdown()


def test_allow_server_transcode_defaults_off():
    from app.config import Settings

    assert Settings.model_fields["allow_server_transcode"].default is False
    # The manager follows the live (admin-editable) setting.
    manager = RemuxManager()
    with patch.object(remux, "settings", SimpleNamespace(allow_server_transcode=False)):
        assert manager.transcode_enabled is False
    with patch.object(remux, "settings", SimpleNamespace(allow_server_transcode=True)):
        assert manager.transcode_enabled is True


@pytest.mark.asyncio
async def test_start_reuses_the_inspector_probe_cache():
    facts = MediaFacts(
        container="matroska",
        duration=5400.0,
        bitrate=None,
        size=None,
        start_time=0.0,
        video=VideoFacts(
            0, "h264", "High", 41, "yuv420p", 8, 1920, 1080, 24.0, "sdr", None, None
        ),
        audio=[
            AudioFacts(1, "eac3", "", 6, "eng", "", True, None),
            AudioFacts(2, "aac", "LC", 2, "jpn", "", False, None),
        ],
    )
    inspector = SimpleNamespace(
        cached=lambda url: facts if url == "https://cdn/known.mkv" else None
    )
    manager = RemuxManager(inspector=inspector)
    with (
        patch.object(manager, "_probe_media", new=AsyncMock()) as probe,
        patch.object(manager, "_spawn_ffmpeg", new=AsyncMock()) as spawn,
        patch.object(manager, "_wait_for_playlist", new=AsyncMock()),
        patch(
            "app.remux.detect_ffmpeg_capabilities",
            new=AsyncMock(return_value=FfmpegCapabilities()),
        ),
    ):
        session = await manager.start(
            "https://cdn/known.mkv",
            "http://proxy/known",
            proxy_token="t",
            profile_id=1,
            audio_language="ja",
        )
    probe.assert_not_awaited()
    assert session.duration == 5400.0
    assert spawn.await_args.kwargs["audio_stream_index"] == 2
    await manager.shutdown()


# --- proxy ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ctype",
    ["text/html; charset=utf-8", "application/json", "application/problem+json"],
)
def test_proxy_rejects_html_and_json_bodies_as_expired(ctype):
    upstream = Upstream(200, {"content-type": ctype}, [b"<html>expired</html>"])

    async def send(_client, _method, _url, _headers, **_kwargs):
        return upstream

    response = _proxy_get(send)
    assert response.status_code == 410
    assert response.headers["x-curatio-error"] == "source-expired"
    assert "expired" in response.json()["detail"].lower()
    assert upstream.closed


def test_proxy_head_also_reports_an_expired_link():
    async def send(_client, _method, _url, _headers, **_kwargs):
        return Upstream(200, {"content-type": "text/html"})

    assert _proxy_get(send, method="HEAD").status_code == 410


def test_proxy_resumes_a_body_that_drops_mid_stream():
    calls = []

    async def send(_client, method, url, headers, **_kwargs):
        calls.append(dict(headers))
        if len(calls) == 1:
            return Upstream(
                200,
                {"content-type": "video/x-matroska", "content-length": "9"},
                [b"abc", b"def", b"never"],
                fail_after=2,
            )
        return Upstream(
            206,
            {
                "content-type": "video/x-matroska",
                "content-range": "bytes 6-8/9",
                "content-length": "3",
            },
            [b"ghi"],
        )

    response = _proxy_get(send)
    assert response.status_code == 200
    assert response.content == b"abcdefghi"
    assert "Range" not in calls[0]
    assert calls[1]["Range"] == "bytes=6-8"


def test_proxy_resume_keeps_the_requested_range_end():
    calls = []

    async def send(_client, _method, _url, headers, **_kwargs):
        calls.append(dict(headers))
        if len(calls) == 1:
            return Upstream(
                206,
                {
                    "content-type": "video/mp4",
                    "content-range": "bytes 100-105/1000",
                    "content-length": "6",
                },
                [b"012"],
                fail_after=1,
            )
        return Upstream(
            206,
            {"content-type": "video/mp4", "content-range": "bytes 103-105/1000"},
            [b"345"],
        )

    response = _proxy_get(send, headers={"Range": "bytes=100-105"})
    assert response.status_code == 206
    assert response.content == b"012345"
    assert calls[1]["Range"] == "bytes=103-105"


def test_proxy_caches_the_resolved_url_and_skips_the_redirect_chain():
    calls = []
    final = httpx.URL("https://cdn.example/real/file.mkv?sig=1")

    async def send(_client, _method, url, _headers, **_kwargs):
        calls.append(url)
        return Upstream(
            206,
            {"content-type": "video/x-matroska", "content-range": "bytes 0-2/3"},
            [b"abc"],
            url=final,
        )

    source = "https://resolver.example/play/abc"
    assert (
        _proxy_get(send, url=source, headers={"Range": "bytes=0-2"}).status_code == 206
    )
    assert (
        _proxy_get(send, url=source, headers={"Range": "bytes=0-2"}).status_code == 206
    )
    assert calls == [source, str(final)]
    assert main._cached_resolved_url(source) == str(final)


def test_stale_cached_url_falls_back_to_the_source_chain():
    final = "https://cdn.example/real/file.mkv?sig=old"
    renewed = httpx.URL("https://cdn.example/real/file.mkv?sig=new")
    source = "https://resolver.example/play/abc"
    main._remember_resolved_url(source, final)
    calls = []

    async def send(_client, _method, url, _headers, **_kwargs):
        calls.append(url)
        if url == final:
            return Upstream(403, {"content-type": "text/plain"})
        return Upstream(
            206,
            {"content-type": "video/mp4", "content-range": "bytes 0-2/3"},
            [b"abc"],
            url=renewed,
        )

    response = _proxy_get(send, url=source, headers={"Range": "bytes=0-2"})
    assert response.status_code == 206
    assert calls == [final, source]
    assert main._cached_resolved_url(source) == str(renewed)


def test_resolved_url_cache_expires():
    main._remember_resolved_url("https://a/src", "https://b/final")
    assert main._cached_resolved_url("https://a/src") == "https://b/final"
    with patch("app.main.monotonic", return_value=10**12):
        assert main._cached_resolved_url("https://a/src") is None
    assert main._RESOLVED_URL_TTL_SECONDS == 300.0


def test_segment_requests_validate_the_cookie_once(tmp_path):
    playlist = tmp_path / "index.m3u8"
    playlist.write_text("#EXTM3U\n")
    with (
        patch("app.main._get_active_profile", return_value=PROFILE) as lookup,
        patch.object(remux_manager, "file_path", return_value=playlist),
    ):
        client = TestClient(app)
        client.cookies.set("user_session", "cookie-token")
        for _ in range(3):
            assert (
                client.get("/account/api/playback/remux/any/index.m3u8").status_code
                == 200
            )
    assert lookup.call_count == 1
