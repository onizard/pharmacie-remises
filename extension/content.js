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
  function _cookie(name) {
    const m = document.cookie.match('(?:^|; )' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[1]) : '';
  }
  // Découvre le VRAI endpoint des factures : le SPA Digi l'a déjà appelé quand
  // l'utilisateur a ouvert sa page « Factures » → il est dans les ressources
  // réseau (Performance API). Évite un chemin codé en dur qui renvoie du HTML.
  function _discoverEndpoints() {
    const set = new Set();
    try {
      performance.getEntriesByType('resource').forEach(e => {
        try {
          const u = new URL(e.name);
          if (u.origin === location.origin && /\/(invoice|facture|bill)s?\//i.test(u.pathname)) {
            set.add(u.origin + u.pathname);
          }
        } catch (_) {}
      });
    } catch (_) {}
    return [...set];
  }
  // fetch JSON avec en-têtes attendus par Django REST (CSRF + XHR). Distingue
  // page HTML (session/anti-bot) d'une vraie réponse JSON.
  async function _fetchJson(url) {
    const csrf = _cookie('csrftoken');
    const res = await fetch(url, {
      credentials: 'include',
      headers: Object.assign(
        { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        csrf ? { 'X-CSRFToken': csrf } : {},
      ),
    });
    if (res.status === 401 || res.status === 403) { const e = new Error('auth'); e.kind = 'auth'; throw e; }
    if (!res.ok) { const e = new Error('http ' + res.status); e.kind = 'http'; e.status = res.status; throw e; }
    const ct = res.headers.get('content-type') || '';
    if (!ct.includes('json')) {
      const txt = await res.text();
      if (/^\s*</.test(txt)) { const e = new Error('html'); e.kind = 'html'; throw e; }
      try { return JSON.parse(txt); } catch (_) { const e = new Error('html'); e.kind = 'html'; throw e; }
    }
    return res.json();
  }

  async function fetchAllInvoices(onProgress) {
    const q = '?ordering=-billing_date&page_size=100&page=1';
    // Endpoints candidats : ceux détectés dans le trafic (prioritaires) + le défaut.
    const candidates = [..._discoverEndpoints().map(b => b + q), '/api/v1/invoices/' + q];
    let data = null, lastErr = null;
    for (const cand of candidates) {
      try { data = await _fetchJson(cand); break; } catch (e) { lastErr = e; }
    }
    if (!data) {
      if (lastErr && lastErr.kind === 'auth') throw new Error('Session Digipharmacie expirée — reconnectez-vous à Digipharmacie, puis réessayez.');
      if (lastErr && lastErr.kind === 'html') throw new Error("Impossible de lire vos factures. Ouvrez d'abord votre page « Factures » sur Digipharmacie (menu Factures), laissez-la s'afficher, puis re-cliquez ce bouton.");
      throw new Error('Digipharmacie a répondu de façon inattendue' + (lastErr && lastErr.status ? ' (' + lastErr.status + ')' : ''));
    }
    const out = [];
    let guard = 0;
    while (data && guard < 500) {
      guard++;
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
      const next = (!Array.isArray(data) && data.next) ? data.next : null;
      if (!next) break;
      data = await _fetchJson(next);
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
