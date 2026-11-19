/* /account/login: passkey first, password / 2FA as the fallback. Needs auth-common.js. */
(() => {
  'use strict';
  const A = window.CuratioAuth;
  const form = document.getElementById('loginForm');
  const totpGroup = document.getElementById('totpGroup');

  function redirectAfterLogin(data) {
    A.redirectAfterAuth(data, A.nextParam('/account/dashboard'));
  }

  document.getElementById('passkeyLoginBtn').addEventListener('click', async () => {
    const btn = document.getElementById('passkeyLoginBtn');
    const error = document.getElementById('error');
    error.style.display = 'none';
    if (!window.PublicKeyCredential) {
      error.textContent = 'This browser does not support passkeys. Use the password option below.';
      error.style.display = 'block';
      document.querySelector('details').open = true;
      return;
    }
    btn.disabled = true; btn.textContent = 'Checking passkey...';
    try {
      const beginResp = await fetch('/auth/passkeys/login/begin', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: document.getElementById('email').value.trim() }),
      });
      const begin = await beginResp.json();
      if (!beginResp.ok) throw new Error(begin.detail || 'Passkey login failed');
      const credential = await navigator.credentials.get({ publicKey: A.prepareRequestOptions(begin.options) });
      const finishResp = await fetch('/auth/passkeys/login/finish', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: begin.token, credential: A.serializeAssertion(credential) }),
      });
      const finish = await finishResp.json();
      if (!finishResp.ok) throw new Error(finish.detail || 'Passkey verification failed');
      redirectAfterLogin(finish);
    } catch (err) {
      error.textContent = (err.message || 'Passkey login failed.') + ' Try the password option below.';
      error.style.display = 'block';
      document.querySelector('details').open = true;
      btn.disabled = false; btn.textContent = 'Log In with Passkey';
    }
  });

  document.getElementById('email').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      document.getElementById('passkeyLoginBtn').click();
    }
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('submitBtn');
    const error = document.getElementById('error');
    const idleLabel = 'Log In with Password';
    error.style.display = 'none';
    btn.disabled = true;
    btn.textContent = 'Logging in...';

    try {
      const resp = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: document.getElementById('email').value,
          password: document.getElementById('password').value,
          totp_code: document.getElementById('totp_code').value || '',
        }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        if (data.detail === '2FA code required') {
          totpGroup.style.display = 'block';
          document.getElementById('totp_code').focus();
          error.textContent = 'Enter your 2FA code from your authenticator app.';
        } else {
          error.textContent = data.detail || 'Login failed';
        }
        error.style.display = 'block';
        btn.disabled = false;
        btn.textContent = idleLabel;
        return;
      }
      redirectAfterLogin(data);
    } catch (err) {
      error.textContent = 'Network error. Please try again.';
      error.style.display = 'block';
      btn.disabled = false;
      btn.textContent = idleLabel;
    }
  });
})();
