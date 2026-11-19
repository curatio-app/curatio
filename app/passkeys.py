"""Passkey/WebAuthn helpers for Curatio.

The WebAuthn dependency is imported lazily so password-only deployments can still
boot while operators roll out passkeys. Endpoints that need passkeys return a
clear error if the optional dependency has not been installed.
"""

from __future__ import annotations

import base64
import ipaddress
import json
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from app.config import settings


@dataclass(frozen=True)
class WebAuthnConfig:
    rp_id: str
    rp_name: str
    origin: str


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _forwarded_value(request: Request, key: str) -> str:
    """Return a value from the RFC 7239 Forwarded header, if present."""
    forwarded = request.headers.get("forwarded", "")
    first_hop = forwarded.split(",", 1)[0]
    for part in first_hop.split(";"):
        name, _, value = part.strip().partition("=")
        if name.lower() == key:
            return value.strip('"')
    return ""


def _host_is_private_or_internal(host: str | None) -> bool:
    """Return whether a host is unsuitable as a public WebAuthn RP ID."""
    if not host:
        return True
    host = host.strip("[]").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local") or "." not in host
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _public_request_url(request: Request):
    """Build the browser-facing URL from proxy headers with BASE_URL fallback.

    Docker/reverse-proxy deployments sometimes forward requests to the app with
    an internal Host header (for example ``curatio:8000`` or ``172.x.x.x``).
    WebAuthn verification must use the actual browser origin, so prefer explicit
    proxy headers, then fall back to BASE_URL when the request host is clearly
    internal.
    """
    forwarded_proto = _forwarded_value(request, "proto")
    forwarded_host = _forwarded_value(request, "host")
    x_forwarded_proto = (
        request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    )
    x_forwarded_host = (
        request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    )
    x_forwarded_port = (
        request.headers.get("x-forwarded-port", "").split(",", 1)[0].strip()
    )

    scheme = forwarded_proto or x_forwarded_proto or request.url.scheme
    host_header = forwarded_host or x_forwarded_host or request.headers.get("host", "")
    if x_forwarded_port and host_header and ":" not in host_header:
        default_port = (scheme == "https" and x_forwarded_port == "443") or (
            scheme == "http" and x_forwarded_port == "80"
        )
        if not default_port:
            host_header = f"{host_header}:{x_forwarded_port}"

    parsed = urlparse(f"{scheme}://{host_header}") if host_header else urlparse("")
    base_parsed = urlparse(settings.base_url or "")
    has_proxy_headers = bool(
        forwarded_host or x_forwarded_host or forwarded_proto or x_forwarded_proto
    )
    if (
        not parsed.hostname
        or (not has_proxy_headers and _host_is_private_or_internal(parsed.hostname))
    ) and base_parsed.hostname:
        return base_parsed
    return parsed if parsed.hostname else base_parsed


def is_secure_request(request: Request) -> bool:
    """Return whether the browser-facing request is HTTPS."""
    return _public_request_url(request).scheme == "https"


def cookie_secure(request: Request) -> bool:
    """Whether auth cookies get the ``Secure`` flag (always, unless disabled)."""
    if settings.force_secure_cookies:
        return True
    return is_secure_request(request)


def _origin_of(parsed) -> str:
    scheme, host, port = parsed.scheme, parsed.hostname, parsed.port
    if port and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def get_webauthn_config(request: Request) -> WebAuthnConfig:
    """WebAuthn RP values, pinned to BASE_URL (plus configured extra origins).

    Proxy and Host headers are client-controlled when nothing sits in front of
    the app, so they only choose between origins the operator configured; they
    never define a new RP ID or origin.
    """
    base = urlparse(settings.base_url or "")
    if not base.hostname:
        raise HTTPException(status_code=500, detail="Passkey RP host is not configured")
    allowed = {_origin_of(base): base}
    for raw in (settings.webauthn_extra_origins or "").split(","):
        extra = urlparse(raw.strip())
        if extra.scheme in ("http", "https") and extra.hostname:
            allowed.setdefault(_origin_of(extra), extra)

    chosen = base
    requested = _public_request_url(request)
    if requested.hostname:
        chosen = allowed.get(_origin_of(requested), base)
    return WebAuthnConfig(
        rp_id=chosen.hostname, rp_name=settings.app_name, origin=_origin_of(chosen)
    )


def load_webauthn():
    """Import py_webauthn on demand and map missing installs to an API error."""
    try:
        from webauthn import (  # type: ignore[import-not-found]
            generate_authentication_options,
            generate_registration_options,
            options_to_json,
            verify_authentication_response,
            verify_registration_response,
        )
        from webauthn.helpers.structs import (  # type: ignore[import-not-found]
            AuthenticationCredential,
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            RegistrationCredential,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
    except ImportError as exc:  # pragma: no cover - depends on optional package install
        raise HTTPException(
            status_code=503,
            detail="Passkeys are not available until the webauthn package is installed.",
        ) from exc

    return {
        "generate_authentication_options": generate_authentication_options,
        "generate_registration_options": generate_registration_options,
        "options_to_json": options_to_json,
        "verify_authentication_response": verify_authentication_response,
        "verify_registration_response": verify_registration_response,
        "AuthenticationCredential": AuthenticationCredential,
        "AuthenticatorSelectionCriteria": AuthenticatorSelectionCriteria,
        "PublicKeyCredentialDescriptor": PublicKeyCredentialDescriptor,
        "RegistrationCredential": RegistrationCredential,
        "ResidentKeyRequirement": ResidentKeyRequirement,
        "UserVerificationRequirement": UserVerificationRequirement,
    }


def options_to_dict(options, options_to_json) -> dict:
    return json.loads(options_to_json(options))


def credential_from_dict(model_cls, payload: dict):
    """Return a browser WebAuthn credential in a py_webauthn-compatible shape.

    py_webauthn 1.x used Pydantic credential models and expects callers to
    instantiate those models before verification. py_webauthn 2.x removed those
    Pydantic helpers and its verification functions parse JSON-compatible
    dictionaries directly, including browser-standard camelCase keys such as
    ``rawId`` and ``clientDataJSON``. Attempting to call the 2.x dataclass
    constructor with browser JSON raises errors like ``unexpected keyword
    argument 'rawId'``, so only build a model when the imported class exposes a
    Pydantic parsing API.
    """
    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(payload)
    if hasattr(model_cls, "parse_obj"):
        return model_cls.parse_obj(payload)
    return payload
