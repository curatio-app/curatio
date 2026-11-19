"""The interface every playback source provider implements.

Built-in providers (Plex, Jellyfin) and installed addons subclass
``SourceProvider``. Only ``resolve`` is required; every other hook has a safe
default so a minimal addon is a few dozen lines.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from app.sources.models import (
    FailureReport,
    MediaRef,
    PlaybackContext,
    PlaybackSource,
    SourceKind,
    TitleRef,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter


class SourceProvider:
    """Base class for playback source providers.

    Class attributes
    ----------------
    id:      short lowercase identifier, unique across providers (``plex``).
    label:   human name shown in admin and source pickers (``Plex``).
    kind:    ``library`` for media servers the owner runs, ``addon`` otherwise.
    sdk_version: the ``app.sources.sdk.SDK_VERSION`` the provider was built for.
    searchable: an addon that can look up any title on demand sets this True, which
             makes every title ``playable`` while the addon is enabled.
    """

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    kind: ClassVar[SourceKind] = "addon"
    sdk_version: ClassVar[int] = 1
    searchable: ClassVar[bool] = False

    # --- lifecycle -------------------------------------------------------
    def enabled(self) -> bool:
        """True when configured and switched on. Must be cheap (cached)."""
        return False

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def clear_caches(self) -> None:
        return None

    # --- playback --------------------------------------------------------
    async def resolve(
        self, media: MediaRef, ctx: PlaybackContext
    ) -> list[PlaybackSource]:
        raise NotImplementedError

    def availability(self, refs: list[TitleRef]) -> dict[str, bool]:
        """Map ``TitleRef.key()`` → owned. Synchronous and DB-only (no network).

        Library providers answer from their synced index. Addons that cannot
        know ahead of time return ``{}`` and set ``searchable`` instead.
        """
        return {}

    async def on_failure(self, report: FailureReport) -> None:
        return None

    # --- admin -----------------------------------------------------------
    def health(self) -> str:
        """``ok`` | ``degraded`` | ``offline``."""
        return "ok" if self.enabled() else "offline"

    def admin_summary(self) -> dict[str, Any]:
        """Small JSON blob for the Integrations page card."""
        return {}

    def admin_router(self) -> Optional["APIRouter"]:
        """Routes mounted at ``/admin/api/integrations/{id}`` behind admin auth."""
        return None

    def admin_static_dir(self) -> Optional[Path]:
        """Prebuilt settings UI served at ``/admin/addons/{id}/`` (addons only)."""
        return None

    def live_activity(self) -> Optional[dict[str, Any]]:
        """Optional upstream transfer snapshot for Admin → Live Activity."""
        return None

    def background_jobs(self) -> list[tuple[str, int, Any]]:
        """``(name, interval_seconds, callable)`` jobs for the scheduler."""
        return []
