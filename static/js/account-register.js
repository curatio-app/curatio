/* /account/register: passkey-first account creation. Needs auth-common.js. */
(() => {
  'use strict';
  const A = window.CuratioAuth;
  const params = new URLSearchParams(location.search);
  const inviteParam = params.get('invite') || '';
  if (inviteParam) {
    document.getElementById('invite').value = inviteParam;
    document.getElementById('inviteGroup').style.display = 'none';
  }

  async function createWithPasskey() {
    const btn = document.getElementById('passkeyBtn');
    const error = document.getElementById('error');
    const success = document.getElementById('success');
    error.style.display = 'none'; success.style.display = 'none';
    if (!window.PublicKeyCredential) {
      error.textContent = 'This browser does not support passkeys. Open Curatio on a device that does — a phone, or an up-to-date desktop browser — to create your account.';
      error.style.display = 'block';
      return;
    }
    btn.disabled = true; btn.textContent = 'Creating passkey...';
    try {
      const beginResp = await fetch('/auth/passkeys/register/begin', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          invite: document.getElementById('invite').value,
          email: document.getElementById('email').value,
          name: document.getElementById('name').value,
        }),
      });
      const begin = await beginResp.json();
      if (!beginResp.ok) throw new Error(begin.detail || 'Passkey setup failed');
      const credential = await navigator.credentials.create({ publicKey: A.prepareCreationOptions(begin.options) });
      const finishResp = await fetch('/auth/passkeys/register/finish', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: begin.token, credential: A.serializeCredential(credential), nickname: 'First passkey' }),
      });
      const finish = await finishResp.json();
      if (!finishResp.ok) throw new Error(finish.detail || 'Passkey verification failed');
      success.textContent = 'Account created! Pair your device next.';
      success.style.display = 'block';
      A.redirectAfterAuth(finish, '/account/pair');
    } catch (err) {
      error.textContent = err.message || 'Passkey setup failed. Please try again.';
      error.style.display = 'block';
      btn.disabled = false; btn.textContent = 'Create Account with Passkey';
    }
  }

  document.getElementById('passkeyBtn').addEventListener('click', createWithPasskey);
  // Enter in any field starts passkey creation rather than reloading the page.
  document.getElementById('registerForm').addEventListener('submit', (e) => {
    e.preventDefault();
    createWithPasskey();
  });
})();
