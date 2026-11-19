/* /account/security: list, add and remove passkeys. Needs auth-common.js. */
(() => {
  'use strict';
  const A = window.CuratioAuth;

  async function loadPasskeys() {
    const list = document.getElementById('passkeyList');
    try {
      const resp = await fetch('/auth/passkeys');
      if (resp.status === 401) { location.href = '/account/login?next=/account/security'; return; }
      if (!resp.ok) throw new Error('Unable to load passkeys. Refresh to try again.');
      const data = await resp.json();
      list.className = 'passkey-list';
      list.replaceChildren();
      if (!data.passkeys.length) {
        const empty = document.createElement('p');
        empty.className = 'empty-state';
        empty.textContent = 'No passkeys yet. Add at least one passkey.';
        list.appendChild(empty);
        return;
      }
      data.passkeys.forEach(passkey => {
        const item = document.createElement('div');
        item.className = 'passkey-item';
        const details = document.createElement('div');
        const name = document.createElement('strong');
        name.textContent = passkey.nickname || 'Passkey';
        const created = document.createElement('small');
        created.textContent = 'Created ' + new Date(passkey.created_at).toLocaleString();
        details.append(name, created);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = 'Remove';
        remove.setAttribute('aria-label', 'Remove ' + name.textContent);
        remove.addEventListener('click', () => deletePasskey(passkey.id));
        item.append(details, remove);
        list.appendChild(item);
      });
    } catch (error) {
      list.textContent = error.message || 'Unable to load passkeys. Refresh to try again.';
      list.className = 'passkey-list empty-state';
    }
  }

  async function deletePasskey(id) {
    if (!confirm('Remove this passkey?')) return;
    const resp = await fetch('/auth/passkeys/' + encodeURIComponent(id), { method: 'DELETE' });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) alert(data.detail || 'Unable to remove passkey');
    loadPasskeys();
  }

  document.getElementById('addPasskeyBtn').addEventListener('click', async () => {
    const error = document.getElementById('error');
    const success = document.getElementById('success');
    error.style.display = 'none'; success.style.display = 'none';
    try {
      const beginResp = await fetch('/auth/passkeys/register/begin', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ nickname: 'Passkey' }) });
      const begin = await beginResp.json();
      if (!beginResp.ok) throw new Error(begin.detail || 'Unable to start passkey setup');
      const credential = await navigator.credentials.create({ publicKey: A.prepareCreationOptions(begin.options) });
      const finishResp = await fetch('/auth/passkeys/register/finish', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: begin.token, credential: A.serializeCredential(credential), nickname: 'Additional passkey' }) });
      const finish = await finishResp.json();
      if (!finishResp.ok) throw new Error(finish.detail || 'Unable to save passkey');
      success.textContent = 'Passkey added.'; success.style.display = 'block'; loadPasskeys();
    } catch (err) { error.textContent = err.message || 'Passkey setup failed'; error.style.display = 'block'; }
  });

  loadPasskeys();
})();
