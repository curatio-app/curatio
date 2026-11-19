"""Connects providers to the FastAPI app: admin routes, addon UIs, background jobs."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.sources import registry, resolver
from app.sources.sdk import close_http_clients, require_admin

_job_tasks: list[asyncio.Task] = []


def integrations_router() -> APIRouter:
    router = APIRouter(
        prefix="/admin/api/integrations",
        tags=["admin"],
        dependencies=[Depends(require_admin)],
    )

    @router.get("")
    def list_integrations():
        from app.sources.sdk import SDK_VERSION

        items = []
        for provider in registry.all_providers():
            try:
                summary = provider.admin_summary() or {}
            except Exception as exc:
                summary = {"error": type(exc).__name__}
            items.append(
                {
                    "id": provider.id,
                    "label": provider.label,
                    "kind": provider.kind,
                    "enabled": _safe_enabled(provider),
                    "health": _safe_health(provider),
                    "searchable": bool(provider.searchable),
                    "settings_url": (
                        f"/admin/addons/{provider.id}/"
                        if provider.admin_static_dir() is not None
                        else None
                    ),
                    "summary": summary,
                }
            )
        return {
            "sdk_version": SDK_VERSION,
            "integrations": items,
            "rejected_addons": registry.rejected_addons(),
            "source_cache": resolver.cache_stats(),
        }

    @router.post("/cache/clear")
    def clear_cache():
        resolver.clear_cache()
        return {"status": "ok"}

    return router


def _safe_enabled(provider) -> bool:
    try:
        return bool(provider.enabled())
    except Exception:
        return False


def _safe_health(provider) -> str:
    try:
        return provider.health()
    except Exception:
        return "degraded"


def install(app: FastAPI) -> None:
    """Mount integration admin APIs and addon settings UIs. Call once at import."""
    app.include_router(integrations_router())
    for provider in registry.all_providers():
        try:
            router: Optional[APIRouter] = provider.admin_router()
        except Exception as exc:
            logger.warning("Provider {} admin_router failed: {}", provider.id, exc)
            router = None
        if router is not None:
            app.include_router(
                router,
                prefix=f"/admin/api/integrations/{provider.id}",
                tags=["admin"],
                dependencies=[Depends(require_admin)],
            )
        static_dir = provider.admin_static_dir()
        if static_dir is not None and static_dir.is_dir():
            # Addon settings UIs call their own API under
            # /admin/api/integrations/{id}, which is admin-gated above.
            app.mount(
                f"/admin/addons/{provider.id}",
                StaticFiles(directory=str(static_dir), html=True),
                name=f"addon-{provider.id}",
            )


async def _job_loop(provider_id: str, name: str, interval: int, fn) -> None:
    await asyncio.sleep(30)  # let startup settle
    while True:
        provider = registry.get(provider_id)
        if provider is not None and _safe_enabled(provider):
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Job {} failed: {}", name, exc)
        await asyncio.sleep(max(60, int(interval)))


async def startup() -> None:
    for provider in registry.all_providers():
        try:
            await provider.startup()
        except Exception as exc:
            logger.warning("Provider {} startup failed: {}", provider.id, exc)
        try:
            jobs = provider.background_jobs()
        except Exception:
            jobs = []
        for name, interval, fn in jobs:
            _job_tasks.append(
                asyncio.create_task(_job_loop(provider.id, name, interval, fn))
            )


async def shutdown() -> None:
    for task in _job_tasks:
        task.cancel()
    _job_tasks.clear()
    for provider in registry.all_providers():
        try:
            await provider.shutdown()
        except Exception as exc:
            logger.warning("Provider {} shutdown failed: {}", provider.id, exc)
    from app import media_proxy

    await media_proxy.close_client()
    await close_http_clients()
