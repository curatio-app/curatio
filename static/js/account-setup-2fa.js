/* /account/setup-2fa: authenticator enrollment. Needs auth-common.js. */
(() => {
  'use strict';
  const A = window.CuratioAuth;
  const destination = A.nextParam('/account/dashboard');

  document.getElementById('skipBtn').addEventListener('click', () => { location.href = destination; });

  // Skipping is only on the table when the server isn't actually blocking on
  // 2FA — i.e. an admin has granted this account an enrollment exemption.
  async function offerSkipIfOptional() {
    try {
      const resp = await fetch('/auth/me');
      if (!resp.ok) return;
      const me = await resp.json();
      if (me.auth_requirement !== 'totp_required') {
        document.getElementById('skipBtn').style.display = 'block';
      }
    } catch (err) { /* leave the skip button hidden */ }
  }

  function showQrError(message) {
    const notice = document.createElement('p');
    notice.className = 'error';
    notice.textContent = message;
    document.getElementById('qrContainer').replaceChildren(notice);
  }

  async function setup() {
    try {
      const resp = await fetch('/auth/setup-2fa', { method: 'POST' });
      if (!resp.ok) {
        if (resp.status === 401) { location.href = A.loginUrlForHere(); return; }
        const data = await resp.json();
        showQrError(data.detail || 'Failed to set up 2FA');
        return;
      }
      const data = await resp.json();
      const qr = document.createElement('img');
      qr.src = data.qr_data_url;
      qr.alt = 'QR code for your authenticator app';
      qr.width = 200;
      qr.height = 200;
      document.getElementById('qrContainer').replaceChildren(qr);
      document.getElementById('secretCode').textContent = data.secret;
      document.getElementById('secretDisplay').style.display = 'block';
      document.getElementById('verifyForm').style.display = 'block';
    } catch (err) {
      showQrError('Network error. Please refresh.');
    }
  }

  document.getElementById('verifyForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('submitBtn');
    const error = document.getElementById('error');
    const success = document.getElementById('success');
    error.style.display = 'none';
    btn.disabled = true;

    try {
      const resp = await fetch('/auth/confirm-2fa', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: document.getElementById('code').value }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        error.textContent = data.detail || 'Verification failed';
        error.style.display = 'block';
        btn.disabled = false;
        return;
      }
      success.textContent = '2FA enabled! Continuing...';
      success.style.display = 'block';
      setTimeout(() => { location.href = destination; }, 1500);
    } catch (err) {
      error.textContent = 'Network error.';
      error.style.display = 'block';
      btn.disabled = false;
    }
  });

  offerSkipIfOptional();
  setup();
})();
