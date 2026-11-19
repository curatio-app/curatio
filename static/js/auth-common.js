/* Shared helpers for the server-rendered account pages (window.CuratioAuth). */
(() => {
  'use strict';

  // Only same-origin paths survive; '/\evil.com', '//evil.com' and absolute
  // URLs to other hosts fall back. The URL parser does the normalising, so this
  // matches what the browser would actually navigate to.
  function safeNext(n, fallback = '/') {
    if (!n) return fallback;
    try {
      const u = new URL(n, location.origin);
      return u.origin === location.origin ? u.pathname + u.search + u.hash : fallback;
    } catch {
      return fallback;
    }
  }

  function nextParam(fallback) {
    return safeNext(new URLSearchParams(location.search).get('next') || '', fallback);
  }

  function loginUrlForHere() {
    return '/account/login?next=' + encodeURIComponent(location.pathname + location.search);
  }

  function bufferToBase64url(buffer) {
    return btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
  }

  function base64urlToBuffer(value) {
    const padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4);
    const binary = atob(padded);
    return Uint8Array.from(binary, c => c.charCodeAt(0));
  }

  function prepareCreationOptions(options) {
    options.challenge = base64urlToBuffer(options.challenge);
    options.user.id = base64urlToBuffer(options.user.id);
    if (options.excludeCredentials) {
      options.excludeCredentials = options.excludeCredentials.map(c => ({ ...c, id: base64urlToBuffer(c.id) }));
    }
    return options;
  }

  function prepareRequestOptions(options) {
    options.challenge = base64urlToBuffer(options.challenge);
    if (options.allowCredentials) {
      options.allowCredentials = options.allowCredentials.map(c => ({ ...c, id: base64urlToBuffer(c.id) }));
    }
    return options;
  }

  function serializeCredential(cred) {
    return {
      id: cred.id,
      rawId: bufferToBase64url(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bufferToBase64url(cred.response.clientDataJSON),
        attestationObject: bufferToBase64url(cred.response.attestationObject),
        transports: cred.response.getTransports ? cred.response.getTransports() : [],
      },
    };
  }

  function serializeAssertion(cred) {
    return {
      id: cred.id,
      rawId: bufferToBase64url(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bufferToBase64url(cred.response.clientDataJSON),
        authenticatorData: bufferToBase64url(cred.response.authenticatorData),
        signature: bufferToBase64url(cred.response.signature),
        userHandle: cred.response.userHandle ? bufferToBase64url(cred.response.userHandle) : null,
      },
    };
  }

  // Routes a fresh sign-in through any outstanding enrollment step.
  function redirectAfterAuth(data, destination) {
    const requirement = data && (data.auth_requirement || data.totp_requirement);
    if (requirement === 'passkey_required') {
      location.href = '/account/setup-passkey?next=' + encodeURIComponent(destination);
    } else if (requirement === 'totp_required') {
      location.href = '/account/setup-2fa?next=' + encodeURIComponent(destination);
    } else {
      location.href = destination;
    }
  }

  // Sign-out links: <a href="/auth/logout" data-curatio-logout>. The endpoint is
  // POST-only, so the click posts and then lands on the login page.
  document.querySelectorAll('[data-curatio-logout]').forEach((link) => {
    link.addEventListener('click', async (e) => {
      e.preventDefault();
      try { await fetch('/auth/logout', { method: 'POST' }); } catch (err) { /* fall through */ }
      location.href = '/account/login';
    });
  });

  window.CuratioAuth = Object.freeze({
    safeNext,
    nextParam,
    loginUrlForHere,
    bufferToBase64url,
    base64urlToBuffer,
    prepareCreationOptions,
    prepareRequestOptions,
    serializeCredential,
    serializeAssertion,
    redirectAfterAuth,
  });
})();
