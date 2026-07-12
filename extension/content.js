// break-pharma connect — injecté sur app.digipharmacie.fr.
// Ajoute un bouton flottant qui lit la liste des factures/avoirs via l'API Digi
// DANS LA SESSION de l'utilisateur (cookies inclus → contourne l'anti-bot), puis
// l'envoie au service worker qui la transmet à break-pharma.

(function () {
  if (window.__bpConnectInjected) return;
  window.__bpConnectInjected = true;

  // Fournisseurs pertinents : labos génériqueurs + dépositaires (facturent au nom
  // du labo) + répartiteurs. On ne remonte QUE ces factures (comme le scraper
  // serveur) au lieu de tout envoyer en brut. Liste = api_scraper/scraper.py.
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
  // Normalise (minuscules, sans accents, ponctuation/underscores → espaces) et
  // matche par MOT entier → « Centre_Specialites_Pharmaceutiques » == « csp »/…,
  // sans faux positifs sur des sous-chaînes.
  function _norm(s) {
    return ' ' + (s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '')
      .replace(/[^a-z0-9]+/g, ' ').trim() + ' ';
  }
  const _GEN_NORM = GENERIC_LABS.map(_norm);
  function _isGenericProvider(txt) {
    const n = _norm(txt);
    return _GEN_NORM.some(k => n.includes(k));
  }

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
    maxWidth: '360px', maxHeight: '50vh', overflowY: 'auto', display: 'none',
    background: '#04060f', color: '#e6f7ff',
    border: '1px solid #0369a1', borderRadius: '10px', padding: '10px 12px',
    font: '13px system-ui,sans-serif', lineHeight: '1.45', whiteSpace: 'pre-wrap',
    wordBreak: 'break-word', userSelect: 'text',
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
          if (u.origin !== location.origin) return;
          const segs = u.pathname.split('/').filter(Boolean);
          // Segment EXACTEMENT « invoices/facture/bill » (± s) : on TRONQUE juste
          // après → la LISTE (/…/invoices/), pas une action (/invoices/spend_by_month/).
          const i = segs.findIndex(s => /^(invoices?|factures?|bills?)$/i.test(s));
          if (i >= 0) set.add(u.origin + '/' + segs.slice(0, i + 1).join('/') + '/');
        } catch (_) {}
      });
    } catch (_) {}
    return [...set];
  }
  // Une réponse « liste de factures » a des lignes avec un PDF ou des champs
  // typiques (date/fournisseur) — sinon c'est un agrégat (spend_by_month…).
  function _looksLikeInvoices(results) {
    return results.some(inv => inv && typeof inv === 'object' &&
      (_pdfUrl(inv) || inv.billing_date || inv.provider_ref || inv.provider_name || inv.invoice_date));
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

  // Pagine une réponse JSON de liste (walk sur `next`) et extrait les factures.
  async function _paginate(firstData, onProgress) {
    const out = [];
    const sampleProviders = [];
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
        const pref = inv.provider_ref  || inv.provider || inv.supplier_ref || '';
        const pnam = inv.provider_name || inv.supplier_name || inv.supplier || '';
        if (sampleProviders.length < 6 && (pref || pnam)) sampleProviders.push((pref + ' ' + pnam).trim());
        // On ne garde QUE les fournisseurs pertinents (labos/dépositaires/répartiteurs).
        if (!_isGenericProvider(pref + ' ' + pnam)) continue;
        out.push({
          file, provider_ref: pref, provider_name: pnam,
          billing_date: inv.billing_date || inv.date || inv.created_at || inv.invoice_date || '',
        });
      }
      onProgress(out.length);
      const next = (!Array.isArray(data) && data.next) ? data.next : null;
      if (!next) break;
      data = await _fetchJson(next);
    }
    return { invoices: out, rawCount, withPdf, sampleKeys, sampleProviders };
  }

  async function fetchAllInvoices(onProgress) {
    const q = '?ordering=-billing_date&page_size=100&page=1';
    // Candidats : endpoints détectés (tronqués à la liste) + défauts connus.
    const candidates = [...new Set([..._discoverEndpoints(), '/invoices/', '/api/v1/invoices/'])].map(b => b + q);
    let lastErr = null, best = null;
    for (const cand of candidates) {
      let d;
      try { d = await _fetchJson(cand); } catch (e) { lastErr = e; continue; }
      const results = Array.isArray(d) ? d : (d.results || []);
      // Bon endpoint = celui dont les lignes ressemblent à des factures. Sinon
      // (agrégat type spend_by_month) on passe au candidat suivant.
      if (_looksLikeInvoices(results)) {
        const r = await _paginate(d, onProgress);
        return Object.assign(r, { endpoint: cand });
      }
      if (!best || results.length > best.rawCount) {
        best = { invoices: [], rawCount: results.length,
                 sampleKeys: results[0] ? Object.keys(results[0]) : null, endpoint: cand };
      }
    }
    if (best) return best;  // rien de convaincant → diagnostic (voir bouton)
    if (lastErr && lastErr.kind === 'auth') throw new Error('Session Digipharmacie expirée — reconnectez-vous à Digipharmacie, puis réessayez.');
    if (lastErr && lastErr.kind === 'html') throw new Error("Impossible de lire vos factures. Ouvrez d'abord votre page « Factures » sur Digipharmacie (menu Factures), laissez-la s'afficher, puis re-cliquez ce bouton.");
    throw new Error('Digipharmacie a répondu de façon inattendue' + (lastErr && lastErr.status ? ' (' + lastErr.status + ')' : ''));
  }
  // Cherche l'URL du PDF dans une facture, quel que soit le nom du champ.
  function _pdfUrl(inv) {
    if (!inv || typeof inv !== 'object') return '';
    for (const k of ['file', 'file_url', 'pdf', 'pdf_url', 'document', 'document_url', 'url', 'download_url', 'href']) {
      if (typeof inv[k] === 'string' && inv[k]) return inv[k];
    }
    // Sinon : première valeur ressemblant à une URL de PDF/média.
    for (const v of Object.values(inv)) {
      if (typeof v === 'string' && /^https?:\/\/\S+/.test(v) && /\.pdf(\?|$)|\/media\/|\/documents?\//i.test(v)) return v;
    }
    return '';
  }

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const label = btn.textContent;
    try {
      log('Lecture des factures Digipharmacie…', 'info');
      const r = await fetchAllInvoices((n) => log('Lecture… ' + n + ' facture(s)', 'info'));
      const invoices = r.invoices;
      if (!invoices.length) {
        const ep = (r.endpoint || '').replace(/^https?:\/\/[^/]+/, '').split('?')[0];
        if (r.withPdf > 0) {
          // Des PDF existent mais aucun fournisseur reconnu comme labo/répartiteur.
          log('DIAG : ' + r.withPdf + ' facture(s) avec PDF sur ' + ep + ', mais aucun fournisseur '
            + 'reconnu (labo/répartiteur). Exemples de fournisseurs : '
            + ((r.sampleProviders || []).join(' | ') || '?')
            + '. Copiez ce message et envoyez-le au support break-pharma.', 'warn');
        } else if (r.rawCount > 0) {
          log('DIAG : ' + r.rawCount + ' ligne(s) lues sur ' + ep + ' mais aucun lien PDF reconnu. '
            + 'Champs disponibles : ' + (r.sampleKeys ? r.sampleKeys.join(', ') : '?')
            + '. Copiez ce message et envoyez-le au support break-pharma.', 'warn');
        } else {
          log('DIAG : aucune facture lue (endpoint ' + (ep || 'non trouvé') + '). '
            + 'Ouvrez d’abord votre page « Factures » sur Digipharmacie, laissez-la s’afficher, puis re-cliquez.', 'warn');
        }
        return;
      }
      log(invoices.length + ' facture(s) labo/répartiteur retenue(s) sur ' + (r.withPdf || invoices.length) + ' avec PDF.', 'info');

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
