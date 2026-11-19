/*
 * /account/pair and /account/activate: claim a device short code. Needs
 * auth-common.js. Page wording comes from data-* attributes on #deviceClaimBtn:
 * data-busy-label, data-success-message, data-invalid-message.
 */
(() => {
  'use strict';
  const A = window.CuratioAuth;
  const input = document.getElementById('deviceCodeInput');
  const btn = document.getElementById('deviceClaimBtn');
  const error = document.getElementById('deviceError');
  const success = document.getElementById('deviceSuccess');
  const idleLabel = btn.textContent;
  const busyLabel = btn.dataset.busyLabel || 'Pairing...';
  const successMessage = btn.dataset.successMessage || 'Device paired successfully!';
  const invalidMessage = btn.dataset.invalidMessage || 'Invalid or expired code.';

  function showError(message) {
    error.textContent = message;
    error.style.display = 'block';
  }

  async function requireSignedIn() {
    try {
      const resp = await fetch('/auth/me', { headers: { 'Accept': 'application/json' } });
      if (resp.status === 401) {
        location.href = A.loginUrlForHere();
        return false;
      }
      if (!resp.ok) {
        showError('Unable to verify your session. Please refresh and try again.');
        return false;
      }
      input.disabled = false;
      btn.disabled = false;
      return true;
    } catch (err) {
      showError('Network error. Please refresh and try again.');
      return false;
    }
  }

  async function claimDevice() {
    error.style.display = 'none';
    success.style.display = 'none';

    const code = input.value.trim().toUpperCase();
    if (!code || code.length < 4) {
      showError('Please enter the code shown on your device.');
      return;
    }

    btn.disabled = true;
    btn.textContent = busyLabel;

    try {
      const resp = await fetch('/auth/device/claim', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ short_code: code }),
      });
      if (!resp.ok) {
        if (resp.status === 401) { location.href = A.loginUrlForHere(); return; }
        const data = await resp.json();
        showError(data.detail || invalidMessage);
        return;
      }
      success.textContent = successMessage;
      success.style.display = 'block';
      input.value = '';
    } catch (err) {
      showError('Network error. Please try again.');
    } finally {
      btn.disabled = false;
      btn.textContent = idleLabel;
    }
  }

  btn.addEventListener('click', claimDevice);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') claimDevice();
  });
  requireSignedIn();
})();
