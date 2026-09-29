// break-pharma connect — pont OSPHARM (monde ISOLÉ).
//
// ospharm_main.js fait la collecte dans le contexte de la page (il a besoin de
// `webix`) ; ce fichier-ci a accès à `chrome.runtime` et fait le reste : décider
// quand collecter, afficher un retour discret, et transmettre au service worker.

(() => {
  'use strict';

  const LOCK_KEY = 'bp_osp_last_sync';
  const LOCK_MS  = 20 * 3600 * 1000;           // une collecte par 20 h, comme Digi
  // Marqueur posé par le service worker sur l'onglet qu'il ouvre lui-même
  // (query et non fragment : le fragment est réservé au routeur Webix).
  const isAuto = () => location.search.includes('bpsync');

  let busy = false;

  // ── Bulle de retour ───────────────────────────────────────────────────────
  let bubble = null;
  function toast(text, tone) {
    if (!bubble) {
      bubble = document.createElement('div');
      bubble.style.cssText = [
        'position:fixed', 'z-index:2147483647', 'right:18px', 'bottom:18px',
        'max-width:320px', 'padding:12px 16px', 'border-radius:12px',
        'font:13px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif',
        'color:#fff', 'box-shadow:0 6px 24px rgba(0,0,0,.25)',
        'background:#0f766e', 'transition:opacity .3s',
      ].join(';');
      document.documentElement.appendChild(bubble);
    }
    bubble.style.background = tone === 'err' ? '#b91c1c' : tone === 'ok' ? '#15803d' : '#0f766e';
    bubble.textContent = text;
    bubble.style.opacity = '1';
    clearTimeout(bubble._t);
    if (tone) bubble._t = setTimeout(() => { bubble.style.opacity = '0'; }, 6000);
  }

  // ── Dialogue avec la page ─────────────────────────────────────────────────
  function ask(year) {
    window.postMessage({ source: 'bp-ext', cmd: 'collect', year }, '*');
  }

  window.addEventListener('message', (e) => {
    if (e.source !== window) return;
    const d = e.data;
    if (!d || d.source !== 'bp-page') return;

    if (d.type === 'progress') {
      if (d.label) toast(`break-pharma · lecture ${d.label} (${d.done}/${d.total})`);
      return;
    }
    if (d.type === 'error') {
      busy = false;
      toast('break-pharma : ' + d.error, 'err');
      finish();
      return;
    }
    if (d.type === 'result') {
      send(d.months, d.total);
    }
  });

  function finish() {
    // Onglet ouvert par la synchro automatique : on demande sa fermeture au
    // service worker (lui seul peut fermer un onglet).
    if (isAuto()) {
      try { chrome.runtime.sendMessage({ type: 'bp-sync-done' }); } catch (_) {}
    }
  }

  function send(months, total) {
    toast(`break-pharma · envoi de ${total} lignes…`);
    chrome.runtime.sendMessage({ type: 'bp-ospharm-import', months }, (rep) => {
      busy = false;
      if (!rep || !rep.ok) {
        if (rep && rep.needLogin) {
          toast('break-pharma : connectez-vous dans l’extension pour envoyer vos données', 'err');
        } else {
          toast('break-pharma : ' + ((rep && rep.error) || 'envoi impossible'), 'err');
        }
        finish();
        return;
      }
      try { chrome.storage.local.set({ [LOCK_KEY]: Date.now() }); } catch (_) {}
      const c = rep.cip || {};
      const reste = (c.ambigus || 0) + (c.echecs || 0);
      toast(`break-pharma ✓ ${rep.lignes_importees} lignes sur ${(rep.mois || []).length} mois`
            + (reste ? ` · ${reste} sans code produit` : ''), 'ok');
      finish();
    });
  }

  // ── Déclenchement ─────────────────────────────────────────────────────────
  async function maybeRun(force) {
    if (busy) return;
    const s = await chrome.storage.local.get([LOCK_KEY, 'access_token']);
    if (!s.access_token) return;                        // pas connecté à break-pharma
    if (!force && s[LOCK_KEY] && Date.now() - s[LOCK_KEY] < LOCK_MS) return;
    busy = true;
    // Année N-1 : c'est la période que le comparateur analyse.
    ask(new Date().getFullYear() - 1);
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.type === 'bp-osp-sync-now') {
      maybeRun(true);
      sendResponse({ ok: true });
      return true;
    }
  });

  // Laisse l'application Webix finir de se monter avant de solliciter la page.
  setTimeout(() => maybeRun(isAuto()), 4000);
})();
