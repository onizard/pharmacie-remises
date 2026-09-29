/**
 * Test du collecteur OSPHARM (ospharm_main.js) contre un faux DATASTAT.
 *
 * On ne peut pas éprouver le vrai site depuis l'intégration continue : ce banc
 * reproduit ce qui compte — l'arbre de menu Webix, le sélecteur de dates, l'en-tête
 * de période, et une table dont le contenu DÉPEND du mois sélectionné. Il vérifie
 * donc que le collecteur navigue, règle réellement chaque période, et lit la bonne
 * table — ce qu'aucune relecture de code ne garantit.
 *
 * Lancer : node extension/test_ospharm_main.js
 */
'use strict';

let ok = 0, ko = 0;
const check = (label, got, want) => {
  const good = JSON.stringify(got) === JSON.stringify(want);
  console.log((good ? '  ✔ ' : '  ✘ ') + label + ' → ' + JSON.stringify(got) +
              (good ? '' : '   ATTENDU ' + JSON.stringify(want)));
  good ? ok++ : ko++;
};

// ── Faux DATASTAT ───────────────────────────────────────────────────────────
const two = (n) => String(n).padStart(2, '0');
let periode = '01/08/26 à 31/08/26';        // période par défaut, comme le vrai site
let onglet  = 'PAR LABORATOIRE';
let pending = {};
const vues  = { generic: false };

// Lignes par mois : janvier en a 2, les autres 1 — de quoi vérifier que chaque
// période est bien appliquée AVANT lecture, et non lue douze fois d'affilée.
function rowsFor() {
  const m = periode.slice(3, 5);
  const n = m === '01' ? 2 : 1;
  return Array.from({ length: n }, (_, i) => ({
    product_name: 'MOLECULE ' + m + ' ' + i, package: '30.0',
    manufacturer_name: 'BIOGARAN', sellin_price_ht: '1.5',
    quantity: '2', product_discount: '10.0', sum_sellin_ht: '2.7',
  }));
}

class FakeEl {
  constructor(attrs, text) { this.attrs = attrs || {}; this.textContent = text || ''; this._h = []; }
  getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
  getBoundingClientRect() { return { width: 100, height: 20 }; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  addEventListener(t, f) { this._h.push([t, f]); }
  dispatchEvent(ev) {
    if (ev.type !== 'click') return true;
    const id = this.attrs.webix_tm_id;
    if (id === '/generic') vues.genericOpen = true;
    if (id === 'generic.sellout') vues.generic = true;
    if (/pr[ée]sentation/i.test(this.textContent)) onglet = 'PAR PRESENTATION GENERIQUE';
    if (/valider/i.test(this.textContent) && pending.s && pending.e) {
      const f = (d) => two(d.getDate()) + '/' + two(d.getMonth() + 1) + '/' + two(d.getFullYear() % 100);
      periode = f(pending.s) + ' à ' + f(pending.e);
      pending = {};
    }
    return true;
  }
}

const elements = [
  new FakeEl({ webix_tm_id: '/generic', class: 'webix_tree_item' }, 'Audit génériques'),
  new FakeEl({ webix_tm_id: 'generic.sellout', class: 'webix_tree_item' }, 'Mes ventes'),
  new FakeEl({ view_id: 'button_date_picker' }, ''),
  new FakeEl({ class: 'webix_item_tab' }, 'PAR LABORATOIRE'),
  new FakeEl({ class: 'webix_item_tab' }, 'PAR PRESENTATION GENERIQUE'),
];
const validerBtn = new FakeEl({}, 'Valider');

function matchSel(el, sel) {
  return sel.split(',').some((part) => {
    part = part.trim();
    if (part.startsWith('[') && part.endsWith(']')) {
      const m = part.slice(1, -1).split('=');
      const k = m[0];
      if (m.length === 1) return el.getAttribute(k) !== null;
      return el.getAttribute(k) === m[1].replace(/^"|"$/g, '');
    }
    if (part.startsWith('.')) return (el.attrs.class || '').split(' ').includes(part.slice(1));
    if (part === 'button') return /valider/i.test(el.textContent);
    return false;
  });
}

global.document = {
  body: { get innerText() { return 'Situation ' + periode + ' ' + onglet; } },
  querySelectorAll: (sel) => elements.filter((e) => matchSel(e, sel)),
  querySelector: (sel) => elements.find((e) => matchSel(e, sel)) || null,
  documentElement: { appendChild() {} },
};
global.MouseEvent = class { constructor(type) { this.type = type; } };
global.webix = {
  $$: (id) => {
    if (id === 'dt_sellout_resume_productname') {
      return vues.generic ? { data: { serialize: rowsFor } } : null;
    }
    if (id === 'startDate') return { setValue: (d) => { pending.s = d; } };
    if (id === 'endDate')   return { setValue: (d) => { pending.e = d; } };
    if (id === 'my_datepicker') {
      return { $view: { querySelectorAll: () => [validerBtn] }, hide() {} };
    }
    return null;
  },
};

const listeners = [];
const recu = [];
global.window = {
  addEventListener: (t, f) => { if (t === 'message') listeners.push(f); },
  postMessage: (data) => {
    if (data && data.source === 'bp-page') recu.push(data);
    listeners.forEach((f) => f({ source: global.window, data }));
  },
  location: { hash: '#!/top/organization.dashboard', search: '' },
};
global.location = global.window.location;
global.setTimeout = setTimeout;

// ── Exécution ───────────────────────────────────────────────────────────────
require('./ospharm_main.js');

(async () => {
  console.log('── collecte sur faux DATASTAT ──');
  check('signal « prêt » émis', recu.some((m) => m.type === 'ready'), true);

  global.window.postMessage({ source: 'bp-ext', cmd: 'collect', year: 2025 });

  const t0 = Date.now();
  while (!recu.some((m) => m.type === 'result' || m.type === 'error')) {
    if (Date.now() - t0 > 120000) break;
    await new Promise((r) => setTimeout(r, 100));
  }

  const err = recu.find((m) => m.type === 'error');
  if (err) { check('aucune erreur', err.error, '(aucune)'); }

  const res = recu.find((m) => m.type === 'result');
  check('résultat reçu', !!res, true);
  if (res) {
    check('12 mois collectés', res.months.length, 12);
    check('janvier : 2 lignes', res.months[0].rows.length, 2);
    check('février : 1 ligne', res.months[1].rows.length, 1);
    check('total des lignes', res.total, 13);
    check('période de janvier', [res.months[0].period_start, res.months[0].period_end],
          ['2025-01-01', '2025-01-31']);
    check('février bissextile 2025', res.months[1].period_end, '2025-02-28');
    check('mois lus dans l’ordre', res.months.map((m) => m.month),
          [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    // La table ne contient les bonnes lignes que si la période a réellement été
    // appliquée avant lecture : le libellé porte le numéro de mois.
    check('contenu propre au mois', res.months[2].rows[0].product_name.slice(0, 11), 'MOLECULE 03');
  }
  check('navigation effectuée', vues.generic, true);
  check('onglet présentation sélectionné', onglet, 'PAR PRESENTATION GENERIQUE');
  check('progression émise', recu.filter((m) => m.type === 'progress').length >= 12, true);

  console.log('\n' + '='.repeat(52) + `\n  ${ok} réussis, ${ko} échoués\n` + '='.repeat(52));
  process.exit(ko ? 1 : 0);
})();
