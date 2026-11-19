"""
Account management web pages for Curatio v2.

Provides HTML pages for registration, login, 2FA setup, and app pairing.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.ui_theme import (
    curatio_auth_header,
    curatio_auth_style_tag,
    curatio_script_tags,
)

router = APIRouter(prefix="/account", tags=["account"])

_COMMON_STYLES = curatio_auth_style_tag()
_COMMON_HEADER = curatio_auth_header()

# Page behaviour is served from /static/js so no page carries an inline script.
_REGISTER_SCRIPTS = curatio_script_tags("auth-common", "account-register")
_LOGIN_SCRIPTS = curatio_script_tags("auth-common", "account-login")
_SETUP_PASSKEY_SCRIPTS = curatio_script_tags("auth-common", "account-setup-passkey")
_SETUP_2FA_SCRIPTS = curatio_script_tags("auth-common", "account-setup-2fa")
_DEVICE_CLAIM_SCRIPTS = curatio_script_tags("auth-common", "account-device-claim")
_IMPORT_SCRIPTS = curatio_script_tags("account-import")
_SECURITY_SCRIPTS = curatio_script_tags("auth-common", "account-security")


@router.get("/register", response_class=HTMLResponse)
async def register_page():
    """Registration page with passkey-first onboarding and invite prefill."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Create Account - Curatio</title>
    {_COMMON_STYLES}
    {_REGISTER_SCRIPTS}
</head>
<body>
    {_COMMON_HEADER}
    <div class="container" role="main">
        <h1>Create Account</h1>
        <p class="subtitle">Use your invite link and create a passkey. Every Curatio account is secured with a passkey, with an authenticator app as the backup — a password on its own is never enough to sign in.</p>

        <form id="registerForm">
            <div id="inviteGroup">
                <label for="invite">Invite Code</label>
                <input type="text" id="invite" name="invite" required autocomplete="off">
            </div>

            <label for="name">Display Name (optional)</label>
            <input type="text" id="name" name="name" autocomplete="name">

            <label for="email">Email</label>
            <input type="email" id="email" name="email" required autocomplete="email">

            <button type="button" id="passkeyBtn">Create Account with Passkey</button>

            <p class="auth-help">
                Your device will ask for your fingerprint, face, or screen lock. Nothing to remember, nothing to leak.
            </p>

            <div class="error" id="error"></div>
            <div class="success" id="success"></div>
        </form>

        <div class="footer">
            Already have an account? <a href="/account/login" class="link">Log in</a>
        </div>
    </div>

</body>
</html>""",
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    """Login page."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Log In - Curatio</title>
    {_COMMON_STYLES}
    {_LOGIN_SCRIPTS}
</head>
<body>
    {_COMMON_HEADER}
    <div class="container" role="main">
        <h1>Log In</h1>
        <p class="subtitle">Sign in with a passkey, or use password/2FA as a backup.</p>

        <form id="loginForm">
            <label for="email">Email <span class="subtitle">(optional for passkey)</span></label>
            <input type="email" id="email" name="email" autocomplete="email" placeholder="you@example.com">

            <button type="button" id="passkeyLoginBtn">Log In with Passkey</button>

            <details>
                <summary>Use password / 2FA backup instead</summary>

                <label for="password">Password</label>
                <input type="password" id="password" name="password" autocomplete="current-password">

                <div id="totpGroup" style="display:none;">
                    <label for="totp_code">2FA Code</label>
                    <input type="text" id="totp_code" name="totp_code" autocomplete="one-time-code"
                           pattern="[0-9]{{6}}" maxlength="6" placeholder="6-digit code">
                </div>

                <button type="submit" id="submitBtn">Log In with Password</button>
            </details>

            <div class="error" id="error"></div>
        </form>

        <div class="footer">
            Need an account? <a href="/account/register" class="link">Register</a>
        </div>
    </div>

</body>
</html>""",
    )


@router.get("/setup-passkey", response_class=HTMLResponse)
async def setup_passkey_page():
    """Blocking passkey enrollment page.

    Where the server sends a signed-in account that holds no passkey. Enrollment
    is the only way forward — there is deliberately no skip, because the
    requirement is enforced server-side and a client-side bypass would just
    bounce straight back here.
    """
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Set Up Your Passkey - Curatio</title>
    {_COMMON_STYLES}
    {_SETUP_PASSKEY_SCRIPTS}
</head>
<body>
    {_COMMON_HEADER}
    <div class="container" role="main">
        <h1>Set Up Your Passkey</h1>
        <p class="subtitle">Curatio requires a passkey on every account. Your device will ask for your fingerprint, face, or screen lock — there's nothing to remember and nothing to leak.</p>

        <button type="button" id="createBtn">Create Your Passkey</button>

        <div class="error" id="error"></div>
        <div class="success" id="success"></div>

        <p class="auth-help">
            After this you'll add an authenticator app as a backup, so you can still
            get in if this device isn't to hand.
        </p>

        <div class="footer">
            <a href="/auth/logout" class="link" id="signOutLink" data-curatio-logout>Sign out</a>
        </div>
    </div>

</body>
</html>""",
    )


@router.get("/setup-2fa", response_class=HTMLResponse)
async def setup_2fa_page():
    """2FA setup page — shows QR code for authenticator app."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Set Up 2FA - Curatio</title>
    {_COMMON_STYLES}
    {_SETUP_2FA_SCRIPTS}
</head>
<body>
    {_COMMON_HEADER}
    <div class="container" role="main">
        <h1>Set Up Two-Factor Authentication</h1>
        <p class="subtitle">Your backup for when your passkey device isn't to hand. Scan this QR code with an authenticator app.</p>

        <div class="qr-container" id="qrContainer">
            <p class="auth-help">Loading...</p>
        </div>

        <div id="secretDisplay" style="display:none;">
            <label>Manual entry key:</label>
            <div class="code-display" id="secretCode"></div>
        </div>

        <form id="verifyForm" style="display:none;">
            <label for="code">Enter the 6-digit code from your app to verify</label>
            <input type="text" id="code" name="code" pattern="[0-9]{{6}}" maxlength="6"
                   placeholder="000000" required autocomplete="one-time-code">

            <div class="error" id="error"></div>
            <div class="success" id="success"></div>

            <button type="submit" id="submitBtn">Verify & Enable 2FA</button>
        </form>
        <button type="button" id="skipBtn" style="display:none;">Skip for Now</button>
    </div>

</body>
</html>""",
    )


@router.get("/pair", response_class=HTMLResponse)
async def pair_page():
    """App pairing page — shows QR code and short code for the Curatio apps."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pair a TV Device - Curatio</title>
    {_COMMON_STYLES}
    {_DEVICE_CLAIM_SCRIPTS}
</head>
<body>
    {_COMMON_HEADER}
    <div class="container" role="main">
        <h1>Pair a TV Device</h1>
        <p class="subtitle">Enter the code shown on your Apple TV or other device.</p>

        <div class="device-claim-row">
            <input type="text" id="deviceCodeInput" maxlength="6" placeholder="ABC123" disabled
                   aria-label="Code shown on your device" inputmode="text" autocomplete="off">
            <button type="button" id="deviceClaimBtn" disabled
                    data-busy-label="Pairing..." data-success-message="Device paired successfully!"
                    data-invalid-message="Invalid or expired code.">Pair</button>
        </div>
        <div class="error" id="deviceError"></div>
        <div class="success" id="deviceSuccess" style="display:none;"></div>

        <div class="footer">
            <a href="/account/security" class="link">Security</a> · <a href="/auth/logout" class="link" data-curatio-logout>Log out</a>
        </div>
    </div>

</body>
</html>""",
    )


@router.get("/activate", response_class=HTMLResponse)
async def activate_page():
    """Device activation page — enter the code shown on your TV to pair it."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Activate Device - Curatio</title>
    {_COMMON_STYLES}
    {_DEVICE_CLAIM_SCRIPTS}
</head>
<body>
    {_COMMON_HEADER}
    <div class="container" role="main">
        <h1>Activate Your Device</h1>
        <p class="subtitle">Enter the code shown on your Apple TV to link it to your account.</p>

        <div class="device-claim-row">
            <input type="text" id="deviceCodeInput" maxlength="6" placeholder="ABC123" autofocus disabled
                   aria-label="Code shown on your Apple TV" inputmode="text" autocomplete="off">
            <button type="button" id="deviceClaimBtn" disabled
                    data-busy-label="Activating..." data-success-message="Device activated successfully! You can now use your TV app."
                    data-invalid-message="Invalid or expired code. Please try again.">Activate</button>
        </div>
        <div class="error" id="deviceError"></div>
        <div class="success" id="deviceSuccess" style="display:none;"></div>

        <div class="footer">
            <a href="/account" class="link">Back to Account</a>
        </div>
    </div>

</body>
</html>""",
    )


@router.get("/import", response_class=HTMLResponse)
async def import_page():
    """CSV import page for seeding recommendations from TMDB IDs."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Import Watch History - Curatio</title>
    {_COMMON_STYLES}
    {_IMPORT_SCRIPTS}
</head>
<body>
    {_COMMON_HEADER}
    <div class="container" role="main">
        <h1>Import Watch History</h1>
        <p class="subtitle">Paste TMDB IDs for series you've watched to improve your recommendations.</p>

        <label for="csvInput">TMDB IDs (one per line or comma-separated)</label>
        <textarea id="csvInput" placeholder="12345&#10;67890&#10;11111"></textarea>
        <div class="info">
            Only series (TV) TMDB IDs are supported. Imported items are used to build
            recommendations &mdash; they won't create a "Because You Watched" row.
            You can export these from another watch-history source or find them on themoviedb.org.
        </div>

        <div class="result" id="result"></div>

        <button type="button" class="btn" id="importBtn">Import</button>
    </div>

</body>
</html>""",
    )


@router.get("/security", response_class=HTMLResponse)
async def security_page():
    """Passkey recovery and management page."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Security - Curatio</title>
    {_COMMON_STYLES}
    {_SECURITY_SCRIPTS}
</head>
<body>
    {_COMMON_HEADER}
    <div class="container" role="main">
        <h1>Passkeys & Recovery</h1>
        <p class="subtitle">Add more than one passkey so you can still sign in if a device is lost. Keep password/TOTP as fallback during rollout.</p>
        <div id="passkeyList" class="passkey-list" role="status" aria-live="polite">Loading passkeys...</div>
        <button id="addPasskeyBtn">Add Another Passkey</button>
        <div class="error" id="error"></div>
        <div class="success" id="success"></div>
        <div class="footer"><a href="/account/pair" class="link">Pair a device</a></div>
    </div>
</body>
</html>""",
    )
