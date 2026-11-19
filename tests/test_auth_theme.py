"""Shared Obsidian theme and no-inline-script contract for server-rendered pages."""

import asyncio
import importlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

account = importlib.import_module("app.account")


@pytest.mark.parametrize(
    "page,form_hook",
    [
        (account.register_page, 'id="registerForm"'),
        (account.login_page, 'id="loginForm"'),
        (account.setup_passkey_page, 'id="createBtn"'),
        (account.setup_2fa_page, 'id="verifyForm"'),
        (account.pair_page, 'id="deviceClaimBtn"'),
        (account.activate_page, 'id="deviceClaimBtn"'),
        (account.import_page, 'id="csvInput"'),
        (account.security_page, 'id="passkeyList"'),
    ],
)
def test_account_pages_share_workspace_theme_and_preserve_form_hooks(page, form_hook):
    html = asyncio.run(page()).body.decode()

    assert '<link rel="stylesheet" href="/static/workspace-tokens.css">' in html
    assert '<link rel="stylesheet" href="/static/auth.css">' in html
    assert '<meta name="color-scheme" content="dark">' in html
    assert "theme-init.js" not in html
    assert "data-curatio-theme-select" not in html
    assert '<div class="container" role="main">' in html
    assert form_hook in html
    _assert_no_inline_script(html)
    for src in re.findall(r'<script src="([^"]+)"', html):
        assert src.startswith("/static/js/")
        assert (ROOT / src.lstrip("/")).is_file(), src


def _assert_no_inline_script(html: str) -> None:
    """Every <script> is external (src=) or a non-executed JSON data block."""
    for tag in re.findall(r"<script\b[^>]*>", html, flags=re.I):
        assert "src=" in tag or 'type="application/json"' in tag, tag
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.I), "inline event handler"


def test_welcome_and_admin_login_use_the_shared_theme_without_inline_script():
    from app.admin import _admin_login_html
    from app.landing import welcome_page_html

    for html in (welcome_page_html(), _admin_login_html()):
        assert '<link rel="stylesheet" href="/static/workspace-tokens.css">' in html
        assert '<link rel="stylesheet" href="/static/auth.css">' in html
        assert "auth-legacy.css" not in html
        assert "theme-init.js" not in html
        _assert_no_inline_script(html)
    assert (
        '<script src="/static/js/admin-login.js" defer></script>' in _admin_login_html()
    )


def test_obsidian_tokens_are_dark_only_and_legacy_assets_are_gone():
    tokens = (ROOT / "static" / "workspace-tokens.css").read_text()
    landing = (ROOT / "app" / "landing.py").read_text()

    assert "color-scheme: dark" in tokens
    assert "--ws-font-ui" in tokens and "--ws-font-mono" in tokens
    assert "--ws-accent: #ffffff" in tokens
    assert "--ws-accent-contrast: #000000" in tokens
    assert "--ws-canvas: #000000" in tokens
    assert "--curatio-canvas" in tokens
    assert "prefers-color-scheme" not in tokens
    assert "data-theme" not in tokens
    assert "@font-face" not in tokens
    assert "auth-legacy.css" not in landing
    assert not (ROOT / "static" / "auth-legacy.css").exists()
    assert not (ROOT / "static" / "theme-init.js").exists()
    for dead in ("landing_page_html", "auth_success_html", "auth_error_html"):
        assert dead not in landing


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_safe_next_only_allows_same_origin_paths():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const location = { origin: 'https://curatio.test', pathname: '/account/login', search: '' };
const window = {};
const document = { querySelectorAll() { return []; } };
vm.runInNewContext(source, { window, document, location, URL, URLSearchParams });
const { safeNext } = window.CuratioAuth;
assert.equal(safeNext('/account/pair?x=1#y'), '/account/pair?x=1#y');
assert.equal(safeNext('//evil.com'), '/');
assert.equal(safeNext('/\\evil.com'), '/');
assert.equal(safeNext('/\t/evil.com'), '/');
assert.equal(safeNext('https://evil.com/'), '/');
assert.equal(safeNext('javascript:alert(1)'), '/');
assert.equal(safeNext('', '/account/dashboard'), '/account/dashboard');
assert.equal(safeNext('https://curatio.test/account/security'), '/account/security');
const odd = safeNext('/%09/evil.com');
assert.ok(odd.startsWith('/') && !odd.startsWith('//'), odd);
"""
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "static" / "js" / "auth-common.js")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
