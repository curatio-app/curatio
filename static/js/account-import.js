/* /account/import: seed recommendations from pasted TMDB series IDs. */
(() => {
  'use strict';

  async function doImport() {
    const btn = document.getElementById('importBtn');
    const result = document.getElementById('result');
    const csvText = document.getElementById('csvInput').value.trim();

    if (!csvText) {
      result.className = 'result error';
      result.textContent = 'Please paste some TMDB IDs first.';
      result.style.display = 'block';
      return;
    }

    btn.disabled = true;
    btn.textContent = 'Importing...';
    result.style.display = 'none';

    try {
      const resp = await fetch('/api/import-history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ csv_text: csvText }),
      });

      if (!resp.ok) {
        if (resp.status === 401) {
          location.href = '/account/login?next=' + encodeURIComponent(location.pathname);
          return;
        }
        const data = await resp.json();
        throw new Error(data.detail || 'Import failed');
      }

      const data = await resp.json();
      result.className = 'result success';
      result.textContent = `Imported ${data.imported} series` +
        (data.skipped > 0 ? ` (${data.skipped} already imported)` : '') +
        '. Your recommendations will update shortly.';
      result.style.display = 'block';
      document.getElementById('csvInput').value = '';
    } catch (e) {
      result.className = 'result error';
      result.textContent = e.message;
      result.style.display = 'block';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Import';
    }
  }

  document.getElementById('importBtn').addEventListener('click', doImport);
})();
