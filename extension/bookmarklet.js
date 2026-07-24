// break-pharma — bookmarklet mobile « un seul onglet » (Digi → break-pharma)
// ============================================================================
// Version MOBILE fiable. Contrairement à l'ancien bookmarklet (qui ouvrait un
// second onglet break-pharma pour l'envoi), celui-ci fait TOUT dans l'onglet
// Digipharmacie : il n'y a pas d'onglet d'arrière-plan que le mobile mettrait en
// veille au milieu de la synchro.
//
// Flux :
//   1. lit tes factures via l'API Digi DANS ta session (même origine, franchit
//      Cloudflare) ;
//   2. te connecte à break-pharma (GoTrue, CORS ouvert) — demande e-mail + mot de
//      passe break-pharma (RIEN n'est stocké : on redemande à chaque lancement, pour
//      ne pas laisser de jeton sur le site Digipharmacie) ;
//   3. POST direct des factures à l'API break-pharma (/import/digi-json). L'origine
//      Digipharmacie est autorisée côté serveur (CORS).
//
// La version « à coller » (javascript:…) est dans bookmarklet.url.txt.
// ============================================================================
(function () {
  var SUPA = 'https://api.break-pharma.fr';
  var ANON = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYW5vbiIsImlzcyI6InN1cGFiYXNlLXNlbGYiLCJpYXQiOjE3ODM1NDU0MjV9.Ga5ubKMU5mnlcBncdb1TUgprBHxuDkRw0LBmGP81XwM';
  var API  = 'https://pharmacie-remises.onrender.com';

  // Fournisseurs pertinents (labos génériqueurs + dépositaires + répartiteurs).
  var GEN = ['biogaran','teva','mylan','viatris','zydus','sandoz','zentiva','arrow',
    'cristers','eg labo','eg labs','evolupharm','ranbaxy','ratiopharm','actavis',
    'hexal','aurobindo','intas','sun pharma','pharmaki','strides','qualimed','almus',
    'ibigen','substipharm','medipha','phlorogine','alloga','cegedim','movianto',
    'cerp','ocp','alliance','phoenix','cooperation pharmaceutique','cooperation pharma',
    'csp','centre specialites pharmaceutiques'];
  function nrm(s) {
    return ' ' + (s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '')
      .replace(/[^a-z0-9]+/g, ' ').trim() + ' ';
  }
  var GN = GEN.map(nrm);
  function isGen(t) { var n = nrm(t); return GN.some(function (k) { return n.indexOf(k) >= 0; }); }
  function ck(n) { var m = document.cookie.match('(?:^|; )' + n + '=([^;]*)'); return m ? decodeURIComponent(m[1]) : ''; }

  function pdf(inv) {
    if (!inv || typeof inv !== 'object') return '';
    var ks = ['file','file_url','pdf','pdf_url','document','document_url','url','download_url','href'];
    for (var i = 0; i < ks.length; i++) { if (typeof inv[ks[i]] === 'string' && inv[ks[i]]) return inv[ks[i]]; }
    for (var k in inv) {
      var v = inv[k];
      if (typeof v === 'string' && /^https?:\/\/\S+/.test(v) && /\.pdf(\?|$)|\/media\/|\/documents?\//i.test(v)) return v;
    }
    return '';
  }
  function looks(r) {
    return r.some(function (inv) {
      return inv && typeof inv === 'object' &&
        (pdf(inv) || inv.billing_date || inv.provider_ref || inv.provider_name || inv.invoice_date);
    });
  }
  function disc() {
    var s = {};
    try {
      performance.getEntriesByType('resource').forEach(function (e) {
        try {
          var u = new URL(e.name);
          if (u.origin !== location.origin) return;
          var sg = u.pathname.split('/').filter(Boolean);
          var i = sg.findIndex(function (x) { return /^(invoices?|factures?|bills?)$/i.test(x); });
          if (i >= 0) s[u.origin + '/' + sg.slice(0, i + 1).join('/') + '/'] = 1;
        } catch (_) {}
      });
    } catch (_) {}
    return Object.keys(s);
  }
  function fj(url) {
    var csrf = ck('csrftoken');
    var h = { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' };
    if (csrf) h['X-CSRFToken'] = csrf;
    return fetch(url, { credentials: 'include', headers: h }).then(function (r) {
      if (!r.ok) throw new Error('http ' + r.status);
      return r.json();
    });
  }
  function collect(data, out) {
    var res = Array.isArray(data) ? data : (data.results || []);
    res.forEach(function (inv) {
      var f = pdf(inv); if (!f) return;
      var pr = inv.provider_ref || inv.provider || inv.supplier_ref || '';
      var pn = inv.provider_name || inv.supplier_name || inv.supplier || '';
      if (!isGen(pr + ' ' + pn)) return;
      out.push({ file: f, provider_ref: pr, provider_name: pn,
        billing_date: inv.billing_date || inv.date || inv.created_at || inv.invoice_date || '' });
    });
    return (data && !Array.isArray(data) && data.next) ? data.next : null;
  }

  // Connexion break-pharma via l'API Render (qui autorise l'origine Digi en CORS et
  // relaie GoTrue). Rien n'est stocké : on redemande à chaque lancement.
  async function bpLogin() {
    var email = prompt('E-mail break-pharma :'); if (!email) return null;
    var pass  = prompt('Mot de passe break-pharma :'); if (!pass) return null;
    try {
      var r = await fetch(API + '/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email, password: pass }) });
      var d = await r.json().catch(function () { return {}; });
      if (!r.ok || !d.access_token) { alert('Connexion break-pharma refusée : ' + (d.detail || d.error_description || d.msg || ('HTTP ' + r.status))); return null; }
      return d.access_token;
    } catch (e) { alert('Connexion break-pharma impossible : ' + e.message); return null; }
  }

  // Envoi d'un lot de factures à l'API break-pharma (origine Digi autorisée par CORS).
  async function push(token, invoices) {
    var r = await fetch(API + '/import/digi-json', {
      method: 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ invoices: invoices }) });
    if (r.status === 401) throw new Error('session');
    if (!r.ok) throw new Error('api ' + r.status);
    var d = await r.json(); return d.queued || 0;
  }

  // Lit page par page ET envoie au fur et à mesure (INCRÉMENTAL) : même si tu quittes
  // la page en cours de route, ce qui est déjà lu est déjà parti (le serveur dédoublonne).
  async function run() {
    var q = '?ordering=-billing_date&page_size=100&page=1';
    var cands = disc().concat(['/invoices/', '/api/v1/invoices/']).map(function (b) { return b + q; });
    var token = null, queued = 0, seenEndpoint = false;
    for (var i = 0; i < cands.length; i++) {
      var d; try { d = await fj(cands[i]); } catch (_) { continue; }
      var res = Array.isArray(d) ? d : (d.results || []);
      if (!looks(res)) continue;
      seenEndpoint = true;
      // On a trouvé l'API factures → se connecter à break-pharma une seule fois.
      token = await bpLogin(); if (!token) return;
      var data = d, g = 0;
      while (data && g < 500) {
        g++;
        var out = []; var next = collect(data, out);
        if (out.length) {
          try { queued += await push(token, out); }
          catch (e) {
            if (e.message === 'session') { alert('Session break-pharma expirée — relance.'); return; }
            alert('Erreur d’envoi : ' + e.message); return;
          }
        }
        if (!next) break;
        try { data = await fj(next); } catch (_) { break; }
      }
      alert('✓ Terminé — ' + queued + ' facture(s) envoyée(s) à break-pharma.\n(Le serveur télécharge et analyse en fond ; relance « ré-analyser » dans break-pharma si besoin.)');
      return;
    }
    if (!seenEndpoint) alert('Ouvre d’abord ta page « Factures » sur Digipharmacie, puis relance.');
  }

  run();
})();
