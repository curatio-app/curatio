"""Regression tests for security and data-integrity hardening."""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_security.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import (  # noqa: E402
    Account,
    AdminSession,
    AppPairingSession,
    Base,
    MovieTag,
    Tag,
    UserSession,
    User,
    WatchProgress,
)


def _bearer(user_key):
    """Profile credential header for the native /api/v2 API."""
    return {"Authorization": f"Bearer {user_key}"}


def _make_account_with_profile(db, *, user_key, email=None):
    """Create an Account and one linked profile (User). Returns (account, user)."""
    account = Account(auth_source="local", email=email, is_active=True)
    db.add(account)
    db.commit()
    user = User(user_key=user_key, account_id=account.id, is_active=True)
    db.add(user)
    db.commit()
    return account, user


TEST_DATABASE_URL = "sqlite:///./test_security.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def test_movie_tags_are_distinct_by_media_type(db):
    tag = Tag(name="Noir", category="style")
    db.add(tag)
    db.flush()

    db.add_all(
        [
            MovieTag(tmdb_id=42, media_type="movie", tag_id=tag.id, confidence=0.9),
            MovieTag(tmdb_id=42, media_type="tv", tag_id=tag.id, confidence=0.8),
        ]
    )
    db.commit()

    rows = db.query(MovieTag).filter(MovieTag.tmdb_id == 42).all()
    assert {(row.media_type, row.confidence) for row in rows} == {
        ("movie", 0.9),
        ("tv", 0.8),
    }


@pytest.mark.asyncio
async def test_pairing_status_does_not_claim_session(db):
    from starlette.requests import Request
    import app.main as main

    user = User(user_key="user-key", is_active=True)
    db.add(user)
    db.flush()
    from app.auth import _token_digest

    session = AppPairingSession(
        token=_token_digest("token-1"),
        short_code="ABC123",
        user_id=user.id,
        expires_at=main.datetime.utcnow() + main.timedelta(minutes=5),
    )
    db.add(session)
    db.commit()

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    response = await main.pair_status(request, "token-1", db)

    status = db.query(AppPairingSession).filter_by(token=_token_digest("token-1")).one()
    assert response == {"status": "pending"}
    assert status.claimed is False


def test_watch_progress_model_has_logical_unique_constraint():
    constraints = {
        c.name: tuple(c.columns.keys())
        for c in WatchProgress.__table__.constraints
        if getattr(c, "name", None)
    }
    assert constraints["uq_progress_user_item_episode"] == (
        "user_id",
        "tmdb_id",
        "media_type",
        "season",
        "episode",
    )


def test_cached_user_rebinds_scalar_id_per_session(db):
    import app.main as main

    main._user_cache.clear()
    user = User(user_key="cached-user", is_active=True)
    db.add(user)
    db.commit()

    first = main._get_cached_user("cached-user", db)
    assert first is not None
    user_id = first.id
    assert main._user_cache["cached-user"][1] == user_id

    db.rollback()
    db.close()
    rebound_session = TestingSessionLocal()
    try:
        second = main._get_cached_user("cached-user", rebound_session)
        assert second is not None
        assert second.id == user_id
        assert second is not first
    finally:
        rebound_session.close()
        main._user_cache.clear()


@pytest.mark.asyncio
async def test_report_progress_updates_legacy_movie_null_episode_row(monkeypatch, db):
    import app.main as main

    user = User(user_key="progress-user", is_active=True)
    db.add(user)
    db.flush()
    db.add(
        WatchProgress(
            user_id=user.id,
            tmdb_id=123,
            media_type="movie",
            season=None,
            episode=None,
            position=30,
            duration=100,
            title="Legacy Title",
        )
    )
    db.commit()

    @contextmanager
    def _test_db():
        yield db

    monkeypatch.setattr(main, "get_db", _test_db)
    main._user_cache.clear()

    response = await main.report_progress(
        "progress-user",
        main.ProgressRequest(
            tmdb_id=123,
            media_type="movie",
            position=60,
            duration=100,
            title="Updated Title",
        ),
    )

    rows = db.query(WatchProgress).filter_by(user_id=user.id, tmdb_id=123).all()
    assert response == {"status": "ok"}
    assert len(rows) == 1
    assert rows[0].season is None
    assert rows[0].episode is None
    assert rows[0].position == 60
    assert rows[0].title == "Updated Title"
    main._user_cache.clear()


def test_long_utf8_passwords_are_rejected():
    from app.auth import hash_password, verify_password

    long_password = "🔒" * 40  # 160 UTF-8 bytes, above bcrypt's 72-byte limit
    with pytest.raises(ValueError):
        hash_password(long_password)
    assert (
        verify_password(
            long_password,
            "$2b$12$abcdefghijklmnopqrstuuq6F2uE8J1jFQy4J8O1z5QKJp0zQOqfS",
        )
        is False
    )


def test_user_session_tokens_are_hashed_at_rest(db):
    from app.auth import create_user_session, verify_user_session

    account, user = _make_account_with_profile(db, user_key="token-user")

    raw_token = create_user_session(account.id, db, active_profile_id=user.id)
    stored = db.query(UserSession).filter(UserSession.account_id == account.id).first()
    assert stored is not None
    assert stored.token != raw_token
    assert len(stored.token) == 64
    assert verify_user_session(raw_token, db) is not None


@pytest.mark.asyncio
async def test_auth_cookies_forced_secure_even_with_http_request(db, monkeypatch):
    from starlette.requests import Request
    import app.main as main
    import app.admin as admin

    account, user = _make_account_with_profile(db, user_key="secure-cookie-user")

    insecure_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": [(b"host", b"testserver"), (b"x-forwarded-proto", b"http")],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )

    user_resp = main._issue_account_session_response(
        insecure_request, account, db, {"status": "ok"}, active_profile_id=user.id
    )
    assert "Secure" in user_resp.headers["set-cookie"]

    # monkeypatch, not `admin.get_db = ...`. A bare assignment is never undone,
    # so every later test *file* in the same process kept resolving admin's
    # sessions against this module's database -- which the fixture drops at
    # teardown. Any admin endpoint tested after this one then failed with "no
    # such table", pointing at itself rather than here.
    @contextmanager
    def _test_admin_db():
        yield db

    monkeypatch.setattr(admin, "get_db", _test_admin_db)
    admin_resp = admin._issue_admin_session_response(insecure_request, user_id=user.id)
    assert "Secure" in admin_resp.headers["set-cookie"]


def test_admin_session_tokens_are_hashed_at_rest(db, monkeypatch):
    from starlette.requests import Request
    import app.admin as admin

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/api/login",
            "headers": [(b"host", b"testserver")],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )

    @contextmanager
    def _test_admin_db():
        yield db

    monkeypatch.setattr(admin, "get_db", _test_admin_db)
    response = admin._issue_admin_session_response(request, user_id=None)
    cookie = response.headers["set-cookie"]
    raw_token = cookie.split("admin_token=", 1)[1].split(";", 1)[0]

    stored = db.query(AdminSession).first()
    assert stored is not None
    assert stored.token != raw_token
    assert len(stored.token) == 64


def test_passkey_credential_dict_passthrough_for_webauthn_v2_dataclass():
    from app.passkeys import credential_from_dict

    class WebAuthnV2Credential:
        pass

    browser_payload = {
        "id": "credential-id",
        "rawId": "credential-id",
        "type": "public-key",
        "response": {
            "clientDataJSON": "client-data",
            "attestationObject": "attestation",
        },
    }

    assert (
        credential_from_dict(WebAuthnV2Credential, browser_payload) is browser_payload
    )


@pytest.mark.asyncio
async def test_mobile_auth_start_login_redirect_uses_relative_next(db):
    from urllib.parse import parse_qs, urlparse

    from starlette.requests import Request
    import app.main as main

    challenge = "A" * 43
    query_string = (
        "redirect_uri=curatio://auth/callback"
        "&state=client-state"
        f"&code_challenge={challenge}"
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/mobile/start",
            "query_string": query_string.encode(),
            "headers": [(b"host", b"testserver")],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )

    response = await main.mobile_auth_start(
        request,
        redirect_uri="curatio://auth/callback",
        code_challenge=challenge,
        state="client-state",
        db=db,
    )

    login_location = response.headers["location"]
    parsed_login = urlparse(login_location)
    assert parsed_login.path == "/account/login"
    next_values = parse_qs(parsed_login.query)["next"]
    assert next_values == [f"/auth/mobile/start?{query_string}"]
    assert not next_values[0].startswith("http://")


@pytest.mark.asyncio
async def test_mobile_auth_pkce_flow_returns_client_credentials(db):
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    from starlette.requests import Request
    import app.main as main
    from app.auth import create_user_session
    from app.models import MobileAuthCode

    account, user = _make_account_with_profile(
        db, user_key="mobile-user-key", email="ios@example.test"
    )
    _add_passkey(db, account)
    account.totp_enabled = True
    db.commit()
    session_token = create_user_session(account.id, db, active_profile_id=user.id)

    verifier = "A" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    start_url = (
        "http://testserver/auth/mobile/start?redirect_uri=curatio://auth/callback"
        f"&state=client-state&code_challenge={challenge}"
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/mobile/start",
            "query_string": start_url.split("?", 1)[1].encode(),
            "headers": [
                (b"host", b"testserver"),
                (b"cookie", f"user_session={session_token}".encode()),
            ],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )

    response = await main.mobile_auth_start(
        request,
        redirect_uri="curatio://auth/callback",
        code_challenge=challenge,
        state="client-state",
        db=db,
    )

    callback = response.headers["location"]
    parsed_callback = urlparse(callback)
    assert parsed_callback.scheme == "curatio"
    assert parsed_callback.netloc == "auth"
    query = parse_qs(parsed_callback.query)
    assert query["state"] == ["client-state"]
    code = query["code"][0]

    credentials = await main.mobile_auth_exchange(
        request,
        main.MobileAuthExchangeRequest(code=code, code_verifier=verifier),
        db,
    )
    assert credentials == {
        "userKey": "mobile-user-key",
        "baseURL": "http://localhost:8000",
        "apiBase": "http://localhost:8000",
    }
    auth_code = db.query(MobileAuthCode).filter_by(code=code).one()
    assert auth_code.consumed_at is not None

    with pytest.raises(main.HTTPException) as exc:
        await main.mobile_auth_exchange(
            request,
            main.MobileAuthExchangeRequest(code=code, code_verifier=verifier),
            db,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_mobile_auth_start_requires_native_redirect_uri(db):
    from starlette.requests import Request
    import app.main as main

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/mobile/start",
            "headers": [(b"host", b"testserver")],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )

    with pytest.raises(main.HTTPException) as exc:
        await main.mobile_auth_start(
            request,
            redirect_uri="https://example.test/callback",
            code_challenge="A" * 43,
            db=db,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_auth_capabilities_advertise_mobile_and_device_auth():
    import app.main as main

    capabilities = await main.auth_capabilities()

    assert capabilities["mobileAuth"]["supported"] is True
    assert capabilities["mobileAuth"]["pkceMethods"] == ["S256"]
    assert capabilities["mobileAuth"]["startURL"].endswith("/auth/mobile/start")
    assert capabilities["mobileAuth"]["exchangeURL"].endswith("/auth/mobile/exchange")
    assert capabilities["deviceCode"]["supported"] is True


def test_crypto_encrypts_with_versioned_prefix_and_decrypts():
    import app.crypto as crypto

    original_keys = crypto.settings.security_encryption_keys
    try:
        crypto.settings.security_encryption_keys = "active-key,fallback-key"
        ciphertext = crypto.encrypt_token("super-secret")
        assert ciphertext.startswith("v2:")
        assert crypto.decrypt_token(ciphertext) == "super-secret"
    finally:
        crypto.settings.security_encryption_keys = original_keys


def test_crypto_rotation_accepts_fallback_key_for_decryption():
    import app.crypto as crypto
    from cryptography.fernet import Fernet

    original_keys = crypto.settings.security_encryption_keys
    try:
        crypto.settings.security_encryption_keys = "new-active,old-key"
        old_fernet = Fernet(crypto._derive_fernet_key("old-key"))  # noqa: SLF001
        old_ciphertext = f"v2:{old_fernet.encrypt(b'legacy-value').decode()}"
        assert crypto.decrypt_token(old_ciphertext) == "legacy-value"
    finally:
        crypto.settings.security_encryption_keys = original_keys


def test_crypto_can_decrypt_legacy_unversioned_ciphertext():
    import app.crypto as crypto

    original_keys = crypto.settings.security_encryption_keys
    try:
        crypto.settings.security_encryption_keys = "some-new-key"
        legacy_ciphertext = (
            crypto._legacy_fernet().encrypt(b"legacy").decode()
        )  # noqa: SLF001
        assert crypto.decrypt_token(legacy_ciphertext) == "legacy"
    finally:
        crypto.settings.security_encryption_keys = original_keys


# ---- Mandatory passkey enrollment ----------------------------------------
#
# Passkeys are required on every account, with TOTP behind them as the fallback.
# The requirement is derived from live state rather than a stored flag, so it
# applies to existing password-only accounts the moment it ships. These tests pin
# the gate itself — it is enforced server-side precisely so that a hand-typed URL
# or a raw API call cannot walk past the client-side redirect.


@contextmanager
def _gate_client(db):
    """Yield a TestClient wired to the test session, with sync scheduling stubbed."""
    from fastapi.testclient import TestClient
    from unittest.mock import patch
    from app.database import get_db_dependency
    from app.main import app

    def _override_db():
        yield db

    app.dependency_overrides[get_db_dependency] = _override_db
    try:
        with patch("app.main._schedule_local_sync"):
            yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _sign_in(db, client, account, user):
    from app.auth import create_user_session

    token = create_user_session(account.id, db, active_profile_id=user.id)
    client.cookies.set("user_session", token)
    return token


def _add_passkey(db, account, credential_id="cred-1"):
    from app.models import PasskeyCredential

    credential = PasskeyCredential(
        account_id=account.id,
        credential_id=credential_id,
        public_key="test-public-key",
    )
    db.add(credential)
    db.commit()
    return credential


def test_auth_requirement_walks_passkey_then_totp(db):
    from app.auth import auth_requirement

    account, _user = _make_account_with_profile(
        db, user_key="req-user", email="req@example.com"
    )

    # A password-only account is blocked on the passkey first.
    assert auth_requirement(db, account) == "passkey_required"

    _add_passkey(db, account)
    assert auth_requirement(db, account) == "totp_required"

    account.totp_enabled = True
    db.commit()
    assert auth_requirement(db, account) == "none"


def test_passkey_exemption_only_counts_while_unexpired(db):
    from datetime import datetime, timedelta
    from app.auth import auth_requirement

    account, _user = _make_account_with_profile(
        db, user_key="exempt-user", email="exempt@example.com"
    )
    assert auth_requirement(db, account) == "passkey_required"

    account.passkey_exempt_until = datetime.utcnow() + timedelta(days=1)
    db.commit()
    assert auth_requirement(db, account) == "none"

    account.passkey_exempt_until = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    assert auth_requirement(db, account) == "passkey_required"


def test_password_only_account_is_blocked_from_protected_api(db):
    """The gate is server-side: a valid session is not enough without a passkey."""
    account, user = _make_account_with_profile(
        db, user_key="gated-user", email="gated@example.com"
    )
    with _gate_client(db) as client:
        _sign_in(db, client, account, user)

        blocked = client.get("/account/api/profiles")
        assert blocked.status_code == 403
        assert blocked.headers["X-Auth-Requirement"] == "passkey_required"

        # ...but the endpoints needed to satisfy the requirement stay reachable.
        me = client.get("/auth/me")
        assert me.status_code == 200
        assert me.json()["auth_requirement"] == "passkey_required"
        assert client.get("/auth/passkeys").status_code == 200
        assert client.post("/auth/setup-2fa").status_code == 200

        # Enrolling clears the passkey step and moves the gate on to TOTP.
        _add_passkey(db, account)
        still_blocked = client.get("/account/api/profiles")
        assert still_blocked.status_code == 403
        assert still_blocked.headers["X-Auth-Requirement"] == "totp_required"

        account.totp_enabled = True
        db.commit()
        assert client.get("/account/api/profiles").status_code == 200


def test_dashboard_redirects_pending_enrollment_to_setup_page(db):
    """A browser gets a redirect to the setup page rather than a bare 403."""
    account, user = _make_account_with_profile(
        db, user_key="redirect-user", email="redirect@example.com"
    )
    with _gate_client(db) as client:
        _sign_in(db, client, account, user)

        resp = client.get("/account/dashboard", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/account/setup-passkey")

        _add_passkey(db, account)
        resp = client.get("/account/dashboard", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/account/setup-2fa")


def test_native_api_still_serves_an_account_pending_enrollment(db):
    """Native routes authenticate by user_key and must not be caught by the gate.

    Regression guard for enforcing in the session dependency rather than in
    middleware: a paired TV keeps working while its owner is mid-enrollment.
    """
    from unittest.mock import patch

    _account, _user = _make_account_with_profile(
        db, user_key="native-user", email="native@example.com"
    )
    import app.main as main

    main._user_cache.clear()
    with _gate_client(db) as client:
        with patch("app.main._schedule_local_sync"):
            assert (
                client.get(
                    "/api/v2/profiles", headers=_bearer("native-user")
                ).status_code
                == 200
            )


def test_last_passkey_cannot_be_deleted_even_with_a_password(db):
    """A password is not a substitute, so the final passkey is not removable."""
    from app.auth import hash_password

    account, user = _make_account_with_profile(
        db, user_key="delete-user", email="delete@example.com"
    )
    account.password_hash = hash_password("a-real-password")
    account.totp_enabled = True
    db.commit()
    only_key = _add_passkey(db, account, credential_id="only-key")

    with _gate_client(db) as client:
        _sign_in(db, client, account, user)

        refused = client.delete(f"/auth/passkeys/{only_key.id}")
        assert refused.status_code == 400
        assert "another passkey" in refused.json()["detail"]

        # With a second one on file, removing the first is fine again.
        second = _add_passkey(db, account, credential_id="second-key")
        assert client.delete(f"/auth/passkeys/{second.id}").status_code == 200


def test_admin_area_is_gated_for_sessions_but_not_for_master_password(db, monkeypatch):
    """Master-password admin login stays ungated as the break-glass route."""
    import app.admin as admin
    from starlette.requests import Request
    from app.auth import create_user_session
    from fastapi import HTTPException

    account, user = _make_account_with_profile(
        db, user_key="admin-user", email="admin@example.com"
    )
    account.is_admin = True
    db.commit()
    token = create_user_session(account.id, db, active_profile_id=user.id)

    @contextmanager
    def _test_admin_db():
        yield db

    monkeypatch.setattr(admin, "get_db", _test_admin_db)

    def _request_with(cookie):
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin/api/stats",
                "headers": [(b"host", b"testserver"), (b"cookie", cookie.encode())],
                "scheme": "http",
                "server": ("testserver", 80),
            }
        )

    with pytest.raises(HTTPException) as exc:
        admin.verify_admin(_request_with(f"user_session={token}"))
    assert exc.value.status_code == 403
    assert exc.value.headers["X-Auth-Requirement"] == "passkey_required"

    # The master-password path keeps the exemption controls reachable.
    admin_response = admin._issue_admin_session_response(
        _request_with("x=1"), user_id=None
    )
    admin_token = admin_response.headers["set-cookie"].split("admin_token=", 1)[1]
    admin_token = admin_token.split(";", 1)[0]
    assert admin.verify_admin(_request_with(f"admin_token={admin_token}")) is True

    # Once enrolled, the session path works too.
    _add_passkey(db, account)
    account.totp_enabled = True
    db.commit()
    assert admin.verify_admin(_request_with(f"user_session={token}")) is True


@pytest.mark.asyncio
async def test_admin_exemption_endpoint_grants_and_clears(db, monkeypatch):
    """The admin escape hatch so a hard requirement can't lock anyone out."""
    import app.admin as admin
    from datetime import datetime
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.auth import auth_requirement

    account, _user = _make_account_with_profile(
        db, user_key="exempt-api-user", email="exempt-api@example.com"
    )
    assert auth_requirement(db, account) == "passkey_required"

    @contextmanager
    def _test_admin_db():
        yield db

    monkeypatch.setattr(admin, "get_db", _test_admin_db)

    def _request(body):
        async def _receive():
            import json

            return {"type": "http.request", "body": json.dumps(body).encode()}

        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/admin/api/users/1/passkey-exemption",
                "headers": [(b"host", b"testserver")],
                "scheme": "http",
                "server": ("testserver", 80),
            },
            receive=_receive,
        )

    profile_id = _user.id
    granted = await admin.set_user_passkey_exemption(profile_id, _request({"days": 3}))
    assert granted["status"] == "ok"
    db.refresh(account)
    assert account.passkey_exempt_until > datetime.utcnow()
    assert auth_requirement(db, account) == "none"

    cleared = await admin.set_user_passkey_exemption(
        profile_id, _request({"clear": True})
    )
    assert cleared["passkey_exempt_until"] is None
    db.refresh(account)
    assert auth_requirement(db, account) == "passkey_required"

    # Out-of-range windows are refused rather than silently clamped.
    with pytest.raises(HTTPException) as exc:
        await admin.set_user_passkey_exemption(profile_id, _request({"days": 365}))
    assert exc.value.status_code == 400


def _desktop_request(path: str, query: str = "", cookie: str | None = None):
    from starlette.requests import Request

    headers = [(b"host", b"testserver")]
    if cookie:
        headers.append((b"cookie", f"user_session={cookie}".encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": query.encode(),
            "headers": headers,
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def _pkce_pair(verifier: str = "B" * 64):
    import base64
    import hashlib

    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


@pytest.mark.asyncio
async def test_desktop_sign_in_finishes_required_passkey_setup_in_the_browser(db):
    import app.main as main
    from app.auth import create_user_session

    account, user = _make_account_with_profile(
        db, user_key="enrol-key", email="enrol@example.test"
    )
    browser_session = create_user_session(account.id, db, active_profile_id=user.id)
    _, challenge = _pkce_pair()
    query = (
        "redirect_uri=http%3A%2F%2F127.0.0.1%3A53682%2Fcallback"
        f"&code_challenge={challenge}"
    )
    response = await main.desktop_auth_start(
        _desktop_request("/auth/desktop/start", query, cookie=browser_session),
        redirect_uri="http://127.0.0.1:53682/callback",
        code_challenge=challenge,
        db=db,
    )
    location = response.headers["location"]
    if main.auth_requirement(db, account) == "none":
        pytest.skip("this server does not require a passkey")
    assert location.startswith("/account/setup-passkey?next=")
    assert "%2Fauth%2Fdesktop%2Fstart%3F" in location


@pytest.mark.asyncio
async def test_desktop_sign_in_hands_back_a_long_lived_session(db, monkeypatch):
    """The desktop app signs in in the browser (passkeys work there) and gets a
    90-day web session through a loopback redirect, bound by PKCE."""
    from datetime import timedelta
    from urllib.parse import parse_qs, urlparse

    import app.main as main
    from app.auth import create_user_session, get_user_session, verify_user_session

    account, user = _make_account_with_profile(
        db, user_key="desktop-user-key", email="desktop@example.test"
    )
    browser_session = create_user_session(account.id, db, active_profile_id=user.id)
    monkeypatch.setattr(main, "auth_requirement", lambda *_: "none")
    verifier, challenge = _pkce_pair()
    redirect_uri = "http://127.0.0.1:53682/callback"
    request = _desktop_request("/auth/desktop/start", cookie=browser_session)

    response = await main.desktop_auth_start(
        request,
        redirect_uri=redirect_uri,
        code_challenge=challenge,
        state="desktop-state",
        db=db,
    )
    callback = urlparse(response.headers["location"])
    assert (callback.scheme, callback.netloc, callback.path) == (
        "http",
        "127.0.0.1:53682",
        "/callback",
    )
    query = parse_qs(callback.query)
    assert query["state"] == ["desktop-state"]

    result = await main.desktop_auth_exchange(
        request,
        main.MobileAuthExchangeRequest(code=query["code"][0], code_verifier=verifier),
        db,
    )
    assert result["max_age"] == 90 * 24 * 3600
    token = result["session_token"]
    assert verify_user_session(token, db).id == account.id
    session = get_user_session(token, db)
    assert session.active_profile_id == user.id
    assert session.expires_at - session.created_at > timedelta(days=89)

    # The code is single use.
    with pytest.raises(main.HTTPException):
        await main.desktop_auth_exchange(
            request,
            main.MobileAuthExchangeRequest(
                code=query["code"][0], code_verifier=verifier
            ),
            db,
        )


@pytest.mark.asyncio
async def test_desktop_sign_in_only_redirects_to_this_computer(db):
    import app.main as main

    _, challenge = _pkce_pair()
    for bad in (
        "https://evil.example/callback",
        "http://evil.example:8080/callback",
        "http://127.0.0.1/callback",  # no port: not a loopback listener
        "http://user@127.0.0.1:5000/callback",
        "curatio://auth/callback",
    ):
        with pytest.raises(main.HTTPException):
            await main.desktop_auth_start(
                _desktop_request("/auth/desktop/start"),
                redirect_uri=bad,
                code_challenge=challenge,
                db=db,
            )


@pytest.mark.asyncio
async def test_a_mobile_code_cannot_become_a_desktop_session(db):
    import app.main as main
    from app.auth import create_mobile_auth_code

    _, user = _make_account_with_profile(
        db, user_key="mobile-only-key", email="mobile-only@example.test"
    )
    verifier, challenge = _pkce_pair()
    code = create_mobile_auth_code(
        db,
        user_id=user.id,
        redirect_uri="curatio://auth/callback",
        code_challenge=challenge,
    )
    with pytest.raises(main.HTTPException):
        await main.desktop_auth_exchange(
            _desktop_request("/auth/desktop/exchange"),
            main.MobileAuthExchangeRequest(code=code, code_verifier=verifier),
            db,
        )


@pytest.mark.asyncio
async def test_desktop_sessions_renew_and_browser_sessions_do_not(db):
    from datetime import timedelta

    import app.main as main
    from app.auth import DESKTOP_SESSION_LIFETIME, create_user_session, get_user_session
    from app.time_utils import utc_now

    account, user = _make_account_with_profile(
        db, user_key="renew-key", email="renew@example.test"
    )
    desktop = create_user_session(
        account.id, db, active_profile_id=user.id, lifetime=DESKTOP_SESSION_LIFETIME
    )
    session = get_user_session(desktop, db)
    session.expires_at = utc_now() + timedelta(days=3)  # nearly lapsed
    db.commit()
    result = await main.desktop_auth_renew(
        _desktop_request("/auth/desktop/renew", cookie=desktop), db
    )
    assert result == {"max_age": 90 * 24 * 3600}
    assert get_user_session(desktop, db).expires_at > utc_now() + timedelta(days=89)

    browser = create_user_session(account.id, db, active_profile_id=user.id)
    with pytest.raises(main.HTTPException):
        await main.desktop_auth_renew(
            _desktop_request("/auth/desktop/renew", cookie=browser), db
        )


# ---- 2026-11 cleanup-pass hardening ---------------------------------------


def test_profile_routes_enforce_enrollment_like_account_routes(db):
    """A password-only session cannot mint a pairing code or browse the catalog.

    Profile-resolving routes used to skip the enrollment gate, so a stolen
    password could pair a device and walk away with a permanent ``user_key``.
    """
    account, user = _make_account_with_profile(
        db, user_key="pair-gate-user", email="pair-gate@example.com"
    )
    with _gate_client(db) as client:
        _sign_in(db, client, account, user)
        for method, path in (
            ("post", "/auth/pair/create"),
            ("get", "/account/api/browse"),
            ("get", "/account/api/dashboard"),
        ):
            resp = getattr(client, method)(path)
            assert resp.status_code == 403, path
            assert resp.headers["X-Auth-Requirement"] == "passkey_required"

        # The setup pages' status poll stays readable mid-enrollment.
        assert client.get("/auth/me").status_code == 200


def test_admin_email_login_requires_finished_enrollment(db, monkeypatch):
    """Only the master password is break-glass; account sign-in is gated."""
    import app.admin as admin
    from fastapi.testclient import TestClient
    from app.auth import hash_password
    from app.main import app

    account, _user = _make_account_with_profile(
        db, user_key="admin-gate-user", email="admin-gate@example.com"
    )
    account.is_admin = True
    account.password_hash = hash_password("a-long-admin-password")
    db.commit()

    @contextmanager
    def _test_admin_db():
        yield db

    monkeypatch.setattr(admin, "get_db", _test_admin_db)
    admin._ADMIN_LOGIN_ATTEMPTS.clear()
    client = TestClient(app)
    body = {"email": "admin-gate@example.com", "password": "a-long-admin-password"}

    resp = client.post("/admin/api/login", json=body)
    assert resp.status_code == 403
    assert resp.headers["X-Auth-Requirement"] == "passkey_required"

    account.is_active = False
    db.commit()
    assert client.post("/admin/api/login", json=body).status_code == 403


def test_admin_session_ends_when_account_is_demoted(db, monkeypatch):
    import app.admin as admin
    from fastapi import HTTPException
    from starlette.requests import Request

    account, _user = _make_account_with_profile(
        db, user_key="demoted-admin", email="demoted@example.com"
    )
    account.is_admin = True
    db.commit()

    @contextmanager
    def _test_admin_db():
        yield db

    monkeypatch.setattr(admin, "get_db", _test_admin_db)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"host", b"testserver")],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )
    response = admin._issue_admin_session_response(request, user_id=account.id)
    token = response.headers["set-cookie"].split("admin_token=", 1)[1].split(";")[0]
    assert admin._validate_admin_session_token(token) is True

    account.is_admin = False
    db.commit()
    with pytest.raises(HTTPException) as exc:
        admin._validate_admin_session_token(token)
    assert exc.value.status_code == 401
    assert db.query(AdminSession).count() == 0


def test_proxy_never_relays_renderable_content_types():
    import app.main as main

    assert main._safe_proxy_content_type("video/mp4") == "video/mp4"
    assert main._safe_proxy_content_type("application/vnd.apple.mpegurl") == (
        "application/vnd.apple.mpegurl"
    )
    for hostile in (
        "text/html; charset=utf-8",
        "image/svg+xml",
        "text/xml",
        "application/xhtml+xml",
        "",
        None,
    ):
        assert main._safe_proxy_content_type(hostile) == "application/octet-stream"
    assert "sandbox" in main._PROXY_SAFETY_HEADERS["content-security-policy"]
    assert main._PROXY_SAFETY_HEADERS["x-content-type-options"] == "nosniff"


def test_ffmpeg_inputs_are_restricted_to_the_loopback_proxy():
    from app.media_probe import SAFE_INPUT_ARGS, media_inspector

    command = media_inspector.command("http://127.0.0.1:8000/x", "t")
    joined = " ".join(command)
    assert " ".join(SAFE_INPUT_ARGS) in joined
    whitelist = SAFE_INPUT_ARGS[SAFE_INPUT_ARGS.index("-format_whitelist") + 1]
    assert "hls" not in whitelist.split(",")
    assert SAFE_INPUT_ARGS[SAFE_INPUT_ARGS.index("-protocol_whitelist") + 1] == (
        "http,tcp"
    )


def test_ssrf_guard_blocks_non_global_ranges_and_unresolvable_hosts(monkeypatch):
    import ipaddress
    import socket

    import app.net_safety as net_safety

    for address in ("100.64.0.1", "127.0.0.1", "10.1.2.3", "169.254.169.254"):
        assert net_safety._ip_is_blocked(ipaddress.ip_address(address)), address
    assert net_safety._ip_is_blocked(ipaddress.ip_address("::ffff:192.168.1.5"))
    assert not net_safety._ip_is_blocked(ipaddress.ip_address("8.8.8.8"))

    def _fail(*_args, **_kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(net_safety.socket, "getaddrinfo", _fail)
    net_safety._host_safety_cache.clear()
    assert net_safety.is_safe_source_url("https://does-not-resolve.example") is False


@pytest.mark.asyncio
async def test_public_only_transport_refuses_rebound_hosts(monkeypatch):
    import socket

    import httpx

    import app.net_safety as net_safety

    async def _resolve_to_loopback(self, host, port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(
        type(__import__("asyncio").get_running_loop()),
        "getaddrinfo",
        _resolve_to_loopback,
    )
    async with httpx.AsyncClient(
        transport=net_safety.public_only_transport()
    ) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://rebinding.example/")


def test_totp_codes_are_single_use(db):
    import pyotp

    from app.auth import encrypt_totp_secret, verify_account_totp

    account, _user = _make_account_with_profile(
        db, user_key="totp-replay", email="totp@example.com"
    )
    secret = pyotp.random_base32()
    account.totp_secret = encrypt_totp_secret(secret)
    account.totp_enabled = True
    db.commit()

    code = pyotp.TOTP(secret).now()
    assert verify_account_totp(account, code) is True
    assert verify_account_totp(account, code) is False
    assert verify_account_totp(account, "000000") is False


def test_qr_pairing_claims_by_token_exactly_once(db):
    from app.auth import claim_pairing_session, create_pairing_session

    _account, user = _make_account_with_profile(
        db, user_key="qr-pair-user", email="qr@example.com"
    )
    created = create_pairing_session(user.id, db)
    first = claim_pairing_session(db, token=created["token"])
    assert first and first["user_key"] == "qr-pair-user"
    assert claim_pairing_session(db, token=created["token"]) is None


def test_webauthn_origin_is_pinned_to_base_url(monkeypatch):
    from starlette.requests import Request

    import app.passkeys as passkeys

    def _request(host):
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [
                    (b"host", host),
                    (b"x-forwarded-host", host),
                    (b"x-forwarded-proto", b"https"),
                ],
                "scheme": "http",
                "server": ("testserver", 80),
            }
        )

    monkeypatch.setattr(passkeys.settings, "base_url", "https://curatio.example")
    monkeypatch.setattr(passkeys.settings, "webauthn_extra_origins", "")
    forged = passkeys.get_webauthn_config(_request(b"evil.example"))
    assert (forged.rp_id, forged.origin) == (
        "curatio.example",
        "https://curatio.example",
    )

    monkeypatch.setattr(
        passkeys.settings, "webauthn_extra_origins", "https://tv.curatio.example"
    )
    extra = passkeys.get_webauthn_config(_request(b"tv.curatio.example"))
    assert extra.origin == "https://tv.curatio.example"


@pytest.mark.asyncio
async def test_mobile_sign_in_finishes_required_enrollment_first(db):
    """The app's code is a credential: a password-only browser sets up first."""
    from starlette.requests import Request
    import app.main as main
    from app.auth import create_user_session

    account, user = _make_account_with_profile(
        db, user_key="mobile-gated-key", email="ios-gated@example.test"
    )
    session_token = create_user_session(account.id, db, active_profile_id=user.id)
    challenge = "A" * 43
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/mobile/start",
            "query_string": (
                "redirect_uri=curatio://auth/callback&state=s"
                f"&code_challenge={challenge}"
            ).encode(),
            "headers": [
                (b"host", b"testserver"),
                (b"cookie", f"user_session={session_token}".encode()),
            ],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )
    response = await main.mobile_auth_start(
        request,
        redirect_uri="curatio://auth/callback",
        code_challenge=challenge,
        state="s",
        db=db,
    )
    assert response.headers["location"].startswith("/account/setup-passkey")


def test_fingerprint_worker_reads_sources_only_through_the_proxy(monkeypatch):
    """The worker's ffmpeg/ffprobe never open a source URL themselves.

    Opening it directly would let ffmpeg follow redirects or playlist entries
    to internal addresses without the proxy's per-hop SSRF checks.
    """
    from urllib.parse import parse_qs, urlparse

    import workers.fingerprint_worker as fw
    from app.config import settings
    from app.media_probe import SAFE_INPUT_ARGS
    from app.remux import verify_proxy_token

    source = "https://cdn.example/episode.mkv"
    captured: list[list[str]] = []

    def _fake_check_output(command, **_kwargs):
        captured.append(command)
        return (1).to_bytes(4, "little")

    class _Done:
        stdout = '{"format": {"duration": "60"}}'
        stderr = ""
        returncode = 0

    def _fake_run(command, **_kwargs):
        captured.append(command)
        return _Done()

    monkeypatch.setattr(fw.subprocess, "check_output", _fake_check_output)
    monkeypatch.setattr(fw.subprocess, "run", _fake_run)
    fw._extract_ffmpeg_chromaprint_raw(source, 0, 30)
    fw._probe_duration(source)

    assert len(captured) == 2
    for command in captured:
        assert source not in command
        assert " ".join(SAFE_INPUT_ARGS) in " ".join(command)
        target = command[command.index("-i") + 1]
        parsed = urlparse(target)
        assert (parsed.hostname, parsed.path) == (
            "127.0.0.1",
            "/account/api/playback/proxy",
        )
        assert parse_qs(parsed.query)["url"] == [source]
        token = command[command.index("-headers") + 1]
        token = token.split(": ", 1)[1].strip()
        assert verify_proxy_token(source, token, secret_key=settings.secret_key)


def _plain_request(headers=None, client=("203.0.113.9", 4321)):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
            ],
            "client": client,
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def test_client_ip_ignores_forwarded_header_without_trusted_proxy(monkeypatch):
    import app.auth as auth

    monkeypatch.setattr(auth.settings, "trusted_proxy_count", 0)
    request = _plain_request(
        {"X-Forwarded-For": "1.2.3.4, 198.51.100.7"}, client=("127.0.0.1", 1)
    )
    assert auth.get_client_ip(request) == "127.0.0.1"


def test_client_ip_reads_rightmost_hop_behind_one_trusted_proxy(monkeypatch):
    import app.auth as auth

    monkeypatch.setattr(auth.settings, "trusted_proxy_count", 1)
    # The left-most value is client-supplied and must never be trusted.
    spoofed = _plain_request(
        {"X-Forwarded-For": "1.2.3.4, 198.51.100.7"}, client=("127.0.0.1", 1)
    )
    assert auth.get_client_ip(spoofed) == "198.51.100.7"
    direct = _plain_request(client=("198.51.100.8", 1))
    assert auth.get_client_ip(direct) == "198.51.100.8"


def test_provider_api_keys_are_encrypted_at_rest(db, monkeypatch):
    import app.admin as admin
    from app.models import AdminSetting

    @contextmanager
    def _test_admin_db():
        yield db
        db.commit()

    monkeypatch.setattr(admin, "get_db", _test_admin_db)
    stored = admin._stored_setting_value("OPENAI_API_KEY", "sk-test-secret-value")
    assert stored.startswith("v2:") and "sk-test-secret-value" not in stored
    assert admin._setting_plaintext("OPENAI_API_KEY", stored) == "sk-test-secret-value"
    # Non-secret settings stay readable.
    assert admin._stored_setting_value("APP_NAME", "Curatio") == "Curatio"

    # A legacy plaintext row is applied and re-encrypted on the next load.
    db.add(AdminSetting(key="TMDB_API_KEY", value="legacy-plain-tmdb-key"))
    db.commit()
    applied = {}
    monkeypatch.setattr(admin, "_apply_setting", lambda k, v: applied.__setitem__(k, v))
    admin.load_settings_from_db()
    row = db.get(AdminSetting, "TMDB_API_KEY")
    assert row.value.startswith("v2:")
    assert applied["TMDB_API_KEY"] == "legacy-plain-tmdb-key"


def test_undecryptable_provider_key_is_not_applied(monkeypatch):
    import app.admin as admin

    assert admin._setting_plaintext("GEMINI_API_KEY", "v2:not-a-fernet-token") == ""
