// break-pharma connect — pont OSPHARM (monde ISOLÉ).
//
// ospharm_main.js fait la collecte dans le contexte de la page (il a besoin de
// `webix`) ; ce fichier-ci a accès à `chrome.runtime` et fait le reste : décider
// quand collecter, afficher un retour discret, et transmettre au service worker.
//
// RÈGLE : ne JAMAIS renoncer en silence. La première version rendait la main sans
// rien dire quand l'extension n'était pas connectée ou quand le verrou 20 h était
// posé — sur DATASTAT, rien ne s'affichait et il était impossible de savoir si le
// script n'était pas injecté, s'il avait planté, ou s'il avait simplement décidé
// de ne rien faire. Chaque chemin affiche donc désormais une bulle, et trace son
// choix dans la console sous l'étiquette [bp-ospharm].

(() => {
  'use strict';

  const TAG      = '[bp-ospharm]';
  const LOCK_KEY = 'bp_osp_last_sync';
  const LOCK_MS  = 20 * 3600 * 1000;           // une collecte par 20 h, comme Digi
  // Marqueur posé par le service worker sur l'onglet qu'il ouvre lui-même
  // (query et non fragment : le fragment est réservé au routeur Webix).
  const isAuto = () => location.search.includes('bpsync');

  const log = (...a) => { try { console.log(TAG, ...a); } catch (_) {} };

  let busy = false;
  let pageReady = false;                       // ospharm_main.js a répondu

  // ── Bulle de retour ───────────────────────────────────────────────────────
  let bubble = null;
  // `sticky` : la bulle reste affichée (message qui appelle une action de
  // l'utilisateur). Sinon elle s'efface au bout de quelques secondes.
  function toast(text, tone, sticky) {
    if (!bubble) {
      bubble = document.createElement('div');
      bubble.style.cssText = [
        'position:fixed', 'z-index:2147483647', 'right:18px', 'bottom:18px',
        'max-width:320px', 'padding:12px 16px', 'border-radius:12px',
        'font:13px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif',
        'color:#fff', 'box-shadow:0 6px 24px rgba(0,0,0,.25)',
        'background:#0f766e', 'transition:opacity .3s', 'cursor:pointer',
      ].join(';');
      bubble.title = 'Cliquer pour (re)lancer la synchronisation break-pharma';
      // Un clic force toujours une collecte : c'est la porte de sortie quand le
      // verrou 20 h est posé ou qu'une tentative a échoué.
      bubble.addEventListener('click', () => { maybeRun(true); });
      document.documentElement.appendChild(bubble);
    }
    bubble.style.background = tone === 'err' ? '#b91c1c' : tone === 'ok' ? '#15803d' : '#0f766e';
    bubble.textContent = text;
    bubble.style.opacity = '1';
    clearTimeout(bubble._t);
    if (tone && !sticky) bubble._t = setTimeout(() => { bubble.style.opacity = '0'; }, 8000);
  }

  const ago = (ts) => {
    const h = Math.floor((Date.now() - ts) / 3600000);
    if (h < 1) return "il y a moins d'une heure";
    return 'il y a ' + h + ' h';
  };

  // ── Dialogue avec la page ─────────────────────────────────────────────────
  const post = (msg) => window.postMessage(Object.assign({ source: 'bp-ext' }, msg), '*');

  window.addEventListener('message', (e) => {
    if (e.source !== window) return;
    const d = e.data;
    if (!d || d.source !== 'bp-page') return;

    if (d.type === 'ready') { pageReady = true; return; }

    if (d.type === 'progress') {
      if (d.label) toast(`break-pharma · lecture ${d.label} (${d.done}/${d.total})`);
      return;
    }
    if (d.type === 'error') {
      busy = false;
      log('erreur du collecteur :', d.error);
      toast('break-pharma : ' + d.error, 'err', true);
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
    log('envoi de', total, 'lignes sur', months.length, 'mois');
    toast(`break-pharma · envoi de ${total} lignes…`);
    chrome.runtime.sendMessage({ type: 'bp-ospharm-import', months }, (rep) => {
      busy = false;
      if (!rep || !rep.ok) {
        const why = rep && rep.needLogin
          ? 'connectez-vous dans l’extension pour envoyer vos données'
          : ((rep && rep.error) || 'envoi impossible');
        log('envoi refusé :', why);
        toast('break-pharma : ' + why, 'err', true);
        finish();
        return;
      }
      try { chrome.storage.local.set({ [LOCK_KEY]: Date.now() }); } catch (_) {}
      const c = rep.cip || {};
      const reste = (c.ambigus || 0) + (c.echecs || 0);
      log('importé :', rep.lignes_importees, 'lignes');
      toast(`break-pharma ✓ ${rep.lignes_importees} lignes sur ${(rep.mois || []).length} mois`
            + (reste ? ` · ${reste} sans code produit` : ''), 'ok');
      finish();
    });
  }

  // ── Déclenchement ─────────────────────────────────────────────────────────
  // Le collecteur vit dans le MONDE PRINCIPAL de la page ; on ne peut pas lire
  // ses variables depuis ici. On le sonde donc jusqu'à ce qu'il réponde « ready ».
  async function waitPage(timeout) {
    const t0 = Date.now();
    while (!pageReady) {
      post({ cmd: 'ping' });
      if (Date.now() - t0 > timeout) return false;
      await new Promise((r) => setTimeout(r, 400));
    }
    return true;
  }

  async function maybeRun(force) {
    if (busy) {
      log('collecte déjà en cours');
      toast('break-pharma · lecture déjà en cours, laissez-la finir '
            + '(12 mois à parcourir).');
      return;
    }

    let s = {};
    try { s = await chrome.storage.local.get([LOCK_KEY, 'access_token']); } catch (_) {}

    if (!s.access_token) {
      log('pas de jeton break-pharma — synchro impossible');
      toast('break-pharma : ouvrez l’extension (icône en haut à droite) et '
            + 'connectez-vous pour synchroniser vos ventes OSPHARM.', 'err', true);
      return;
    }
    if (!force && s[LOCK_KEY] && Date.now() - s[LOCK_KEY] < LOCK_MS) {
      log('verrou 20 h actif, dernière synchro', new Date(s[LOCK_KEY]).toISOString());
      toast(`break-pharma · ventes déjà synchronisées ${ago(s[LOCK_KEY])}`
            + ' — cliquer pour relancer maintenant.');
      return;
    }

    busy = true;
    toast('break-pharma · préparation de la lecture de vos ventes…');

    if (!(await waitPage(15000))) {
      busy = false;
      log('le collecteur de page n’a jamais répondu');
      toast('break-pharma : le collecteur ne s’est pas chargé dans la page. '
            + 'Rechargez DATASTAT (Ctrl+Maj+R) ; si rien ne change, mettez '
            + 'votre navigateur à jour (Chrome 111 minimum).', 'err', true);
      return;
    }

    // Année N-1 : c'est la période que le comparateur analyse.
    const year = new Date().getFullYear() - 1;
    log('lancement de la collecte', year);
    post({ cmd: 'collect', year });
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.type === 'bp-osp-sync-now') {
      maybeRun(true);
      sendResponse({ ok: true });
      return true;
    }
    if (msg && msg.type === 'bp-osp-ping') {       // le popup vérifie l'injection
      sendResponse({ ok: true, busy });
      return true;
    }
  });

  log('pont chargé sur', location.href);
  // Laisse l'application Webix finir de se monter avant de solliciter la page.
  setTimeout(() => maybeRun(isAuto()), 4000);
})();
