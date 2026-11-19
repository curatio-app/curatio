/* /account/setup-passkey: blocking passkey enrollment. Needs auth-common.js. */
(() => {
  'use strict';
  const A = window.CuratioAuth;
  const destination = A.nextParam('/account/dashboard');

  function continueAfterEnrollment(data) {
    if (data && data.auth_requirement === 'totp_required') {
      location.href = '/account/setup-2fa?next=' + encodeURIComponent(destination);
      return;
    }
    location.href = destination;
  }

  async function createPasskey() {
    const btn = document.getElementById('createBtn');
    const error = document.getElementById('error');
    const success = document.getElementById('success');
    error.style.display = 'none'; success.style.display = 'none';
    if (!window.PublicKeyCredential) {
      // No self-service bypass here: the capability check is reported by
      // the client, so trusting it would hand anyone a way past the gate.
      error.textContent = 'This browser does not support passkeys. Open Curatio on a device that does — a phone, or an up-to-date desktop browser — or ask an administrator for a temporary exemption.';
      error.style.display = 'block';
      btn.disabled = true;
      return;
    }
    btn.disabled = true; btn.textContent = 'Creating passkey...';
    try {
      const beginResp = await fetch('/auth/passkeys/register/begin', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nickname: 'Passkey' }),
      });
      const begin = await beginResp.json();
      if (beginResp.status === 401) {
        location.href = A.loginUrlForHere();
        return;
      }
      if (!beginResp.ok) throw new Error(begin.detail || 'Unable to start passkey setup');
      const credential = await navigator.credentials.create({ publicKey: A.prepareCreationOptions(begin.options) });
      const finishResp = await fetch('/auth/passkeys/register/finish', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: begin.token, credential: A.serializeCredential(credential), nickname: 'Passkey' }),
      });
      const finish = await finishResp.json();
      if (!finishResp.ok) throw new Error(finish.detail || 'Unable to save passkey');
      success.textContent = 'Passkey saved! Continuing...';
      success.style.display = 'block';
      setTimeout(() => continueAfterEnrollment(finish), 1200);
    } catch (err) {
      error.textContent = err.message || 'Passkey setup failed. Please try again.';
      error.style.display = 'block';
      btn.disabled = false; btn.textContent = 'Create Your Passkey';
    }
  }

  document.getElementById('createBtn').addEventListener('click', createPasskey);
})();
