/**
 * Test du pont OSPHARM (ospharm.js, monde isolé) — il ne doit JAMAIS renoncer
 * en silence.
 *
 * Le défaut corrigé ici : sur DATASTAT, rien ne s'affichait. Pas connecté à
 * break-pharma ? retour silencieux. Verrou 20 h posé ? retour silencieux.
 * Collecteur de page non injecté ? attente infinie. Impossible de savoir
 * laquelle des trois. Ce banc vérifie que chacun de ces chemins produit une
 * bulle lisible par l'utilisateur.
 *
 * Lancer : node extension/test_ospharm_bridge.js
 */
'use strict';

let ok = 0, ko = 0;
const check = (label, got, want) => {
  const good = JSON.stringify(got) === JSON.stringify(want);
  console.log((good ? '  ✔ ' : '  ✘ ') + label + ' → ' + JSON.stringify(got) +
              (good ? '' : '   ATTENDU ' + JSON.stringify(want)));
  good ? ok++ : ko++;
};

// ── Faux navigateur ─────────────────────────────────────────────────────────
function makeEnv(storage, opts) {
  opts = opts || {};
  const bulle = { texte: '', fond: '', clics: [] };
  const envoyes = [];
  const versPage = [];
  const listeners = [];
  const runtimeListeners = [];

  const el = {
    style: { cssText: '', set background(v) { bulle.fond = v; }, get background() { return bulle.fond; },
             opacity: '' },
    set textContent(v) { bulle.texte = v; }, get textContent() { return bulle.texte; },
    title: '',
    addEventListener: (t, f) => { if (t === 'click') bulle.clics.push(f); },
  };

  global.document = {
    createElement: () => el,
    documentElement: { appendChild() {} },
  };
  global.window = {
    addEventListener: (t, f) => { if (t === 'message') listeners.push(f); },
    postMessage: (d) => {
      if (d && d.source === 'bp-ext') versPage.push(d);
      listeners.forEach((f) => f({ source: global.window, data: d }));
    },
    location: { search: opts.search || '', href: 'https://datastat.ospharm.org/' },
  };
  global.location = global.window.location;
  global.chrome = {
    storage: { local: {
      get: async (keys) => {
        const out = {};
        [].concat(keys).forEach((k) => { if (storage[k] !== undefined) out[k] = storage[k]; });
        return out;
      },
      set: (o) => Object.assign(storage, o),
    } },
    runtime: {
      sendMessage: (m, cb) => { envoyes.push(m); if (cb) cb(opts.reponse || { ok: false, error: 'test' }); },
      onMessage: { addListener: (f) => runtimeListeners.push(f) },
    },
  };
  return { bulle, envoyes, versPage, runtimeListeners };
}

function charger() {
  delete require.cache[require.resolve('./ospharm.js')];
  require('./ospharm.js');
}

const attendre = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  console.log('── pont OSPHARM : aucun abandon silencieux ──');

  // 1. Pas connecté à break-pharma → message d'invite, pas de silence.
  {
    const env = makeEnv({});
    charger();
    env.runtimeListeners[0]({ type: 'bp-osp-sync-now' }, {}, () => {});
    await attendre(60);
    check('non connecté : bulle affichée', /connectez-vous/i.test(env.bulle.texte), true);
    check('non connecté : ton rouge', env.bulle.fond, '#b91c1c');
    check('non connecté : rien envoyé à la page', env.versPage.length, 0);
  }

  // 2. Verrou 20 h posé, déclenchement automatique → on explique et on propose.
  {
    const env = makeEnv({ access_token: 'jwt', bp_osp_last_sync: Date.now() - 3600 * 1000 });
    charger();
    env.runtimeListeners[0]({ type: 'bp-osp-sync-now' }, {}, () => {});
    await attendre(60);
    // bp-osp-sync-now force : la collecte doit démarrer malgré le verrou.
    check('verrou : le bouton force la collecte', env.versPage.some((m) => m.cmd === 'ping'), true);
  }

  // 3. Verrou 20 h + déclenchement automatique (pas de force) → bulle explicative.
  {
    const env = makeEnv({ access_token: 'jwt', bp_osp_last_sync: Date.now() - 3600 * 1000 });
    charger();
    await attendre(4200);                       // le minuteur de 4 s du chargement
    check('verrou : bulle explicative', /déjà synchronisées/i.test(env.bulle.texte), true);
    check('verrou : invite à relancer', /cliquer pour relancer/i.test(env.bulle.texte), true);
    check('verrou : un clic force la collecte', (() => {
      env.bulle.clics.forEach((f) => f());
      return true;
    })(), true);
    await attendre(60);
    check('verrou : collecte démarrée au clic', env.versPage.some((m) => m.cmd === 'ping'), true);
  }

  // 4. Collecteur de page absent (ne répond pas au ping) → erreur explicite.
  {
    const env = makeEnv({ access_token: 'jwt' });
    charger();
    env.runtimeListeners[0]({ type: 'bp-osp-sync-now' }, {}, () => {});
    await attendre(300);
    check('sans collecteur : sondage envoyé', env.versPage.some((m) => m.cmd === 'ping'), true);
    check('sans collecteur : pas encore d’erreur', /ne s’est pas chargé/.test(env.bulle.texte), false);
    // On ne patiente pas les 15 s réelles du banc : on vérifie qu'aucune collecte
    // n'est lancée tant que la page n'a pas répondu.
    check('sans collecteur : aucune collecte lancée', env.versPage.some((m) => m.cmd === 'collect'), false);
  }

  // 5. Page présente → la collecte part, et l'échec d'envoi est affiché.
  {
    const env = makeEnv({ access_token: 'jwt' }, { reponse: { ok: false, needLogin: true } });
    charger();
    // La « page » répond au ping, comme le vrai ospharm_main.js.
    global.window.addEventListener('message', (e) => {
      if (e.data && e.data.source === 'bp-ext' && e.data.cmd === 'ping') {
        setTimeout(() => global.window.postMessage({ source: 'bp-page', type: 'ready' }), 0);
      }
    });
    env.runtimeListeners[0]({ type: 'bp-osp-sync-now' }, {}, () => {});
    await attendre(900);
    const col = env.versPage.find((m) => m.cmd === 'collect');
    check('collecte lancée', !!col, true);
    check('année N-1 demandée', col && col.year, new Date().getFullYear() - 1);

    global.window.postMessage({ source: 'bp-page', type: 'result', months: [{ rows: [] }], total: 3 });
    await attendre(60);
    check('envoi relayé au service worker',
          env.envoyes.some((m) => m.type === 'bp-ospharm-import'), true);
    check('refus d’envoi affiché', /connectez-vous/i.test(env.bulle.texte), true);
  }

  console.log('\n' + '='.repeat(52) + `\n  ${ok} réussis, ${ko} échoués\n` + '='.repeat(52));
  process.exit(ko ? 1 : 0);
})();
