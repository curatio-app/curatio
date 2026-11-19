import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JS = ROOT / "static" / "js"


def _account_js() -> str:
    """Account page behaviour, which lives in static/js rather than inline."""
    return "\n".join(path.read_text() for path in sorted(JS.glob("account-*.js")))


def test_user_login_has_prominent_passkey_button_and_password_fallback():
    html_source = (ROOT / "app" / "account.py").read_text()
    theme_source = (ROOT / "app" / "ui_theme.py").read_text()
    tokens_source = (ROOT / "static" / "workspace-tokens.css").read_text()
    login_js = (JS / "account-login.js").read_text()

    assert 'id="passkeyLoginBtn"' in html_source
    assert "Log In with Passkey" in html_source
    assert "optional for passkey" in html_source
    assert "Use password / 2FA backup instead" in html_source
    assert "document.getElementById('email').value.trim()" in login_js
    assert "curatio_auth_style_tag" in html_source
    assert '"auth-common", "account-login"' in html_source
    assert 'href="/static/workspace-tokens.css"' in theme_source
    assert 'href="/static/auth.css"' in theme_source
    assert "--ws-canvas: #000000" in tokens_source
    assert "data-theme" not in tokens_source


def test_admin_login_defaults_to_passkey_with_password_2fa_fallback():
    from app.admin import _admin_login_html

    html_source = _admin_login_html()

    assert 'id="login-form"' in html_source
    assert 'id="passkey-login-btn"' in html_source
    assert "Sign in with passkey" in html_source
    assert "Admin email" in html_source and "(optional for passkey)" in html_source
    assert "Use password / 2FA backup instead" in html_source
    assert 'id="login-password"' in html_source
    assert 'href="/static/auth.css"' in html_source
    assert '<script src="/static/js/admin-login.js" defer></script>' in html_source


def test_landing_login_omits_invite_install_section():
    landing_source = (ROOT / "app" / "landing.py").read_text()

    assert 'href="/account/login"' in landing_source
    assert 'href="/account/register"' in landing_source
    assert "Install with invite code" not in landing_source
    assert "verifyAndInstall" not in landing_source
    assert "invite-code" not in landing_source


def test_user_passkey_failure_auto_reveals_password_fallback():
    account_source = (JS / "account-login.js").read_text()

    # User login: <details> auto-opens on passkey failure (not supported or API error)
    assert "document.querySelector('details').open = true;" in account_source
    # Enter key on email input triggers passkey button, not hidden form submit
    assert (
        "e.key === 'Enter'" in account_source
        and "document.getElementById('passkeyLoginBtn').click();" in account_source
    )


def test_admin_master_password_toggle_uses_password_fallback_form():
    admin_source = (JS / "admin-login.js").read_text()

    assert "masterMode=!masterMode" in admin_source
    assert "placeholder=masterMode?'Master Password':'Password'" in admin_source
    assert "Use admin email instead" in admin_source
    assert "Use master password instead" in admin_source


def test_admin_passkey_failure_auto_reveals_password_fallback():
    admin_source = (JS / "admin-login.js").read_text()

    assert "$('password-toggle').open=true" in admin_source
    assert "$('passkey-login-btn').click()" in admin_source
    assert "'/admin/api/passkeys/login/begin'" in admin_source
    assert "'/admin/api/passkeys/login/finish'" in admin_source


def test_passkey_login_challenges_support_discoverable_credentials():
    main_source = (ROOT / "app" / "main.py").read_text()
    admin_source = (ROOT / "app" / "admin.py").read_text()

    assert "email or None" in main_source
    assert "if allow_credentials is not None:" in main_source
    assert (
        "challenge_row.account_id and stored.account_id != challenge_row.account_id"
        in main_source
    )
    assert 'account: "Account | None"' in admin_source
    assert (
        "challenge.account_id and stored.account_id != challenge.account_id"
        in admin_source
    )


def test_admin_invite_copy_uses_the_browser_clipboard_api():
    access_source = (ROOT / "src" / "admin" / "pages" / "AccessPages.tsx").read_text()

    assert "navigator.clipboard.writeText(invite.invite_url)" in access_source
    assert 'variant="danger" size="compact"' in access_source


def test_admin_login_handler_and_login_401_flow_are_preserved():
    admin_source = (JS / "admin-login.js").read_text()

    assert "$('passkey-login-btn').addEventListener('click'" in admin_source
    assert "$('login-form').addEventListener('submit'" in admin_source
    assert "event.preventDefault()" in admin_source
    assert "await api('/admin/api/login',payload)" in admin_source
    assert "payload.totp_code=$('login-totp').value.trim()" in admin_source
    assert "error.message==='2FA code required'" in admin_source
    assert "$('login-totp').focus()" in admin_source


def test_admin_account_recovery_actions_require_confirmation():
    access_source = (ROOT / "src" / "admin" / "pages" / "AccessPages.tsx").read_text()

    assert "/reset-passkeys" in access_source
    assert "Remove every passkey from this account?" in access_source
    assert "/reset-2fa" in access_source
    assert "Disable 2FA for this account?" in access_source


def test_web_dashboard_hero_uses_tmdb_logos_when_available():
    main_source = (ROOT / "app" / "main.py").read_text()
    hero_source = (ROOT / "src" / "components" / "HeroCarousel.tsx").read_text()
    media_utils_source = (ROOT / "src" / "api" / "mediaUtils.ts").read_text()

    # The hero renders the TMDB logo lockup when a logo is available.
    assert "logoImage(item)" in hero_source
    assert "styles.logo" in hero_source
    assert "logo_path ?? item?.logo" in media_utils_source
    assert "'/account/api/details?tmdb_id='" not in main_source
    assert (
        '"logo": _image_url(meta.logo_path if meta else None, "original")'
        in main_source
    )


def test_browser_playback_uses_vetted_client_remux() -> None:
    """
    Playback rescue prefers vetted browser libraries, with a bounded server HLS
    remux only when MSE/client preparation is unavailable or fails. Both paths
    copy H.264 video so hardware decode is preserved.
    Guard that we did NOT reintroduce the old hand-rolled MSE worker (which
    produced wrong color / silent audio) and did NOT bundle a heavyweight
    ffmpeg.wasm, and that the real client modules are wired in.
    """
    main_source = (ROOT / "app" / "main.py").read_text()
    app_source = (ROOT / "src" / "App.tsx").read_text()

    # The broken hand-rolled remux worker is gone for good.
    assert not (ROOT / "static" / "remux-worker.js").exists()
    assert "playWithRemuxWorker" not in main_source
    assert "remux-worker.js" not in main_source
    assert "playWithRemuxWorker" not in app_source

    # No heavyweight ffmpeg.wasm bundled into the server or the dashboard entry.
    assert "ffmpeg.wasm" not in main_source
    assert "ffmpeg-core.js" not in main_source
    assert "ffmpeg.wasm" not in app_source

    # No client-side converters: containers/audio the browser can't play are
    # prepared by the server as HLS. The web player keeps exactly three engines
    # (<video>, hls.js, native HLS) and decides the path from server facts.
    player_dir = ROOT / "src" / "player"
    assert not (player_dir / "mseRemux.ts").exists()
    assert not (player_dir / "audioTranscode.ts").exists()
    assert not (player_dir / "subtitleExtract.ts").exists()
    package_source = (ROOT / "package.json").read_text()
    assert "mediabunny" not in package_source
    assert "@libav.js" not in package_source
    assert "ffmpeg" not in package_source
    plan_source = (player_dir / "playbackPlan.ts").read_text()
    assert "'server-hls'" in plan_source
    assert "decodingInfo" in plan_source
    player_sources = "\n".join(path.read_text() for path in player_dir.glob("*.ts*"))
    assert "ffmpeg.wasm" not in player_sources
    assert "startRemuxSession" in player_sources
    assert "keepaliveRemuxSession" in (player_dir / "serverSession.ts").read_text()

    # The hardened compatibility manager is present and profile-owned.
    remux_source = (ROOT / "app" / "remux.py").read_text()
    assert "/account/api/playback/remux/" in main_source
    assert "max_concurrent" in remux_source
    assert "profile_id" in remux_source
    # Video is copied whenever the browser can decode it (hardware decode is
    # preserved); audio is normalized to AAC.
    assert 'video_args = ["-c:v", "copy"]' in remux_source
    assert '"-c:a",\n                "aac"' in remux_source

    # Stream sorter must still prefer browser-native sources.


def test_playback_proxy_endpoint_present_and_guarded() -> None:
    """The same-origin media byte proxy must be wired and SSRF-guarded.

    Browser playback fetches stream bytes through this proxy so that hosts with
    broken/duplicated CORS headers (and Safari's strict byte-range handling) can't
    block playback. It remains a pure byte passthrough and is also the SSRF-safe
    input boundary for server compatibility sessions.
    """
    main_source = (ROOT / "app" / "main.py").read_text()
    engine_source = (ROOT / "src" / "player" / "playerEngine.ts").read_text()

    # Server: range-aware passthrough endpoint and every redirect stay guarded.
    assert '"/account/api/playback/proxy"' in main_source
    assert "is_safe_source_url" in main_source
    assert "StreamingResponse" in main_source
    assert "verify_proxy_token" in main_source
    assert "remux_manager" in main_source

    # Client: remux/transcode + the Safari native retry route through the proxy.
    assert "proxiedUrl" in engine_source
    assert "/account/api/playback/proxy" in engine_source


def test_docker_image_includes_static_worker_assets():
    dockerfile = (ROOT / "Dockerfile").read_text()
    main_source = (ROOT / "app" / "main.py").read_text()

    assert "COPY static/ ./static/" in dockerfile
    web_entry_copy = next(
        line for line in dockerfile.splitlines() if line.startswith("COPY index.html")
    )
    assert "admin.html" in web_entry_copy
    assert 'STATIC_DIR = BASE_DIR / "static"' in main_source
    assert "StaticFiles(directory=STATIC_DIR)" in main_source


def test_html_security_headers_and_csp_controls_present():
    main_source = (ROOT / "app" / "main.py").read_text()
    config_source = (ROOT / "app" / "config.py").read_text()

    assert "def _html_security_headers" in main_source
    assert '"X-Content-Type-Options": "nosniff"' in main_source
    assert '"Referrer-Policy": "strict-origin-when-cross-origin"' in main_source
    assert (
        '"Permissions-Policy": "camera=(), microphone=(), geolocation=()"'
        in main_source
    )
    assert '"X-Frame-Options": "DENY"' in main_source
    assert '"Strict-Transport-Security"' in main_source
    assert '"Content-Security-Policy-Report-Only"' in main_source
    assert '"Content-Security-Policy"' in main_source
    assert "security_csp_report_only" in config_source
    assert "security_csp" in config_source


def test_register_page_offers_passkey_only_with_no_password_path():
    """Signup is passkey-first with no password-only escape hatch."""
    account_source = (ROOT / "app" / "account.py").read_text() + _account_js()

    assert "Create Account with Passkey" in account_source
    # The password fallback form and its /auth/register submit handler are gone.
    assert "Use password fallback instead" not in account_source
    assert "Create Password Account" not in account_source
    assert "'/auth/register'" not in account_source


def test_setup_passkey_page_is_a_blocking_enrollment_step():
    """The enrollment page enrolls or nothing — no skip, no client-side bypass."""
    account_source = (ROOT / "app" / "account.py").read_text()
    enroll_js = (JS / "account-setup-passkey.js").read_text()

    assert 'router.get("/setup-passkey"' in account_source
    assert "Create Your Passkey" in account_source
    assert "/auth/passkeys/register/begin" in enroll_js
    assert "/auth/passkeys/register/finish" in enroll_js
    # No WebAuthn means an admin exemption, not a self-service skip.
    assert "ask an administrator for a temporary exemption" in enroll_js
    # Enrollment hands off to the 2FA step when that is still outstanding.
    assert "auth_requirement === 'totp_required'" in enroll_js


def test_login_redirects_branch_on_the_passkey_requirement():
    """Sign-in routes a passkey-less account to enrollment via the shared helper."""
    common_source = (JS / "auth-common.js").read_text()

    assert "auth_requirement" in common_source
    assert "/account/setup-passkey?next=" in common_source
    for name in ("account-login.js", "account-register.js"):
        assert "redirectAfterAuth(" in (JS / name).read_text()


def test_admin_users_table_exposes_passkey_exemption_controls():
    admin_source = (ROOT / "app" / "admin.py").read_text()
    access_source = (ROOT / "src" / "admin" / "pages" / "AccessPages.tsx").read_text()

    assert "/passkey-exemption" in admin_source
    assert "/passkey-exemption" in access_source
    assert "7-day exemption" in access_source
    assert "Clear exemption" in access_source
    # Zero passkeys is flagged in the table unless an exemption is in force.
    assert "passkey_exempt_until" in access_source
