// break-pharma — bookmarklet mobile « Digi → break-pharma »
// ============================================================================
// Équivalent de l'extension navigateur, mais utilisable sur SMARTPHONE (iPhone
// comme Android), où les extensions n'existent pas.
//
// Fonctionnement : lancé (tapé) pendant que tu es sur app.digipharmacie.fr et
// connecté, il lit tes factures labo DANS ta session Digi (même origine — le seul
// contexte qui franchit Cloudflare), ouvre un onglet break-pharma.fr#digi-import
// et lui transmet la liste par postMessage. C'est break-pharma (origine autorisée
// par le CORS de l'API) qui envoie ensuite à l'API, avec TA session break-pharma.
//
// On NE poste PAS directement vers break-pharma depuis la page Digi : ce serait
// bloqué (CORS + CSP). D'où le relais par onglet.
//
// La version « à coller » (javascript:…) est dans bookmarklet.url.txt (générée
// depuis ce fichier). Ce .js est la SOURCE lisible, pour maintenance.
// ============================================================================
(function () {
  var BP = 'https://break-pharma.fr';
  // Ouvrir l'onglet break-pharma DANS le geste utilisateur (sinon pop-up bloquée).
  var W = window.open(BP + '/#digi-import', '_blank');
  if (!W) { alert('Autorise les pop-ups pour break-pharma, puis relance.'); return; }

  // Fournisseurs pertinents (labos génériqueurs + dépositaires + répartiteurs).
  var GEN = ['biogaran','teva','mylan','viatris','zydus','sandoz','zentiva','arrow',
    'cristers','eg labo','eg labs','evolupharm','ranbaxy','ratiopharm','actavis',
    'hexal','aurobindo','intas','sun pharma','pharmaki','strides','qualimed','almus',
    'ibigen','substipharm','medipha','phlorogine','alloga','cegedim','movianto',
    'cerp','ocp','alliance','phoenix','cooperation pharmaceutique','cooperation pharma',
    'csp','centre specialites pharmaceutiques'];
  function nrm(s) {
    return ' ' + (s || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '')
      .replace(/[^a-z0-9]+/g, ' ').trim() + ' ';
  }
  var GN = GEN.map(nrm);
  function isGen(t) { var n = nrm(t); return GN.some(function (k) { return n.indexOf(k) >= 0; }); }
  function ck(n) { var m = document.cookie.match('(?:^|; )' + n + '=([^;]*)'); return m ? decodeURIComponent(m[1]) : ''; }

  // URL du PDF (signée) dans un objet facture, tous formats de champ confondus.
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
  // Devine l'endpoint « …/invoices/ » depuis les requêtes déjà faites par la page.
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
  async function read() {
    var q = '?ordering=-billing_date&page_size=100&page=1';
    var cands = disc().concat(['/invoices/', '/api/v1/invoices/']).map(function (b) { return b + q; });
    for (var i = 0; i < cands.length; i++) {
      var d; try { d = await fj(cands[i]); } catch (_) { continue; }
      var res = Array.isArray(d) ? d : (d.results || []);
      if (!looks(res)) continue;
      var out = [], g = 0, next = collect(d, out);
      while (next && g < 500) { g++; var dd; try { dd = await fj(next); } catch (_) { break; } next = collect(dd, out); }
      return out;
    }
    return null;   // aucun endpoint factures reconnu
  }

  // Poignée de main avec l'onglet break-pharma : on n'envoie qu'une fois qu'il est
  // prêt (bp-digi-ready) ET que la lecture est terminée.
  var invs = null, ready = false;
  function trySend() { if (ready && invs) W.postMessage({ type: 'bp-digi-invoices', invoices: invs }, BP); }
  window.addEventListener('message', function (ev) {
    if (ev.origin !== BP) return;
    if (ev.data && ev.data.type === 'bp-digi-ready') { ready = true; trySend(); }
    if (ev.data && ev.data.type === 'bp-digi-result') {
      var r = ev.data;
      alert(r.ok ? ('✓ ' + r.queued + ' facture(s) envoyee(s) a break-pharma.')
        : (r.needLogin ? 'Connecte-toi a break-pharma dans l\'onglet ouvert, puis relance.'
                       : 'Echec : ' + (r.error || '?')));
    }
  });
  read().then(function (o) {
    if (o === null) { alert('Ouvre d\'abord ta page « Factures » sur Digipharmacie, puis relance.'); return; }
    if (!o.length) { alert('Aucune facture labo a envoyer.'); return; }
    invs = o; trySend();
  });
})();
