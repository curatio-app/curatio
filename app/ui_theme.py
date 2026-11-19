"""Shared HTML theme snippets for Curatio server-rendered pages.

Every server-rendered page (welcome, account, admin sign-in) uses the same two
cacheable stylesheets. The look is dark only, so there is no theme script and no
appearance control. Page behaviour lives in /static/js/*.js so the CSP can drop
'unsafe-inline' from script-src.
"""

import html

AUTH_STYLESHEETS = (
    '<link rel="stylesheet" href="/static/workspace-tokens.css">'
    '<link rel="stylesheet" href="/static/auth.css">'
)


def curatio_auth_style_tag() -> str:
    """Return the colour-scheme hint and the shared stylesheets."""
    return '<meta name="color-scheme" content="dark">' + AUTH_STYLESHEETS


def curatio_script_tags(*names: str) -> str:
    """Return deferred <script src> tags for /static/js/<name>.js, in order."""
    return "".join(
        f'<script src="/static/js/{html.escape(name, quote=True)}.js" defer></script>'
        for name in names
    )


def curatio_auth_header(*, section: str = "Account") -> str:
    """Return the shared brand bar."""
    return f"""<header class="auth-topbar">
    <div class="auth-brand"><img src="/static/logo.png" alt=""><div><strong>Curatio</strong><small>{html.escape(section)}</small></div></div>
</header>"""
