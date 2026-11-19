"""HTTP middleware shared by the API and browser applications."""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import Message, Receive, Scope, Send


_NO_COMPRESSION_PATHS = ("/account/api/playback/proxy",)
_NO_COMPRESSION_TYPES = (
    "audio/",
    "video/",
    "application/octet-stream",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "text/event-stream",
)


class _SelectiveGZipResponder(GZipResponder):
    """GZip responder that leaves ranged and streamed media byte-identical."""

    skip_compression = False

    async def send_with_gzip(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = Headers(raw=message["headers"])
            content_type = headers.get("content-type", "").lower()
            self.skip_compression = (
                int(message.get("status", 200)) == 206
                or "content-range" in headers
                or "content-length" not in headers
                or any(
                    content_type.startswith(prefix) for prefix in _NO_COMPRESSION_TYPES
                )
            )
            if self.skip_compression:
                self.started = True
                await self.send(message)
                return
        elif self.skip_compression:
            await self.send(message)
            return
        await super().send_with_gzip(message)


class SelectiveGZipMiddleware(GZipMiddleware):
    """Compress regular documents while never transforming playback streams."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path.startswith(_NO_COMPRESSION_PATHS):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if "gzip" not in headers.get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return
        responder = _SelectiveGZipResponder(
            self.app,
            self.minimum_size,
            compresslevel=self.compresslevel,
        )
        await responder(scope, receive, send)
