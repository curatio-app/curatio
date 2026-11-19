"""
Authentication utilities for Curatio v2.

Provides password hashing, TOTP 2FA, and app pairing session management.
"""

import base64
import hashlib
import io
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import pyotp
import qrcode
import qrcode.constants
from base64 import b64encode
from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext
from sqlalchemy.orm import Session, object_session
from loguru import logger

from app.time_utils import utc_now  # noqa: E402
from app.config import settings
from app.crypto import encrypt_token, decrypt_token
from sqlalchemy import or_

from app.models import (
    Account,
    AppPairingSession,
    DevicePairingSession,
    MobileAuthCode,
    PasskeyChallenge,
    PasskeyCredential,
    UserSession,
    User,
)

# Password hashing context (bcrypt)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
DUMMY_PASSWORD_HASH = pwd_context.hash("curatio-dummy-password")


#: Challenge sent with every 401 from the profile API.
BEARER_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="curatio"'}


def invalid_profile_credential(
    detail: str = "Invalid profile credential",
) -> HTTPException:
    """401 for a missing, malformed, unknown or rotated profile key."""
    return HTTPException(status_code=401, detail=detail, headers=BEARER_CHALLENGE)


def bearer_user_key(request: Request) -> str:
    """Return the profile key sent as ``Authorization: Bearer <user_key>``.

    The native API (``/api/v2/...``) takes the key from this header only, never
    the URL, so it stays out of proxy, CDN and server access logs. Whether the
    key is valid is checked by the handler's normal profile lookup, which also
    answers 401.
    """
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 256:
        raise invalid_profile_credential("Missing profile credential")
    return token


BearerUserKey = Annotated[str, Depends(bearer_user_key)]


def get_client_ip(request) -> str:
    """Return the caller's IP for rate limiting / audit, resistant to spoofing.

    Behind a reverse proxy, ``request.client.host`` is the proxy's address and the
    left-most ``X-Forwarded-For`` entry is fully client-controlled. We therefore
    trust ``X-Forwarded-For`` for only ``settings.trusted_proxy_count`` hops and read
    the client address from the RIGHT (never the left-most, spoofable value). With
    the default of 0 trusted proxies we use the real socket peer, which cannot be
    forged by the client.
    """
    hops = settings.trusted_proxy_count
    forwarded = request.headers.get("x-forwarded-for", "") or ""
    if hops > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[max(0, len(parts) - hops)]
    elif forwarded:
        _warn_untrusted_proxy_once()
    client = getattr(request, "client", None)
    return client.host if client else "unknown"


_untrusted_proxy_warned = False


def _warn_untrusted_proxy_once() -> None:
    """Say once that a proxy is in front but its client IP is being ignored.

    With TRUSTED_PROXY_COUNT=0 behind nginx every caller looks like nginx, so
    the login and pairing rate limits become one bucket shared by everyone.
    """
    global _untrusted_proxy_warned
    if _untrusted_proxy_warned:
        return
    _untrusted_proxy_warned = True
    logger.warning(
        "Requests carry X-Forwarded-For but TRUSTED_PROXY_COUNT is 0: every "
        "client shares the proxy's rate-limit bucket. Set TRUSTED_PROXY_COUNT=1 "
        "behind the bundled nginx."
    )


def _token_digest(token: str) -> str:
    """Hash a session token for database storage and lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bcrypt_input(plain: str) -> str:
    """Return bcrypt-safe input without silently truncating new passwords."""
    encoded = plain.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("Password must be at most 72 UTF-8 bytes")
    return plain


def hash_password(plain: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(_bcrypt_input(plain))


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against its bcrypt hash."""
    try:
        candidate = _bcrypt_input(plain)
    except ValueError:
        return False
    return pwd_context.verify(candidate, hashed)


def dummy_verify_password(plain: str) -> bool:
    """Run a dummy bcrypt verification to reduce user-enumeration timing leaks."""
    try:
        candidate = _bcrypt_input(plain)
    except ValueError:
        candidate = ""
    return pwd_context.verify(candidate, DUMMY_PASSWORD_HASH)


def generate_totp_secret() -> str:
    """Generate a new TOTP secret (base32)."""
    return pyotp.random_base32()


def get_totp_provisioning_uri(secret: str, email: str) -> str:
    """Get the provisioning URI for a TOTP authenticator app."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=settings.app_name)


def verify_totp(secret: str, code: str) -> bool:
    """Verify a TOTP code with 1-window tolerance."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def totp_matching_step(secret: str, code: str) -> Optional[int]:
    """The 30-second time step a code belongs to (1-window tolerance), or None."""
    code = (code or "").strip()
    if not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    now = datetime.now(timezone.utc)
    for offset in (0, -1, 1):
        at = now + timedelta(seconds=offset * totp.interval)
        if secrets.compare_digest(totp.at(at), code):
            return totp.timecode(at)
    return None


def generate_totp_qr_data_url(provisioning_uri: str) -> str:
    """Generate a QR code as a base64 data URL for the given provisioning URI."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=6,
        border=2,
    )
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def encrypt_totp_secret(secret: str) -> str:
    """Encrypt a TOTP secret for storage."""
    return encrypt_token(secret)


def decrypt_totp_secret(encrypted: str) -> str:
    """Decrypt a stored TOTP secret."""
    return decrypt_token(encrypted)


def verify_account_totp(account, code: str) -> bool:
    """Verify a TOTP code against an account's stored secret.

    Returns False (never raises) when the secret can't be decrypted — a corrupt
    or rotated-away key must surface as an invalid code on the login/2FA path,
    not an uncaught InvalidToken → 500.
    """
    secret = getattr(account, "totp_secret", None)
    if not secret:
        return False
    try:
        decrypted = decrypt_totp_secret(secret)
    except Exception:
        # InvalidToken (rotated-away key) or any malformed-secret error must surface
        # as an invalid code on the login/2FA path, never an uncaught 500.
        logger.error(
            "totp_secret decrypt failed for account id={}",
            getattr(account, "id", "?"),
        )
        return False
    step = totp_matching_step(decrypted, code)
    if step is None:
        return False
    # Each code is single-use: a code (or an earlier one) already accepted for
    # this account is refused even while it is still inside the time window.
    last = getattr(account, "totp_last_step", None)
    if last is not None and step <= last:
        return False
    account.totp_last_step = step
    session = object_session(account)
    if session is not None:
        session.commit()
    return True


def account_passkey_count(db: Session, account: Account) -> int:
    """Return how many passkeys are registered to an account."""
    return (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.account_id == account.id)
        .count()
    )


def passkey_exempt(account: Account) -> bool:
    """Return whether an admin has granted a still-valid enrollment exemption.

    The escape hatch for someone stuck on a browser without WebAuthn, so a hard
    requirement can never permanently lock an account out.
    """
    until = getattr(account, "passkey_exempt_until", None)
    return bool(until and utc_now() < until)


def auth_requirement(db: Session, account: Account) -> str:
    """Return the blocking enrollment step for an account, or ``none``.

    Every account must hold at least one passkey, with TOTP enrolled behind it
    as the fallback for when the passkey device is unavailable — a password
    alone is never sufficient. Derived from live state rather than a stored
    flag, so existing password-only accounts are gated as soon as this ships
    and no backfill is needed.
    """
    if passkey_exempt(account):
        return "none"
    if account_passkey_count(db, account) == 0:
        return "passkey_required"
    if not account.totp_enabled:
        return "totp_required"
    return "none"


WEB_SESSION_LIFETIME = timedelta(hours=24)
# The desktop app stays signed in like the native apps: its session lasts 90
# days and the app renews it while it is used (POST /auth/desktop/renew).
DESKTOP_SESSION_LIFETIME = timedelta(days=90)


def create_user_session(
    account_id: int,
    db: Session,
    active_profile_id: Optional[int] = None,
    lifetime: timedelta = WEB_SESSION_LIFETIME,
) -> str:
    """Create a web login session for an account. Returns the session token.

    ``active_profile_id`` is the viewing profile selected for this session
    (Netflix-style switch); when omitted the caller's default profile applies.
    """
    token = secrets.token_hex(32)
    session = UserSession(
        token=_token_digest(token),
        account_id=account_id,
        active_profile_id=active_profile_id,
        expires_at=utc_now() + lifetime,
    )
    db.add(session)
    db.commit()
    return token


def get_user_session(token: str, db: Session) -> Optional[UserSession]:
    """Return the (non-expired) session row for a token, or None.

    Expired sessions are deleted as a side effect.
    """
    session = (
        db.query(UserSession).filter(UserSession.token == _token_digest(token)).first()
    )
    if not session:
        return None
    if utc_now() > session.expires_at:
        db.delete(session)
        db.commit()
        return None
    return session


def is_desktop_session(session: UserSession) -> bool:
    """A desktop-app session: created with the desktop lifetime, not a day's."""
    return bool(
        session.created_at
        and session.expires_at
        and session.expires_at - session.created_at > timedelta(days=2)
    )


def renew_desktop_session(token: str, db: Session) -> Optional[UserSession]:
    """Push a live desktop session's expiry out to the full lifetime again."""
    session = get_user_session(token, db)
    if not session or not is_desktop_session(session):
        return None
    session.expires_at = utc_now() + DESKTOP_SESSION_LIFETIME
    db.commit()
    return session


def verify_user_session(token: str, db: Session) -> Optional[Account]:
    """Verify a session token. Returns the authenticated Account or None."""
    session = get_user_session(token, db)
    if not session:
        return None
    account_id = session.account_id
    if account_id is None:
        # Legacy session that predates the migration: resolve via the profile.
        if session.user_id is None:
            return None
        profile = db.query(User).filter(User.id == session.user_id).first()
        account_id = profile.account_id if profile else None
    if account_id is None:
        return None
    return db.query(Account).filter(Account.id == account_id).first()


def set_session_active_profile(token: str, db: Session, profile_id: int) -> bool:
    """Set the active profile for an existing session. Returns True on success."""
    session = get_user_session(token, db)
    if not session:
        return False
    session.active_profile_id = profile_id
    db.commit()
    return True


def delete_user_session(token: str, db: Session) -> None:
    """Delete a session by its (plaintext) token, if present."""
    db.query(UserSession).filter(UserSession.token == _token_digest(token)).delete(
        synchronize_session=False
    )
    db.commit()


def _client_credentials_for_user(user: User) -> dict:
    """Return the credential payload expected by native Curatio clients."""
    return {
        "userKey": user.user_key,
        "baseURL": settings.base_url,
        # Native API root; send userKey as ``Authorization: Bearer``.
        "apiBase": settings.base_url,
    }


def create_mobile_auth_code(
    db: Session,
    *,
    user_id: int,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
) -> str:
    """Create a short-lived one-time mobile authorization code."""
    code = secrets.token_urlsafe(32)
    auth_code = MobileAuthCode(
        code=code,
        user_id=user_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    db.add(auth_code)
    db.commit()
    return code


def _pkce_s256_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _consume_auth_code(
    db: Session, *, code: str, code_verifier: str
) -> Optional[tuple[MobileAuthCode, User]]:
    """Verify and spend a one-time native authorization code (PKCE S256)."""
    auth_code = db.query(MobileAuthCode).filter(MobileAuthCode.code == code).first()
    if not auth_code:
        return None
    if auth_code.consumed_at is not None or utc_now() > auth_code.expires_at:
        return None
    if auth_code.code_challenge_method != "S256":
        return None

    try:
        challenge = _pkce_s256_challenge(code_verifier)
    except UnicodeEncodeError:
        challenge = ""
    if not secrets.compare_digest(challenge, auth_code.code_challenge):
        auth_code.consumed_at = utc_now()
        db.commit()
        return None

    user = db.query(User).filter(User.id == auth_code.user_id).first()
    if not user or not user.is_active:
        return None

    auth_code.consumed_at = utc_now()
    db.commit()
    return auth_code, user


def exchange_mobile_auth_code(
    db: Session, *, code: str, code_verifier: str
) -> Optional[dict]:
    """Exchange a one-time mobile authorization code for app credentials."""
    consumed = _consume_auth_code(db, code=code, code_verifier=code_verifier)
    if not consumed:
        return None
    _, user = consumed
    logger.info(f"Mobile auth code exchanged for user {user.id}")
    return _client_credentials_for_user(user)


def is_loopback_redirect_uri(redirect_uri: str) -> bool:
    """An RFC 8252 loopback redirect: http://127.0.0.1:<port>/... (or [::1])."""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(redirect_uri)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "::1"}
        and port is not None
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


def exchange_desktop_auth_code(
    db: Session, *, code: str, code_verifier: str
) -> Optional[dict]:
    """Exchange a desktop sign-in code for a long-lived web session.

    The desktop app signs in through the system browser (where passkeys and
    password managers work) and receives the code on a loopback redirect.
    """
    consumed = _consume_auth_code(db, code=code, code_verifier=code_verifier)
    if not consumed:
        return None
    auth_code, profile = consumed
    if not is_loopback_redirect_uri(auth_code.redirect_uri) or not profile.account_id:
        return None
    token = create_user_session(
        profile.account_id,
        db,
        active_profile_id=profile.id,
        lifetime=DESKTOP_SESSION_LIFETIME,
    )
    logger.info(f"Desktop sign-in for account {profile.account_id}")
    return {
        "session_token": token,
        "max_age": int(DESKTOP_SESSION_LIFETIME.total_seconds()),
    }


def _generate_short_code() -> str:
    """Generate a 6-character alphanumeric code (uppercase, no ambiguous chars)."""
    alphabet = string.ascii_uppercase + string.digits
    # Remove ambiguous characters: O, 0, I, 1, L
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "")
    alphabet = alphabet.replace("1", "").replace("L", "")
    return "".join(secrets.choice(alphabet) for _ in range(6))


def create_pairing_session(user_id: int, db: Session) -> dict:
    """
    Create a pairing session for app sign-in.

    Returns dict with token, short_code, qr_data_url, expires_at.
    """
    token = secrets.token_hex(32)

    # Generate unique short code (retry on collision)
    for _ in range(10):
        short_code = _generate_short_code()
        existing = (
            db.query(AppPairingSession)
            .filter(
                AppPairingSession.short_code == short_code,
                AppPairingSession.claimed == False,  # noqa: E712
                AppPairingSession.expires_at > utc_now(),
            )
            .first()
        )
        if not existing:
            break
    else:
        raise RuntimeError("Failed to generate unique short code")

    expires_at = utc_now() + timedelta(minutes=5)

    session = AppPairingSession(
        token=_token_digest(token),
        user_id=user_id,
        short_code=short_code,
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()

    pair_url = f"{settings.base_url}/auth/pair/{token}/claim"
    qr_data_url = generate_totp_qr_data_url(pair_url)

    return {
        "token": token,
        "short_code": short_code,
        "claim_url": pair_url,
        "qr_data_url": qr_data_url,
        "expires_at": expires_at.isoformat(),
    }


def claim_pairing_session(
    db: Session, *, token: Optional[str] = None, short_code: Optional[str] = None
) -> Optional[dict]:
    """
    Claim a pairing session by token or short_code.

    Returns dict with user_key and server_url, or None if invalid/expired/claimed.
    """
    if token:
        # Only the digest is stored (create_pairing_session), never the token.
        session = (
            db.query(AppPairingSession)
            .filter(AppPairingSession.token == _token_digest(token))
            .first()
        )
    elif short_code:
        session = (
            db.query(AppPairingSession)
            .filter(AppPairingSession.short_code == short_code.upper())
            .first()
        )
    else:
        return None

    if not session:
        return None
    if session.claimed:
        return None
    if utc_now() > session.expires_at:
        return None

    user = db.query(User).filter(User.id == session.user_id).first()
    if not user:
        return None

    # Conditional update: of two concurrent claims, only one flips the flag.
    won = (
        db.query(AppPairingSession)
        .filter(
            AppPairingSession.token == session.token,
            AppPairingSession.claimed.is_(False),
        )
        .update({"claimed": True}, synchronize_session=False)
    )
    db.commit()
    if not won:
        return None

    logger.info(f"Pairing session claimed for user {user.id}")

    return {
        "user_key": user.user_key,
        "api_base": settings.base_url,
    }


def create_device_pairing_session(db: Session) -> dict:
    """
    Create a device-initiated pairing session (no auth required).

    The device displays the short_code on screen. An authenticated user
    claims it via /auth/device/claim to link their account.

    Returns dict with device_token, short_code, expires_at.
    """
    device_token = secrets.token_hex(32)

    # Generate unique short code (retry on collision)
    for _ in range(10):
        short_code = _generate_short_code()
        existing = (
            db.query(DevicePairingSession)
            .filter(
                DevicePairingSession.short_code == short_code,
                DevicePairingSession.claimed == False,  # noqa: E712
                DevicePairingSession.expires_at > utc_now(),
            )
            .first()
        )
        if not existing:
            break
    else:
        raise RuntimeError("Failed to generate unique short code")

    expires_at = utc_now() + timedelta(minutes=5)

    session = DevicePairingSession(
        device_token=device_token,
        short_code=short_code,
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()

    return {
        "device_token": device_token,
        "short_code": short_code,
        "expires_at": expires_at.isoformat(),
    }


def claim_device_pairing_session(db: Session, short_code: str, user: User) -> bool:
    """
    Claim a device pairing session by short_code (authenticated user).

    Links the user's account to the device session so the device can
    retrieve user_key on its next poll.

    Returns True if claimed, False if invalid/expired/already claimed.
    """
    session = (
        db.query(DevicePairingSession)
        .filter(DevicePairingSession.short_code == short_code.upper())
        .first()
    )
    if not session:
        return False
    if session.claimed:
        return False
    if utc_now() > session.expires_at:
        return False

    won = (
        db.query(DevicePairingSession)
        .filter(
            DevicePairingSession.device_token == session.device_token,
            DevicePairingSession.claimed.is_(False),
        )
        .update({"user_id": user.id, "claimed": True}, synchronize_session=False)
    )
    db.commit()
    if not won:
        return False

    logger.info(f"Device pairing claimed by user {user.id}")
    return True


def poll_device_pairing_session(db: Session, device_token: str) -> Optional[dict]:
    """
    Poll a device pairing session by device_token.

    Returns dict with user_key and server_url if claimed, or None if still pending.
    """
    session = (
        db.query(DevicePairingSession)
        .filter(DevicePairingSession.device_token == device_token)
        .first()
    )
    if not session:
        return None
    if utc_now() > session.expires_at:
        return None
    if not session.claimed or not session.user_id:
        return None

    user = db.query(User).filter(User.id == session.user_id).first()
    if not user:
        return None

    return {
        "user_key": user.user_key,
        "api_base": settings.base_url,
    }


def cleanup_expired_sessions(db: Session) -> int:
    """Delete expired/consumed ephemeral rows and return the number removed.

    Covers every short-lived table: app + device pairing sessions, user sessions,
    passkey challenges, and mobile auth codes (also consumed ones). These tables
    have no other reaper, so without this they grow unbounded. Uses bulk deletes
    instead of loading every row.
    """
    now = utc_now()
    count = 0

    count += (
        db.query(AppPairingSession)
        .filter(AppPairingSession.expires_at < now)
        .delete(synchronize_session=False)
    )
    count += (
        db.query(DevicePairingSession)
        .filter(DevicePairingSession.expires_at < now)
        .delete(synchronize_session=False)
    )
    count += (
        db.query(UserSession)
        .filter(UserSession.expires_at < now)
        .delete(synchronize_session=False)
    )
    count += (
        db.query(PasskeyChallenge)
        .filter(PasskeyChallenge.expires_at < now)
        .delete(synchronize_session=False)
    )
    count += (
        db.query(MobileAuthCode)
        .filter(
            or_(
                MobileAuthCode.consumed_at.isnot(None),
                MobileAuthCode.expires_at < now,
            )
        )
        .delete(synchronize_session=False)
    )

    if count:
        db.commit()
        logger.info(f"Cleaned up {count} expired ephemeral rows")

    return count
