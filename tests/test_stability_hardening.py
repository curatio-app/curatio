"""Regression tests for the stability/performance hardening pass.

Covers the highest-value fixes that lacked direct coverage:
- the cross-user universal-catalog cache-key leak,
- the SSRF host-safety check + its DNS memoization,
- the ffmpeg/fpcalc process-reap helper.
"""

import os
import subprocess

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_stability.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")


# --- Cross-user catalog cache-key isolation -------------------------------


def test_cache_ids_sig_is_order_independent_and_typed():
    """Same logical id-set → same signature regardless of order or container."""
    from app.main import _cache_ids_sig

    assert _cache_ids_sig(None) == "0"
    assert _cache_ids_sig([]) == "0"
    assert _cache_ids_sig(set()) == "0"
    # Order- and container-independent.
    assert _cache_ids_sig([1, 2, 3]) == _cache_ids_sig([3, 2, 1])
    assert _cache_ids_sig({1, 2, 3}) == _cache_ids_sig([2, 1, 3])


def test_cache_ids_sig_distinguishes_different_sets():
    """Different taste/exclude sets MUST produce different signatures.

    This is the crux of the cross-user leak fix: the universal-catalog cache key
    folds in _cache_ids_sig(taste_tags) and _cache_ids_sig(exclude_ids), so one
    local user's personalized ordering + hidden watched items can no longer be
    served to another user under a shared (user=None) key.
    """
    from app.main import _cache_ids_sig

    user_a_watched = _cache_ids_sig({101, 102, 103})
    user_b_watched = _cache_ids_sig({201, 202})
    assert user_a_watched != user_b_watched
    # A superset differs from its subset.
    assert _cache_ids_sig({1, 2, 3}) != _cache_ids_sig({1, 2})


# --- SSRF host-safety check + DNS memoization -----------------------------


def test_is_safe_source_url_blocks_private_and_loopback():
    from app.net_safety import is_safe_source_url

    assert not is_safe_source_url("http://127.0.0.1/x")
    assert not is_safe_source_url("http://192.168.1.10/x")
    assert not is_safe_source_url("http://10.0.0.5/x")
    assert not is_safe_source_url(
        "http://169.254.169.254/latest/meta-data"
    )  # link-local
    assert not is_safe_source_url("http://localhost/x")
    assert not is_safe_source_url("ftp://example.com/x")
    assert not is_safe_source_url("")


def test_is_safe_source_url_memoizes_ip_literals():
    """A resolved host's verdict is cached so a slow resolver isn't re-hit."""
    import app.net_safety as sp

    sp._host_safety_cache.clear()
    # IP literals resolve to themselves via getaddrinfo, so they get cached.
    assert sp.is_safe_source_url("http://192.168.1.10/x") is False
    assert "192.168.1.10" in sp._host_safety_cache
    # Second call hits the cache and returns the same verdict.
    assert sp.is_safe_source_url("http://192.168.1.10/x") is False


# --- ffmpeg/fpcalc process reap helper ------------------------------------


def test_terminate_process_kills_running_child():
    from workers.fingerprint_worker import _terminate_process

    proc = subprocess.Popen(
        ["sleep", "60"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert proc.poll() is None  # running
    _terminate_process(proc)
    assert proc.poll() is not None  # reaped, no orphan


def test_terminate_process_is_noop_on_exited_child():
    from workers.fingerprint_worker import _terminate_process

    proc = subprocess.Popen(["true"])
    proc.wait()
    # Must not raise on an already-exited process, and must stay safe if called twice.
    _terminate_process(proc)
    _terminate_process(proc)


def test_terminate_process_tolerates_none():
    from workers.fingerprint_worker import _terminate_process

    _terminate_process(None)  # must not raise


# --- Expired-ephemeral-row reaper (Phase 2c) ------------------------------


def test_cleanup_expired_sessions_reaps_challenges_and_auth_codes():
    """The reaper must delete expired passkey challenges and expired/consumed
    mobile auth codes (previously untouched), and leave fresh rows alone."""
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Base, PasskeyChallenge, MobileAuthCode, User
    from app.auth import cleanup_expired_sessions

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    try:
        # MobileAuthCode.user_id is a NOT NULL FK (FK enforcement is on).
        user = User(user_key="testkey")
        db.add(user)
        db.commit()
        uid = user.id
        past = datetime.utcnow() - timedelta(hours=1)
        future = datetime.utcnow() + timedelta(hours=1)
        db.add(
            PasskeyChallenge(token="t1", challenge="c", intent="login", expires_at=past)
        )
        db.add(
            PasskeyChallenge(
                token="t2", challenge="c", intent="login", expires_at=future
            )
        )
        db.add(
            MobileAuthCode(
                code="m1",
                user_id=uid,
                code_challenge="x",
                redirect_uri="u",
                expires_at=past,
            )
        )
        db.add(
            MobileAuthCode(  # consumed but not yet expired -> still reaped
                code="m2",
                user_id=uid,
                code_challenge="x",
                redirect_uri="u",
                expires_at=future,
                consumed_at=datetime.utcnow(),
            )
        )
        db.add(
            MobileAuthCode(  # fresh, unconsumed -> survives
                code="m3",
                user_id=uid,
                code_challenge="x",
                redirect_uri="u",
                expires_at=future,
            )
        )
        db.commit()

        deleted = cleanup_expired_sessions(db)
        assert deleted == 3
        assert db.query(PasskeyChallenge).count() == 1
        assert db.query(MobileAuthCode).count() == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


# --- TOTP verify never 500s on a bad/undecryptable secret (Phase 4) -------


def test_verify_account_totp_handles_missing_and_bad_secret():
    from types import SimpleNamespace
    from app.auth import verify_account_totp

    # No secret -> invalid, no crash.
    assert (
        verify_account_totp(SimpleNamespace(totp_secret=None, id=1), "123456") is False
    )
    # A non-decryptable secret must return False (not raise -> 500 on the 2FA path).
    assert (
        verify_account_totp(
            SimpleNamespace(totp_secret="not-a-valid-token", id=1), "123456"
        )
        is False
    )
