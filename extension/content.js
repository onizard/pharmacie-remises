// break-pharma connect — injecté sur app.digipharmacie.fr.
// SYNCHRONISATION AUTOMATIQUE : dès que l'utilisateur est connecté à Digi (et à
// break-pharma via le popup de l'extension), les nouvelles factures labo sont
// envoyées automatiquement (au plus 1×/~20 h), sans bouton ni action manuelle.
// Un « Synchroniser maintenant » reste dispo dans le popup.

(function () {
  if (window.__bpConnectInjected) return;
  window.__bpConnectInjected = true;

  const SYNC_MIN_INTERVAL = 20 * 3600 * 1000;   // ~20 h entre deux synchros auto

  // Fournisseurs pertinents : labos génériqueurs + dépositaires + répartiteurs.
  const GENERIC_LABS = [
    'biogaran', 'teva', 'mylan', 'viatris', 'zydus', 'sandoz', 'zentiva',
    'arrow', 'cristers', 'eg labo', 'eg labs', 'evolupharm',
    'ranbaxy', 'ratiopharm', 'actavis', 'hexal', 'aurobindo', 'intas',
    'sun pharma', 'pharmaki', 'strides', 'qualimed', 'almus', 'ibigen',
    'substipharm', 'medipha', 'phlorogine',
    'alloga', 'cegedim', 'movianto',
    'cerp', 'ocp', 'alliance', 'phoenix',
    'cooperation pharmaceutique', 'cooperation pharma', 'csp',
    'centre specialites pharmaceutiques', 'centre spécialités pharmaceutiques',
  ];
  function _norm(s) {
    return ' ' + (s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '')
      .replace(/[^a-z0-9]+/g, ' ').trim() + ' ';
  }
  const _GEN_NORM = GENERIC_LABS.map(_norm);
  function _isGenericProvider(txt) { const n = _norm(txt); return _GEN_NORM.some(k => n.includes(k)); }

  // ── Bulle de statut (transitoire, auto-masquée) — pas de bouton permanent ────
  const panel = document.createElement('div');
  Object.assign(panel.style, {
    position: 'fixed', right: '18px', bottom: '18px', zIndex: 2147483647,
    maxWidth: '340px', maxHeight: '50vh', overflowY: 'auto', display: 'none',
    background: '#04060f', color: '#e6f7ff', border: '1px solid #0369a1',
    borderRadius: '10px', padding: '10px 12px',
    font: '13px system-ui,sans-serif', lineHeight: '1.45', whiteSpace: 'pre-wrap',
    wordBreak: 'break-word', userSelect: 'text', boxShadow: '0 4px 14px rgba(0,0,0,.35)',
  });
  document.documentElement.appendChild(panel);
  const COLORS = { info: '#e6f7ff', ok: '#00ff88', warn: '#ffab00', error: '#ff3366' };
  let _hideT = null;
  function status(msg, kind, autohideMs) {
    panel.style.display = 'block';
    panel.style.borderColor = COLORS[kind] || '#0369a1';
    panel.style.color = COLORS[kind] || '#e6f7ff';
    panel.textContent = msg;
    if (_hideT) clearTimeout(_hideT);
    if (autohideMs) _hideT = setTimeout(() => { panel.style.display = 'none'; }, autohideMs);
  }

  // ── Lecture des factures (API Digi, dans la session) ─────────────────────────
  function _cookie(name) {
    const m = document.cookie.match('(?:^|; )' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[1]) : '';
  }
  function _discoverEndpoints() {
    const set = new Set();
    try {
      performance.getEntriesByType('resource').forEach(e => {
        try {
          const u = new URL(e.name);
          if (u.origin !== location.origin) return;
          const segs = u.pathname.split('/').filter(Boolean);
          const i = segs.findIndex(s => /^(invoices?|factures?|bills?)$/i.test(s));
          if (i >= 0) set.add(u.origin + '/' + segs.slice(0, i + 1).join('/') + '/');
        } catch (_) {}
      });
    } catch (_) {}
    return [...set];
  }
  function _pdfUrl(inv) {
    if (!inv || typeof inv !== 'object') return '';
    for (const k of ['file', 'file_url', 'pdf', 'pdf_url', 'document', 'document_url', 'url', 'download_url', 'href']) {
      if (typeof inv[k] === 'string' && inv[k]) return inv[k];
    }
    for (const v of Object.values(inv)) {
      if (typeof v === 'string' && /^https?:\/\/\S+/.test(v) && /\.pdf(\?|$)|\/media\/|\/documents?\//i.test(v)) return v;
    }
    return '';
  }
  function _looksLikeInvoices(results) {
    return results.some(inv => inv && typeof inv === 'object' &&
      (_pdfUrl(inv) || inv.billing_date || inv.provider_ref || inv.provider_name || inv.invoice_date));
  }
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
  // full=false : 1re page seulement (synchro quotidienne des récentes ; le serveur
  // dédoublonne). full=true : tout l'historique (synchro manuelle complète).
  async function _paginate(firstData, onProgress, full) {
    const out = [], sampleProviders = [];
    let data = firstData, guard = 0, rawCount = 0, withPdf = 0, sampleKeys = null;
    while (data && guard < 500) {
      guard++;
      const results = Array.isArray(data) ? data : (data.results || []);
      for (const inv of results) {
        rawCount++;
        if (!sampleKeys && inv && typeof inv === 'object') sampleKeys = Object.keys(inv);
        const file = _pdfUrl(inv);
        if (!file) continue;
        withPdf++;
        const pref = inv.provider_ref || inv.provider || inv.supplier_ref || '';
        const pnam = inv.provider_name || inv.supplier_name || inv.supplier || '';
        if (sampleProviders.length < 6 && (pref || pnam)) sampleProviders.push((pref + ' ' + pnam).trim());
        if (!_isGenericProvider(pref + ' ' + pnam)) continue;
        out.push({ file, provider_ref: pref, provider_name: pnam,
          billing_date: inv.billing_date || inv.date || inv.created_at || inv.invoice_date || '' });
      }
      if (onProgress) onProgress(out.length);
      const next = (!full) ? null : ((!Array.isArray(data) && data.next) ? data.next : null);
      if (!next) break;
      data = await _fetchJson(next);
    }
    return { invoices: out, rawCount, withPdf, sampleKeys, sampleProviders };
  }
  async function fetchInvoices(onProgress, full) {
    const q = '?ordering=-billing_date&page_size=100&page=1';
    const candidates = [...new Set([..._discoverEndpoints(), '/invoices/', '/api/v1/invoices/'])].map(b => b + q);
    let lastErr = null, best = null;
    for (const cand of candidates) {
      let d;
      try { d = await _fetchJson(cand); } catch (e) { lastErr = e; continue; }
      const results = Array.isArray(d) ? d : (d.results || []);
      if (_looksLikeInvoices(results)) {
        const r = await _paginate(d, onProgress, full);
        return Object.assign(r, { endpoint: cand });
      }
      if (!best || results.length > best.rawCount) {
        best = { invoices: [], rawCount: results.length,
                 sampleKeys: results[0] ? Object.keys(results[0]) : null, endpoint: cand, sampleProviders: [] };
      }
    }
    if (best) return best;
    if (lastErr && lastErr.kind === 'auth') { const e = new Error('auth'); e.kind = 'auth'; throw e; }
    if (lastErr && lastErr.kind === 'html') { const e = new Error('html'); e.kind = 'html'; throw e; }
    throw new Error('inattendu' + (lastErr && lastErr.status ? ' (' + lastErr.status + ')' : ''));
  }

  // ── Cœur : une synchronisation ───────────────────────────────────────────────
  async function _markSynced() { try { await chrome.storage.local.set({ bp_last_sync: Date.now() }); } catch (_) {} }

  async function runSync({ full = false, manual = false } = {}) {
    if (window.__bpSyncing) return;
    window.__bpSyncing = true;
    try {
      let st = null;
      try { st = await chrome.runtime.sendMessage({ type: 'bp-status' }); } catch (_) {}
      if (!st || !st.loggedIn) {
        if (manual) status('Connectez-vous d’abord à break-pharma via l’icône de l’extension.', 'warn', 7000);
        return { ok: false, needLogin: true };
      }
      if (manual) status('Synchronisation des factures…', 'info');
      let r;
      try {
        r = await fetchInvoices(n => { if (manual && n) status('Lecture… ' + n + ' facture(s)', 'info'); }, full);
      } catch (e) {
        if (e.kind === 'auth') { if (manual) status('Session Digipharmacie expirée — reconnectez-vous à Digipharmacie.', 'warn', 8000); return { ok: false }; }
        if (e.kind === 'html') { if (manual) status('Ouvrez d’abord votre page « Factures » sur Digipharmacie, puis réessayez.', 'warn', 9000); return { ok: false }; }
        if (manual) status('Erreur de lecture : ' + e.message, 'error', 8000);
        return { ok: false };
      }
      if (!r.invoices.length) {
        await _markSynced();   // évite de re-scanner en boucle
        if (manual) {
          const ep = (r.endpoint || '').replace(/^https?:\/\/[^/]+/, '').split('?')[0];
          if (r.withPdf > 0) status('Aucune facture labo à synchroniser (' + r.withPdf + ' PDF vus, fournisseurs : ' + ((r.sampleProviders || []).join(' | ') || '?') + ').', 'warn', 10000);
          else status('Aucune facture trouvée sur ' + (ep || '?') + '. Ouvrez votre page « Factures » puis réessayez.', 'warn', 9000);
        }
        return { ok: true, queued: 0 };
      }
      const resp = await chrome.runtime.sendMessage({ type: 'bp-import', invoices: r.invoices });
      if (resp && resp.ok) {
        await _markSynced();
        status('✓ break-pharma : ' + resp.queued + ' facture(s) synchronisée(s).', 'ok', 6000);
        return { ok: true, queued: resp.queued };
      }
      if (resp && resp.needLogin) { if (manual) status('Connectez-vous à break-pharma via l’icône de l’extension.', 'warn', 7000); return { ok: false, needLogin: true }; }
      if (manual) status('Échec de l’envoi : ' + ((resp && resp.error) || 'erreur inconnue'), 'error', 8000);
      return { ok: false };
    } finally {
      window.__bpSyncing = false;
    }
  }

  // Synchro auto si l'intervalle est écoulé (silencieuse).
  async function _autoSyncIfDue() {
    try {
      const { bp_last_sync } = await chrome.storage.local.get('bp_last_sync');
      if (Date.now() - (bp_last_sync || 0) < SYNC_MIN_INTERVAL) return;
      runSync({ full: false, manual: false });
    } catch (_) {}
  }

  // Déclencheur manuel depuis le popup.
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.type === 'bp-sync-now') {
      runSync({ full: !!msg.full, manual: true }).then(r => sendResponse(r || { ok: false }));
      return true;   // réponse asynchrone
    }
  });

  // Au chargement d'une page Digi (utilisateur connecté) : tentative auto.
  _autoSyncIfDue();
  // Onglets laissés ouverts longtemps : re-vérifie périodiquement.
  setInterval(_autoSyncIfDue, 6 * 3600 * 1000);
})();
