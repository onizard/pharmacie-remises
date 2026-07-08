// break-pharma connect — injecté sur app.digipharmacie.fr.
// Ajoute un bouton flottant qui lit la liste des factures/avoirs via l'API Digi
// DANS LA SESSION de l'utilisateur (cookies inclus → contourne l'anti-bot), puis
// l'envoie au service worker qui la transmet à break-pharma.

(function () {
  if (window.__bpConnectInjected) return;
  window.__bpConnectInjected = true;

  // ── UI : bouton + panneau de statut ────────────────────────────────────────
  const btn = document.createElement('button');
  btn.textContent = '⇪ Envoyer à break-pharma';
  Object.assign(btn.style, {
    position: 'fixed', right: '18px', bottom: '18px', zIndex: 2147483647,
    background: '#0369a1', color: '#fff', border: 'none', borderRadius: '10px',
    padding: '12px 16px', font: '600 14px system-ui,sans-serif', cursor: 'pointer',
    boxShadow: '0 4px 14px rgba(0,0,0,.35)',
  });

  const panel = document.createElement('div');
  Object.assign(panel.style, {
    position: 'fixed', right: '18px', bottom: '64px', zIndex: 2147483647,
    maxWidth: '320px', display: 'none', background: '#04060f', color: '#e6f7ff',
    border: '1px solid #0369a1', borderRadius: '10px', padding: '10px 12px',
    font: '13px system-ui,sans-serif', lineHeight: '1.4',
    boxShadow: '0 4px 14px rgba(0,0,0,.35)',
  });

  document.documentElement.appendChild(btn);
  document.documentElement.appendChild(panel);

  const COLORS = { info: '#e6f7ff', ok: '#00ff88', warn: '#ffab00', error: '#ff3366' };
  function log(msg, kind) {
    panel.style.display = 'block';
    panel.style.borderColor = COLORS[kind] || '#0369a1';
    panel.style.color = COLORS[kind] || '#e6f7ff';
    panel.textContent = msg;
  }

  // ── Lecture paginée des factures Digi ───────────────────────────────────────
  async function fetchAllInvoices(onProgress) {
    const out = [];
    let url = '/api/v1/invoices/?ordering=-billing_date&page_size=100&page=1';
    let guard = 0;
    while (url && guard < 500) {
      guard++;
      const res = await fetch(url, { credentials: 'include', headers: { Accept: 'application/json' } });
      if (res.status === 401 || res.status === 403) {
        throw new Error('Session Digipharmacie expirée — reconnectez-vous à Digipharmacie, puis réessayez.');
      }
      if (!res.ok) throw new Error('Digipharmacie a répondu ' + res.status);
      const data = await res.json();
      const results = Array.isArray(data) ? data : (data.results || []);
      for (const inv of results) {
        const file = inv.file || inv.file_url || '';
        if (!file) continue;
        out.push({
          file,
          provider_ref:  inv.provider_ref  || '',
          provider_name: inv.provider_name || '',
          billing_date:  inv.billing_date  || '',
        });
      }
      onProgress(out.length);
      // `next` peut être une URL absolue (http…) — fetch l'accepte telle quelle.
      url = (!Array.isArray(data) && data.next) ? data.next : null;
    }
    return out;
  }

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const label = btn.textContent;
    try {
      log('Lecture des factures Digipharmacie…', 'info');
      const invoices = await fetchAllInvoices((n) => log('Lecture… ' + n + ' facture(s)', 'info'));
      if (!invoices.length) { log('Aucune facture trouvée sur votre compte Digipharmacie.', 'warn'); return; }

      log('Envoi de ' + invoices.length + ' facture(s) à break-pharma…', 'info');
      const resp = await chrome.runtime.sendMessage({ type: 'bp-import', invoices });

      if (!resp || !resp.ok) {
        if (resp && resp.needLogin) {
          log('Connectez-vous d’abord à break-pharma : cliquez sur l’icône de l’extension (en haut à droite), puis réessayez.', 'warn');
        } else {
          log('Échec de l’envoi : ' + ((resp && resp.error) || 'erreur inconnue'), 'error');
        }
        return;
      }
      log('✓ ' + resp.queued + ' facture(s) mises en file d’attente. Le traitement continue côté serveur : '
        + 'vous pouvez fermer cet onglet, vos remises se mettront à jour sur break-pharma.fr.', 'ok');
    } catch (e) {
      log('Erreur : ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
})();
