"""HTML for the public welcome page served at /."""

from app.ui_theme import curatio_auth_style_tag


def welcome_page_html() -> str:
    """Small public entry point; authentication lives at /account/login."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Curatio</title>{curatio_auth_style_tag()}</head>
<body class="welcome-page"><main class="welcome"><img class="mark" src="/static/logo.png" alt=""><h1>Curatio</h1><p>Your private, personal cinema—thoughtfully organized and ready to play on every screen.</p><div class="actions"><a class="btn" href="/account/login">Sign in</a><a class="btn btn-secondary" href="/account/register">Use an invite</a></div><small>Passkey-first. Your library stays yours.</small></main></body></html>"""
