"""Discovers playback source providers: built-ins plus installed addons.

Addons are ordinary Python packages that declare an entry point::

    [project.entry-points."curatio.sources"]
    myaddon = "my_addon_package:provider"

where ``provider`` is a ``SourceProvider`` instance (or a zero-argument callable
returning one). Addons built for a different ``SDK_VERSION`` are skipped.
"""

from __future__ import annotations

import threading
from importlib.metadata import entry_points
from typing import Optional

from loguru import logger

from app.sources.base import SourceProvider
from app.sources.models import ProviderStatus

ENTRY_POINT_GROUP = "curatio.sources"

_lock = threading.Lock()
_providers: Optional[list[SourceProvider]] = None
_rejected: list[dict] = []


def _builtins() -> list[SourceProvider]:
    from app.sources.jellyfin import provider as jellyfin
    from app.sources.plex import provider as plex

    return [plex, jellyfin]


def _load_addons(taken: set[str]) -> list[SourceProvider]:
    from app.sources.sdk import SDK_VERSION

    found: list[SourceProvider] = []
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # pragma: no cover - broken environment
        logger.warning("Addon discovery failed: {}", exc)
        return found
    for ep in eps:
        try:
            obj = ep.load()
            provider = (
                obj() if callable(obj) and not isinstance(obj, SourceProvider) else obj
            )
        except Exception as exc:
            _rejected.append({"entry_point": ep.name, "reason": f"load failed: {exc}"})
            logger.warning("Addon {} failed to load: {}", ep.name, exc)
            continue
        if not isinstance(provider, SourceProvider):
            _rejected.append({"entry_point": ep.name, "reason": "not a SourceProvider"})
            continue
        if provider.sdk_version != SDK_VERSION:
            _rejected.append(
                {
                    "entry_point": ep.name,
                    "reason": f"built for SDK {provider.sdk_version}, server has {SDK_VERSION}",
                }
            )
            logger.warning(
                "Addon {} skipped: SDK {} != {}",
                ep.name,
                provider.sdk_version,
                SDK_VERSION,
            )
            continue
        if not provider.id or provider.id in taken:
            _rejected.append(
                {"entry_point": ep.name, "reason": "missing or duplicate id"}
            )
            continue
        if provider.kind != "addon":
            # Only the server's own integrations may claim to be a library.
            _rejected.append(
                {"entry_point": ep.name, "reason": "addons must use kind 'addon'"}
            )
            continue
        taken.add(provider.id)
        found.append(provider)
        logger.info("Loaded playback addon {} ({})", provider.id, provider.label)
    return found


def all_providers() -> list[SourceProvider]:
    global _providers
    if _providers is None:
        with _lock:
            if _providers is None:
                _rejected.clear()
                builtins = _builtins()
                taken = {p.id for p in builtins}
                _providers = builtins + _load_addons(taken)
    return list(_providers)


def set_providers_for_tests(providers: Optional[list[SourceProvider]]) -> None:
    """Replace the provider list (``None`` re-runs discovery)."""
    global _providers
    with _lock:
        _providers = None if providers is None else list(providers)


def rejected_addons() -> list[dict]:
    all_providers()
    return list(_rejected)


def get(provider_id: str) -> Optional[SourceProvider]:
    for provider in all_providers():
        if provider.id == provider_id:
            return provider
    return None


def enabled_providers() -> list[SourceProvider]:
    out = []
    for provider in all_providers():
        try:
            if provider.enabled():
                out.append(provider)
        except Exception as exc:
            logger.warning("Provider {} enabled() failed: {}", provider.id, exc)
    return out


def provider_statuses() -> list[ProviderStatus]:
    statuses = []
    for provider in enabled_providers():
        try:
            health = provider.health()
        except Exception:
            health = "degraded"
        if health not in ("ok", "degraded", "offline"):
            health = "degraded"
        statuses.append(
            ProviderStatus(
                id=provider.id, label=provider.label, kind=provider.kind, status=health
            )
        )
    return statuses
