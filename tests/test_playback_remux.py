# ruff: noqa: E402
"""Security, lifecycle, and command tests for browser compatibility HLS."""

import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from threading import Thread
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_playback_remux.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.database import get_db_dependency
from app.main import _UnsafeRedirect, _send_with_validated_redirects, app
from app.remux import (
    live_playlist,
    shift_segment,
    track_shifts,
    MediaProbe,
    SubtitleSidecar,
    fmp4_base_decode_time,
    fmp4_initial_empty_edit,
    fmp4_timescale,
    RemuxCapacityError,
    RemuxManager,
    RemuxPrepareError,
    RemuxSession,
    UnsupportedRemuxVideoError,
    create_proxy_token,
    remux_manager,
    verify_proxy_token,
)


class FakeStderr:
    async def readline(self):
        return b""


class FakeProcess:
    def __init__(self, *, stdout=b"", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.stderr = FakeStderr()
        self.pid = 4242

    async def communicate(self):
        return self._stdout, self._stderr

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class FakeUpstream:
    status_code = 206
    headers = {
        "content-type": "video/x-matroska",
        "content-range": "bytes 0-2/3",
        "content-length": "3",
        "accept-ranges": "bytes",
    }

    async def aiter_raw(self, _chunk_size):
        yield b"abc"

    async def aclose(self):
        return None


class QuietFileHandler(SimpleHTTPRequestHandler):
    """A static file server that answers byte-range requests like a real media
    host (Python's stock handler ignores Range, which breaks FFmpeg seeks)."""

    def log_message(self, _format, *_args):
        return None

    def send_head(self):
        header = self.headers.get("Range", "")
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
        path = self.translate_path(self.path)
        if not match or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        elif last:
            start, end = max(0, size - int(last)), size - 1
        else:
            return super().send_head()
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        with open(path, "rb") as handle:
            handle.seek(start)
            body = handle.read(end - start + 1)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        return io.BytesIO(body)

    def end_headers(self):
        if "Range" not in self.headers:
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()


def test_signed_proxy_token_is_expiring_and_bound_to_the_exact_url():
    source = "https://cdn.example/movie.mkv?token=abc"
    token = create_proxy_token(source, secret_key="secret", ttl_seconds=30, now=100)
    assert verify_proxy_token(source, token, secret_key="secret", now=129)
    assert not verify_proxy_token(source, token, secret_key="secret", now=131)
    assert not verify_proxy_token(
        "https://cdn.example/other.mkv?token=abc",
        token,
        secret_key="secret",
        now=110,
    )
    assert not verify_proxy_token(
        source, f"{token}tampered", secret_key="secret", now=110
    )


def test_signed_proxy_header_bypasses_cookies_only_for_its_bound_url():
    source = "https://cdn.example/movie.mkv"
    token = create_proxy_token(
        source,
        secret_key=os.environ["SECRET_KEY"],
        ttl_seconds=60,
    )

    async def safe(_url):
        return True

    async def send(*_args, **_kwargs):
        return FakeUpstream()

    with (
        patch("app.main._is_safe_source_url_async", new=safe),
        patch("app.main._send_with_validated_redirects", new=send),
    ):
        valid = TestClient(app).get(
            "/account/api/playback/proxy",
            params={"url": source},
            headers={"X-Curatio-Remux-Token": token, "Range": "bytes=0-2"},
        )
        rebound = TestClient(app).get(
            "/account/api/playback/proxy",
            params={"url": "https://cdn.example/other.mkv"},
            headers={"X-Curatio-Remux-Token": token},
        )
    assert valid.status_code == 206
    assert valid.content == b"abc"
    assert rebound.status_code == 401


def test_video_probe_accepts_only_browser_safe_8_bit_h264():
    RemuxManager._validate_video(MediaProbe(120, "h264", "high", "yuv420p", 1))
    with pytest.raises(UnsupportedRemuxVideoError):
        RemuxManager._validate_video(MediaProbe(120, "hevc", "main", "yuv420p", 1))
    with pytest.raises(UnsupportedRemuxVideoError):
        RemuxManager._validate_video(
            MediaProbe(120, "h264", "high 10", "yuv420p10le", 1)
        )
    with pytest.raises(UnsupportedRemuxVideoError):
        RemuxManager._validate_video(MediaProbe(120, "h264", "high", "yuv422p", 1))


@pytest.mark.asyncio
async def test_probe_selects_preferred_audio_language_then_first_track():
    payload = {
        "format": {"duration": "7200.5"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "pix_fmt": "yuv420p",
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "ac3",
                "tags": {"language": "eng"},
            },
            {
                "index": 3,
                "codec_type": "audio",
                "codec_name": "dts",
                "tags": {"language": "jpn"},
            },
        ],
    }
    manager = RemuxManager()
    with patch(
        "app.remux.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=FakeProcess(stdout=json.dumps(payload).encode())),
    ):
        japanese = await manager._probe_media(
            "http://proxy", "ja", proxy_token="signed"
        )
        first = await manager._probe_media(
            "http://proxy", "system", proxy_token="signed"
        )
    assert japanese.audio_stream_index == 3
    assert first.audio_stream_index == 1
    assert japanese.duration == pytest.approx(7200.5)


@pytest.mark.asyncio
async def test_ffmpeg_copies_h264_and_normalizes_selected_audio_to_stereo_aac(tmp_path):
    session = RemuxSession(
        "s1", 7, "https://cdn/movie.mkv", tmp_path, start_offset=95.25
    )
    spawn = AsyncMock(return_value=FakeProcess(returncode=None))
    manager = RemuxManager()
    with patch("app.remux.asyncio.create_subprocess_exec", new=spawn):
        await manager._spawn_ffmpeg(
            session,
            "http://127.0.0.1/proxy?url=source",
            proxy_token="signed-token",
            audio_stream_index=4,
        )
        await asyncio.sleep(0)
    command = list(spawn.await_args.args)
    assert command[0] == "ffmpeg"
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-ac") + 1] == "2"
    assert command[command.index("-map", command.index("-map") + 1) + 1] == "0:4"
    assert command[command.index("-hls_playlist_type") + 1] == "event"
    assert command[command.index("-ss") + 1] == "95.250"
    assert command[command.index("-headers") + 1] == (
        "X-Curatio-Remux-Token: signed-token\r\n"
    )
    assert "https://cdn/movie.mkv" not in command


@pytest.mark.asyncio
async def test_capacity_is_reserved_during_preparation_and_cleanup_is_idempotent():
    manager = RemuxManager(max_concurrent=1)
    probe = MediaProbe(100, "h264", "main", "yuv420p", 1)
    with (
        patch.object(manager, "_probe_media", new=AsyncMock(return_value=probe)),
        patch.object(manager, "_spawn_ffmpeg", new=AsyncMock()),
        patch.object(manager, "_wait_for_playlist", new=AsyncMock()),
    ):
        first = await manager.start(
            "https://cdn/one.mkv",
            "http://proxy/one",
            proxy_token="signed-one",
            profile_id=1,
        )
        with pytest.raises(RemuxCapacityError):
            await manager.start(
                "https://cdn/two.mkv",
                "http://proxy/two",
                proxy_token="signed-two",
                profile_id=1,
            )
    directory = first.directory
    assert await manager.stop(first.session_id, profile_id=1)
    assert not directory.exists()
    assert not await manager.stop(first.session_id, profile_id=1)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_real_ffmpeg_generates_event_hls_and_cleanup(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg runtime is not installed")
    source = tmp_path / "fixture.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=22050",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-profile:v",
            "main",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "ac3",
            "-y",
            str(source),
        ],
        check=True,
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietFileHandler, directory=str(tmp_path))
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manager = RemuxManager(playlist_wait_seconds=10)
    url = f"http://127.0.0.1:{server.server_port}/{source.name}"
    try:
        session = await manager.start(
            url,
            url,
            proxy_token="test-token",
            profile_id=9,
        )
        playlist = manager.file_path(session.session_id, "index.m3u8", profile_id=9)
        assert playlist is not None
        assert "#EXT-X-PLAYLIST-TYPE:EVENT" in playlist.read_text()
        assert (session.directory / "init.mp4").is_file()
        assert list(session.directory.glob("seg_*.m4s"))
        directory = session.directory
        assert await manager.stop(session.session_id, profile_id=9)
        assert not directory.exists()
    finally:
        await manager.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_owned_assets_reject_cross_profile_and_path_traversal_and_reap_idle(
    tmp_path,
):
    manager = RemuxManager(idle_timeout_seconds=30)
    directory = tmp_path / "session"
    directory.mkdir()
    playlist = directory / "index.m3u8"
    playlist.write_text("#EXTM3U\n")
    segment = directory / "seg_00001.m4s"
    segment.write_bytes(b"segment")
    init = directory / "init.mp4"
    init.write_bytes(b"init")
    subtitles = directory / "sub_3.vtt"
    subtitles.write_text("WEBVTT\n")
    session = RemuxSession("owned", 11, "https://cdn/movie.mkv", directory)
    manager._sessions[session.session_id] = session

    assert manager.file_path("owned", "index.m3u8", profile_id=11) == playlist
    assert manager.file_path("owned", "seg_00001.m4s", profile_id=11) == segment
    assert manager.file_path("owned", "init.mp4", profile_id=11) == init
    assert manager.file_path("owned", "sub_3.vtt", profile_id=11) == subtitles
    assert session.last_segment_requested == 1
    assert manager.file_path("owned", "index.m3u8", profile_id=12) is None
    assert manager.file_path("owned", "../index.m3u8", profile_id=11) is None
    assert manager.file_path("owned", "seg_1.m4s", profile_id=11) is None
    assert manager.file_path("owned", "seg_00001.ts", profile_id=11) is None

    session.last_access = time.monotonic() - 31
    assert await manager.reap_stale() == 1
    assert not directory.exists()


@pytest.mark.asyncio
async def test_redirect_hops_are_revalidated_before_fetching_private_targets():
    first = SimpleNamespace(
        is_redirect=True,
        headers={"location": "http://127.0.0.1/private"},
        url=SimpleNamespace(join=lambda location: location),
        aclose=AsyncMock(),
    )
    client = SimpleNamespace(
        build_request=lambda method, url, headers: (method, url, headers),
        send=AsyncMock(return_value=first),
    )

    async def safe(url):
        return not url.startswith("http://127.0.0.1")

    with patch("app.main._is_safe_source_url_async", new=safe):
        with pytest.raises(_UnsafeRedirect):
            await _send_with_validated_redirects(
                client, "GET", "https://public.example/movie", {}
            )
    assert client.send.await_count == 1
    first.aclose.assert_awaited_once()


def _client_with_profile(profile_id=42):
    def override_db():
        yield None

    app.dependency_overrides[get_db_dependency] = override_db
    return TestClient(app), SimpleNamespace(
        id=profile_id,
        is_active=True,
        display_name="Viewer",
        email="viewer@example.com",
        user_key="viewer-key",
    )


def test_hls_assets_are_private_no_store_and_profile_owned(tmp_path):
    playlist = tmp_path / "index.m3u8"
    playlist.write_text("#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n")
    client, profile = _client_with_profile()
    try:
        with (
            patch("app.main._get_active_profile", return_value=profile),
            patch.object(remux_manager, "file_path", return_value=playlist) as lookup,
        ):
            response = client.get("/account/api/playback/remux/owned/index.m3u8")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "application/vnd.apple.mpegurl"
        )
        assert response.headers["cache-control"] == "private, no-store"
        lookup.assert_called_once_with("owned", "index.m3u8", profile_id=profile.id)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RemuxCapacityError("full"), 429),
        (UnsupportedRemuxVideoError("unsupported"), 422),
        (RemuxPrepareError("source probe failed"), 502),
        (RemuxPrepareError("source probe timed out"), 504),
    ],
)
def test_start_endpoint_maps_remux_failures(error, expected):
    client, profile = _client_with_profile()

    async def safe(_url):
        return True

    try:
        with (
            patch("app.main._get_active_profile", return_value=profile),
            patch("app.main._is_safe_source_url_async", new=safe),
            patch.object(remux_manager, "start", new=AsyncMock(side_effect=error)),
        ):
            response = client.post(
                "/account/api/playback/remux/start",
                json={"url": "https://cdn.example/movie.mkv", "start": 90},
            )
        assert response.status_code == expected
    finally:
        app.dependency_overrides.clear()


def test_start_and_stop_endpoints_return_contract_and_enforce_profile_ownership(
    tmp_path,
):
    client, profile = _client_with_profile()
    session = RemuxSession(
        "contract-session",
        profile.id,
        "https://cdn.example/movie.mkv",
        tmp_path,
        start_offset=90,
        duration=7200,
    )
    session.audio_index = 1

    async def safe(_url):
        return True

    start = AsyncMock(return_value=session)
    stop = AsyncMock(return_value=True)
    try:
        with (
            patch("app.main._get_active_profile", return_value=profile),
            patch("app.main._is_safe_source_url_async", new=safe),
            patch.object(remux_manager, "start", new=start),
            patch.object(remux_manager, "stop", new=stop),
        ):
            response = client.post(
                "/account/api/playback/remux/start",
                json={
                    "url": session.source_url,
                    "title": "Movie",
                    "start": 90,
                    "audio_language": "ja",
                },
            )
            stopped = client.post(
                "/account/api/playback/remux/stop",
                json={"session": session.session_id},
            )
        assert response.status_code == 200
        assert response.json() == {
            "playlist_url": "/account/api/playback/remux/contract-session/index.m3u8",
            "session": "contract-session",
            "duration": 7200,
            "start": 90,
            "requested_start": 90,
            "mode": "remux",
            "video_mode": "copy",
            "audio_index": 1,
            "keepalive_url": "/account/api/playback/remux/contract-session/keepalive",
            "subtitles": [],
        }
        assert stopped.json() == {"ok": True}
        call = start.await_args
        assert call.kwargs["profile_id"] == profile.id
        assert call.kwargs["audio_language"] == "ja"
        assert call.args[1].startswith("http://127.0.0.1:")
        assert "remux_token=" not in call.args[1]
        assert verify_proxy_token(
            session.source_url,
            call.kwargs["proxy_token"],
            secret_key=os.environ["SECRET_KEY"],
        )
        stop.assert_awaited_once_with(session.session_id, profile_id=profile.id)
    finally:
        app.dependency_overrides.clear()


def _box(kind: str, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind.encode("latin-1") + payload


def _init_segment(timescale: int) -> bytes:
    mdhd = (
        b"\x00\x00\x00\x00" + b"\x00" * 8 + timescale.to_bytes(4, "big") + b"\x00" * 8
    )
    return _box("ftyp", b"iso6\x00\x00\x00\x00") + _box(
        "moov",
        _box("mvhd", b"\x00" * 20) + _box("trak", _box("mdia", _box("mdhd", mdhd))),
    )


def _media_segment(decode_time: int) -> bytes:
    tfdt = b"\x01\x00\x00\x00" + decode_time.to_bytes(8, "big")
    return _box(
        "moof",
        _box("mfhd", b"\x00" * 8)
        + _box("traf", _box("tfhd", b"\x00" * 8) + _box("tfdt", tfdt)),
    ) + _box("mdat", b"\x00" * 16)


def test_fmp4_boxes_report_timescale_and_first_decode_time():
    assert fmp4_timescale(_init_segment(90000)) == 90000
    assert fmp4_base_decode_time(_media_segment(90000 * 7)) == 90000 * 7
    assert fmp4_timescale(b"not an mp4") is None
    assert fmp4_base_decode_time(b"\x00\x00\x00\x10moof") is None


def _init_with_empty_edit(movie_scale: int, empty: int) -> bytes:
    mvhd = b"\x00" * 12 + movie_scale.to_bytes(4, "big") + b"\x00" * 4
    elst = (
        b"\x00\x00\x00\x00"
        + (2).to_bytes(4, "big")
        + empty.to_bytes(4, "big")
        + (-1).to_bytes(4, "big", signed=True)
        + b"\x00\x01\x00\x00"
        + (0).to_bytes(4, "big")
        + (0).to_bytes(4, "big", signed=True)
        + b"\x00\x01\x00\x00"
    )
    mdhd = b"\x00\x00\x00\x00" + b"\x00" * 8 + (16000).to_bytes(4, "big") + b"\x00" * 8
    return _box("ftyp", b"iso6\x00\x00\x00\x00") + _box(
        "moov",
        _box("mvhd", mvhd)
        + _box(
            "trak", _box("edts", _box("elst", elst)) + _box("mdia", _box("mdhd", mdhd))
        ),
    )


def test_media_start_falls_back_to_the_empty_edit_when_fragments_are_rebased(tmp_path):
    assert fmp4_initial_empty_edit(_init_with_empty_edit(1000, 4008)) == 4.008
    assert fmp4_initial_empty_edit(_init_segment(1000)) is None

    session = RemuxSession("e", 1, "https://cdn/movie.mkv", tmp_path, start_offset=5.0)
    (tmp_path / "init.mp4").write_bytes(_init_with_empty_edit(1000, 4008))
    (tmp_path / "seg_00000.m4s").write_bytes(_media_segment(0))
    assert RemuxManager.measure_media_start(session) == 4.008

    # No edit list and a zero decode time: keep the requested start.
    (tmp_path / "init.mp4").write_bytes(_init_segment(1000))
    assert RemuxManager.measure_media_start(session) == 5.0


def test_media_start_uses_the_keyframe_before_the_requested_time(tmp_path):
    session = RemuxSession(
        "k", 1, "https://cdn/movie.mkv", tmp_path, start_offset=95.25
    )
    (tmp_path / "init.mp4").write_bytes(_init_segment(1000))
    (tmp_path / "seg_00000.m4s").write_bytes(_media_segment(92_000))
    assert RemuxManager.measure_media_start(session) == 92.0

    # A decode time that can't be the keyframe before the target is ignored.
    (tmp_path / "seg_00000.m4s").write_bytes(_media_segment(12_000))
    assert RemuxManager.measure_media_start(session) == 95.25

    fresh = RemuxSession("z", 1, "https://cdn/movie.mkv", tmp_path)
    assert RemuxManager.measure_media_start(fresh) == 0.0


def test_video_plan_copies_what_the_browser_decodes_and_transcodes_only_when_allowed():
    h264 = MediaProbe(100, "h264", "high", "yuv420p", 1)
    hevc10 = MediaProbe(100, "hevc", "main 10", "yuv420p10le", 1)
    dv5 = MediaProbe(100, "hevc", "main 10", "yuv420p10le", 1, dv_profile=5)
    mpeg2 = MediaProbe(100, "mpeg2video", "main", "yuv420p", 1)

    assert RemuxManager._validate_video(h264) == "copy"
    assert RemuxManager._validate_video(hevc10, allow_hevc=True) == "copy"
    with pytest.raises(UnsupportedRemuxVideoError):
        RemuxManager._validate_video(hevc10)
    assert RemuxManager._validate_video(hevc10, allow_transcode=True) == "transcode"
    with pytest.raises(UnsupportedRemuxVideoError):
        RemuxManager._validate_video(dv5, allow_hevc=True)
    assert (
        RemuxManager._validate_video(dv5, allow_hevc=True, allow_transcode=True)
        == "transcode"
    )
    assert RemuxManager._validate_video(mpeg2, allow_transcode=True) == "transcode"


@pytest.mark.asyncio
async def test_ffmpeg_writes_fmp4_hls_tags_hevc_and_keeps_source_time_after_seek(
    tmp_path,
):
    session = RemuxSession("s2", 7, "https://cdn/show.mkv", tmp_path, start_offset=600)
    spawn = AsyncMock(return_value=FakeProcess(returncode=None))
    manager = RemuxManager()
    subtitles = (SubtitleSidecar(5, "eng", "English", False, True),)
    with patch("app.remux.asyncio.create_subprocess_exec", new=spawn):
        await manager._spawn_ffmpeg(
            session,
            "http://127.0.0.1/proxy?url=source",
            proxy_token="signed-token",
            audio_stream_index=1,
            audio_channels=6,
            subtitles=subtitles,
            video_codec="hevc",
        )
        await asyncio.sleep(0)
    command = list(spawn.await_args.args)
    assert command[command.index("-hls_segment_type") + 1] == "fmp4"
    assert command[command.index("-hls_fmp4_init_filename") + 1] == "init.mp4"
    assert command[command.index("-tag:v") + 1] == "hvc1"
    assert command[command.index("-ac") + 1] == "6"
    assert "-copyts" in command and "-start_at_zero" in command
    assert command.index("-copyts") < command.index("-i")
    # Audio starts at the same keyframe as the copied video (not the exact
    # requested time): browsers ignore the edit lists that would line them up.
    assert "-noaccurate_seek" in command
    assert command.index("-noaccurate_seek") < command.index("-i")
    assert command[command.index("-c:s") + 1] == "webvtt"
    assert command[-1] == str(tmp_path / "sub_5.vtt")
    assert command[command.index("-hls_segment_filename") + 1].endswith("seg_%05d.m4s")


@pytest.mark.asyncio
async def test_ffmpeg_transcodes_to_browser_safe_h264_only_in_transcode_mode(tmp_path):
    session = RemuxSession("s3", 7, "https://cdn/old.avi", tmp_path)
    spawn = AsyncMock(return_value=FakeProcess(returncode=None))
    manager = RemuxManager()
    with patch("app.remux.asyncio.create_subprocess_exec", new=spawn):
        await manager._spawn_ffmpeg(
            session,
            "http://127.0.0.1/proxy?url=source",
            proxy_token="signed-token",
            audio_stream_index=None,
            video_mode="transcode",
            target_height=2160,
        )
        await asyncio.sleep(0)
    command = list(spawn.await_args.args)
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert "min(1080,ih)" in command[command.index("-vf") + 1]
    assert "-an" in command
    assert "-copyts" not in command and "-ss" not in command


@pytest.mark.asyncio
async def test_keepalive_holds_a_paused_session_and_old_segments_are_pruned(tmp_path):
    manager = RemuxManager(idle_timeout_seconds=30, back_window_seconds=60)
    session = RemuxSession("lease", 4, "https://cdn/movie.mkv", tmp_path)
    manager._sessions[session.session_id] = session
    for number in range(40):
        (tmp_path / f"seg_{number:05d}.m4s").write_bytes(b"x" * 10)

    assert manager.file_path("lease", "seg_00035.m4s", profile_id=4) is not None
    session.last_access = time.monotonic() - 25
    assert manager.keepalive("lease", profile_id=4)
    assert not manager.keepalive("lease", profile_id=5)
    assert await manager.reap_stale() == 0

    # 60 s back window = 15 segments behind segment 35 survive.
    assert not (tmp_path / "seg_00019.m4s").exists()
    assert (tmp_path / "seg_00020.m4s").exists()
    assert manager.file_path("lease", "seg_00010.m4s", profile_id=4) is None
    assert manager.file_path("lease", "seg_00030.m4s", profile_id=4) is not None
    await manager.shutdown()


def test_keepalive_endpoint_renews_owned_sessions_only():
    client, profile = _client_with_profile()
    try:
        with (
            patch("app.main._get_active_profile", return_value=profile),
            patch.object(
                remux_manager, "keepalive", side_effect=[True, False]
            ) as renew,
        ):
            ok = client.post("/account/api/playback/remux/abc/keepalive")
            gone = client.post("/account/api/playback/remux/abc/keepalive")
        assert ok.status_code == 200
        assert gone.status_code == 404
        renew.assert_called_with("abc", profile_id=profile.id)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_real_ffmpeg_seek_reports_the_keyframe_it_started_from(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg runtime is not installed")
    source = tmp_path / "gop.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=22050",
            "-t",
            "8",
            "-c:v",
            "libx264",
            "-g",
            "24",
            "-keyint_min",
            "24",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "ac3",
            "-y",
            str(source),
        ],
        check=True,
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietFileHandler, directory=str(tmp_path))
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manager = RemuxManager(playlist_wait_seconds=10)
    url = f"http://127.0.0.1:{server.server_port}/{source.name}"
    try:
        session = await manager.start(
            url, url, proxy_token="t", profile_id=3, start_offset=5.0
        )
        # Keyframes every 2 s: copying from 5 s starts at the 4 s keyframe.
        assert session.media_start == pytest.approx(4.0, abs=0.15)
        await manager.stop(session.session_id, profile_id=3)
    finally:
        await manager.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---- A/V alignment for browsers (edit lists applied to tfdt) -------------------


def _full(kind: str, version: int, body: bytes) -> bytes:
    return _box(kind, bytes([version, 0, 0, 0]) + body)


def _sync_init(tracks) -> bytes:
    """moov with an mvhd (1000/s) and one trak per (id, timescale, [(duration, media_time)])."""
    mvhd = _full("mvhd", 0, bytes(8) + (1000).to_bytes(4, "big") + bytes(84))
    traks = b""
    for track_id, timescale, edits in tracks:
        tkhd = _full("tkhd", 0, bytes(8) + track_id.to_bytes(4, "big") + bytes(68))
        elst_body = len(edits).to_bytes(4, "big") + b"".join(
            duration.to_bytes(4, "big")
            + media_time.to_bytes(4, "big", signed=True)
            + (0x10000).to_bytes(4, "big")
            for duration, media_time in edits
        )
        mdhd = _full("mdhd", 0, bytes(8) + timescale.to_bytes(4, "big") + bytes(8))
        traks += _box(
            "trak",
            tkhd + _box("edts", _full("elst", 0, elst_body)) + _box("mdia", mdhd),
        )
    return _box("moov", mvhd + traks)


def _sync_segment(*tracks) -> bytes:
    trafs = b"".join(
        _box(
            "traf",
            _full("tfhd", 0, track_id.to_bytes(4, "big"))
            + _full("tfdt", 1, decode.to_bytes(8, "big")),
        )
        for track_id, decode in tracks
    )
    return _box("moof", trafs)


def _decode_times(segment: bytes) -> dict:
    out, at = {}, segment.find(b"tfhd")
    while at >= 0:
        track_id = int.from_bytes(segment[at + 8 : at + 12], "big")
        tfdt = segment.find(b"tfdt", at)
        out[track_id] = int.from_bytes(segment[tfdt + 8 : tfdt + 16], "big")
        at = segment.find(b"tfhd", at + 4)
    return out


def test_track_shifts_line_up_b_frame_video_and_aac_priming():
    # Video 16 kHz with a 2-frame reorder delay (83 ms); AAC priming 1024 samples.
    init = _sync_init([(1, 16000, [(0, 1328)]), (2, 48000, [(0, 1024)])])
    shifts = track_shifts(init)
    assert shifts[1] == 0
    assert shifts[2] == round((1328 / 16000 - 1024 / 48000) * 48000)
    segment = _sync_segment((1, 160000), (2, 480000))
    assert _decode_times(shift_segment(segment, shifts)) == {
        1: 160000,
        2: 480000 + shifts[2],
    }


def test_track_shifts_include_leading_empty_edits_after_a_seek():
    init = _sync_init(
        [(1, 16000, [(10000, -1), (0, 1328)]), (2, 48000, [(10000, -1), (0, 0)])]
    )
    assert track_shifts(init) == {1: 0, 2: round(1328 / 16000 * 48000)}


def test_aligned_files_and_garbage_pass_through():
    assert (
        track_shifts(_sync_init([(1, 16000, [(0, 0)]), (2, 48000, [(0, 0)])])) is None
    )
    assert track_shifts(b"not mp4") is None
    segment = _sync_segment((1, 5))
    assert shift_segment(segment, None) is segment


def test_segments_are_served_aligned(tmp_path):
    (tmp_path / "init.mp4").write_bytes(
        _sync_init([(1, 16000, [(0, 1328)]), (2, 48000, [(0, 0)])])
    )
    segment = tmp_path / "seg_00000.m4s"
    segment.write_bytes(_sync_segment((1, 0), (2, 0)))
    client, profile = _client_with_profile()
    session = RemuxSession("owned", profile.id, "https://cdn/x.mkv", tmp_path)
    remux_manager._sessions["owned"] = session
    try:
        with (
            patch("app.main._get_active_profile", return_value=profile),
            patch.object(remux_manager, "file_path", return_value=segment),
        ):
            response = client.get("/account/api/playback/remux/owned/seg_00000.m4s")
        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["cache-control"] == "private, no-store"
        assert _decode_times(response.content) == {1: 0, 2: round(1328 / 16000 * 48000)}
    finally:
        remux_manager._sessions.pop("owned", None)
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_real_ffmpeg_resume_starts_audio_and_video_together(tmp_path):
    """The resume bug: video began at the keyframe, audio at the requested time,
    and browsers (ignoring edit lists) played them seconds apart."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg runtime is not installed")
    source = tmp_path / "gop.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "12",
            *_h264_with_b_frames(),
            "-g",
            "48",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "ac3",
            "-y",
            str(source),
        ],
        check=True,
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietFileHandler, directory=str(tmp_path))
    )
    Thread(target=server.serve_forever, daemon=True).start()
    manager = RemuxManager(playlist_wait_seconds=10)
    url = f"http://127.0.0.1:{server.server_port}/{source.name}"
    try:
        # Keyframes every 4 s: resuming at 7 s copies video from the 4 s keyframe.
        session = await manager.start(
            url, url, proxy_token="t", profile_id=3, start_offset=7.0
        )
        await asyncio.sleep(1.0)
        init = (session.directory / "init.mp4").read_bytes()
        # Where each track really starts = its leading empty edit; they must agree
        # to within a frame (before the fix audio started 3 s after video).
        starts = {}
        for track_id, timescale, edits in _edit_lists(init):
            empty = sum(d for d, m in edits if m == -1) / 1000
            media = next((m for d, m in edits if m != -1), 0) / timescale
            starts[track_id] = empty - media
        assert len(starts) == 2
        assert abs(starts[1] - starts[2]) < 0.2, starts
        await manager.stop(session.session_id, profile_id=3)
    finally:
        await manager.shutdown()
        server.shutdown()


def _h264_with_b_frames() -> list:
    """libx264 where available (CI); the platform encoder on an LGPL-only build."""
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    if "libx264" in encoders:
        return [
            "-c:v",
            "libx264",
            "-bf",
            "2",
            "-keyint_min",
            "48",
            "-sc_threshold",
            "0",
        ]
    if "h264_videotoolbox" in encoders:
        return ["-c:v", "h264_videotoolbox", "-bf", "2", "-profile:v", "high"]
    pytest.skip("no H.264 encoder with B-frames in this FFmpeg")


def _edit_lists(init: bytes):
    """[(track_id, timescale, [(duration, media_time)])] from a real init.mp4."""
    from app.remux import _find_box, _read_box_headers

    moov = _find_box(init, ["moov"])
    mvhd = _find_box(init, ["moov", "mvhd"])
    movie_scale = int.from_bytes(init[mvhd[0] + 12 : mvhd[0] + 16], "big")
    out = []
    for kind, payload, end in _read_box_headers(init, moov[0], moov[1]):
        if kind != "trak":
            continue
        tkhd = _find_box(init, ["tkhd"], payload, end)
        mdhd = _find_box(init, ["mdia", "mdhd"], payload, end)
        elst = _find_box(init, ["edts", "elst"], payload, end)
        track_id = int.from_bytes(init[tkhd[0] + 12 : tkhd[0] + 16], "big")
        timescale = int.from_bytes(init[mdhd[0] + 12 : mdhd[0] + 16], "big")
        edits = []
        if elst:
            count = int.from_bytes(init[elst[0] + 4 : elst[0] + 8], "big")
            at = elst[0] + 8
            for _ in range(count):
                duration = (
                    int.from_bytes(init[at : at + 4], "big") * 1000 // movie_scale
                )
                media_time = int.from_bytes(init[at + 4 : at + 8], "big", signed=True)
                edits.append((duration, media_time))
                at += 12
        out.append((track_id, timescale, edits))
    return out


# ---- hls.js long-poll playlist (?hold=1) ---------------------------------------

_GROWING = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:10.0,\nseg_00000.m4s\n"


def test_live_playlist_advertises_a_short_target_until_it_ends():
    assert "#EXT-X-TARGETDURATION:1\n" in live_playlist(_GROWING)
    assert "seg_00000.m4s" in live_playlist(_GROWING)
    ended = _GROWING + "#EXT-X-ENDLIST\n"
    assert live_playlist(ended) == ended


@pytest.mark.asyncio
async def test_playlist_request_is_held_until_the_next_segment(tmp_path):
    playlist = tmp_path / "index.m3u8"
    playlist.write_text(_GROWING)
    manager = RemuxManager()
    session = RemuxSession("s", 7, "https://cdn/x.mkv", tmp_path)
    session.process = FakeProcess(returncode=None)
    manager._sessions["s"] = session

    first = await manager.playlist_text("s", playlist)
    assert first.count("#EXTINF") == 1  # answered at once

    async def add_segment():
        await asyncio.sleep(0.25)
        playlist.write_text(_GROWING + "#EXTINF:10.0,\nseg_00001.m4s\n")

    began = time.monotonic()
    writer = asyncio.create_task(add_segment())
    second = await manager.playlist_text("s", playlist)
    await writer
    assert 0.2 <= time.monotonic() - began < 2.0
    assert "seg_00001.m4s" in second

    # FFmpeg finished: nothing more will come, so don't hold.
    session.process = FakeProcess(returncode=0)
    began = time.monotonic()
    await manager.playlist_text("s", playlist)
    assert time.monotonic() - began < 0.5


def test_playlist_is_long_polled_only_when_asked(tmp_path):
    playlist = tmp_path / "index.m3u8"
    playlist.write_text(_GROWING)
    client, profile = _client_with_profile()
    try:
        with (
            patch("app.main._get_active_profile", return_value=profile),
            patch.object(remux_manager, "file_path", return_value=playlist),
        ):
            plain = client.get("/account/api/playback/remux/owned/index.m3u8")
            held = client.get("/account/api/playback/remux/owned/index.m3u8?hold=1")
        # Native HLS (Safari) gets the playlist exactly as FFmpeg wrote it.
        assert "#EXT-X-TARGETDURATION:10" in plain.text
        assert "#EXT-X-TARGETDURATION:1\n" in held.text
        assert held.headers["content-type"].startswith("application/vnd.apple.mpegurl")
        assert held.headers["cache-control"] == "private, no-store"
    finally:
        app.dependency_overrides.clear()


# ---- explicit audio track choice (the player's Audio menu) ----------------------


def _facts_with_three_audio_tracks():
    from app.media_probe import parse_ffprobe

    return parse_ffprobe(
        {
            "format": {"format_name": "matroska,webm", "duration": "100"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "profile": "High",
                    "pix_fmt": "yuv420p",
                    "height": 720,
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "truehd",
                    "channels": 6,
                    "tags": {"language": "eng"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 2,
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "channels": 2,
                    "tags": {"language": "eng", "title": "Commentary"},
                },
                {
                    "index": 3,
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "channels": 6,
                    "tags": {"language": "fra"},
                },
            ],
        }
    )


def test_audio_index_picks_that_track_over_the_language_preference():
    facts = _facts_with_three_audio_tracks()
    pick = RemuxManager._probe_from_facts
    assert pick(facts, "system").audio_stream_index == 1  # the default track
    assert pick(facts, "fr").audio_stream_index == 3  # by language
    # Two English tracks: only an index can reach the commentary.
    assert pick(facts, "en", 2).audio_stream_index == 2
    assert pick(facts, "fr", 2).audio_stream_index == 2
    # An index the file does not have falls back to the usual choice.
    assert pick(facts, "fr", 9).audio_stream_index == 3
