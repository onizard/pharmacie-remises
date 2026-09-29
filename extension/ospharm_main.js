// break-pharma connect — collecte OSPHARM, exécutée DANS le contexte de la page.
//
// POURQUOI CE FICHIER EST SÉPARÉ
// Les données de DATASTAT vivent dans les composants Webix de la page
// (`webix.$$('dt_sellout_resume_productname')`). Un script de contenu ordinaire
// s'exécute dans un monde ISOLÉ : il voit le DOM, mais pas les variables
// JavaScript de la page — donc pas `webix`. Ce fichier est donc déclaré avec
// `"world": "MAIN"` dans le manifeste. En contrepartie il n'a PAS accès à
// `chrome.runtime` : il dialogue avec ospharm.js par window.postMessage.
//
// CE QU'IL LIT
// « Audit génériques → Mes ventes » (#!/top/generic.sellout), onglet « PAR
// PRESENTATION GENERIQUE », table `dt_sellout_resume_productname` : une ligne
// par présentation générique, avec laboratoire, quantité, PFHT unitaire et
// remise réellement obtenue. C'est la seule vue qui remonte des données —
// « Analyse des ventes → Toutes les ventes » est cassée chez OSPHARM.

(() => {
  'use strict';

  const TAG = '[bp-ospharm]';
  const say = (type, data) =>
    window.postMessage(Object.assign({ source: 'bp-page', type }, data || {}), '*');

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function waitFor(fn, { timeout = 20000, step = 250 } = {}) {
    const t0 = Date.now();
    for (;;) {
      let v;
      try { v = fn(); } catch (_) { v = null; }
      if (v) return v;
      if (Date.now() - t0 > timeout) return null;
      await sleep(step);
    }
  }

  const W = () => (typeof webix !== 'undefined' ? webix : null);

  function clickNode(el) {
    if (!el) return false;
    for (const t of ['mousedown', 'mouseup', 'click']) {
      el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
    }
    return true;
  }

  function itemByLabel(prefix) {
    const p = prefix.toLowerCase();
    return [...document.querySelectorAll('.webix_tree_item,[webix_tm_id]')]
      .find((el) => (el.textContent || '').trim().toLowerCase().startsWith(p)) || null;
  }

  function table() {
    const w = W();
    if (!w) return null;
    try {
      const v = w.$$('dt_sellout_resume_productname');
      return v && v.data && v.data.serialize ? v : null;
    } catch (_) { return null; }
  }

  // ── Navigation vers Audit génériques → Mes ventes ──────────────────────────
  async function gotoGenericSellout() {
    if (location.hash.includes('generic.sellout') && table()) return true;

    // Le clic par LIBELLÉ résiste aux changements d'identifiants : OSPHARM a déjà
    // renommé « sellout.all » en « /sellout » en septembre 2026, ce qui avait
    // silencieusement cassé le scraper (la vue s'affichait, sans données).
    clickNode(itemByLabel('audit génériques') || itemByLabel('audit generiques'));
    await sleep(1200);

    const kid = document.querySelector('[webix_tm_id="generic.sellout"]');
    if (kid) clickNode(kid);
    else location.hash = '#!/top/generic.sellout';

    const ok = await waitFor(() => table(), { timeout: 25000 });
    if (!ok) return false;

    // Onglet « PAR PRESENTATION GENERIQUE » : la vue ouvre par défaut sur
    // « PAR LABORATOIRE », qui n'a qu'une ligne par génériqueur.
    const tab = [...document.querySelectorAll(
      '.webix_item_tab,.webix_segment_0,.webix_segment_1,.webix_segment_N,button')]
      .find((el) => /pr[ée]sentation/i.test(el.textContent || ''));
    if (tab) { clickNode(tab); await sleep(2500); }
    return true;
  }

  // ── Réglage de la période ─────────────────────────────────────────────────
  const two = (n) => String(n).padStart(2, '0');

  function headerPeriod() {
    // L'en-tête affiche « 01/01/25 à 31/01/25 ». C'est le témoin le plus fiable
    // que le filtre a bien été pris en compte : plus sûr qu'un délai fixe.
    const m = (document.body.innerText || '')
      .match(/(\d{2}\/\d{2}\/\d{2})\s*(?:à|au)\s*(\d{2}\/\d{2}\/\d{2})/);
    return m ? m[1] + '>' + m[2] : '';
  }

  async function setMonth(year, month) {
    const w = W();
    if (!w) return false;
    const last = new Date(year, month, 0).getDate();
    const want = `01/${two(month)}/${two(year % 100)}>${two(last)}/${two(month)}/${two(year % 100)}`;
    if (headerPeriod() === want) return true;

    const btn = document.querySelector('[view_id="button_date_picker"]');
    clickNode(btn && (btn.querySelector('button,.webix_button,.webix_template') || btn));
    await sleep(1200);

    try {
      const sd = w.$$('startDate');
      const ed = w.$$('endDate');
      if (!sd || !ed) return false;
      sd.setValue(new Date(year, month - 1, 1));
      ed.setValue(new Date(year, month - 1, last));
    } catch (_) { return false; }

    // Valider dans le popup
    const pop = (() => { try { return w.$$('my_datepicker'); } catch (_) { return null; } })();
    const scope = (pop && pop.$view) || document;
    const valider = [...scope.querySelectorAll('button,.webix_button,[view_id]')]
      .find((el) => {
        const t = (el.textContent || '').trim().toLowerCase();
        const r = el.getBoundingClientRect();
        return r.width > 1 && r.height > 1 &&
               ['valider', 'ok', 'appliquer', 'rechercher'].some((k) => t.startsWith(k));
      });
    if (valider) clickNode(valider);
    else if (pop && pop.hide) pop.hide();

    return !!(await waitFor(() => headerPeriod() === want, { timeout: 30000 }));
  }

  // ── Collecte ──────────────────────────────────────────────────────────────
  async function collect(year) {
    if (!(await waitFor(() => W(), { timeout: 30000 }))) {
      say('error', { error: 'Interface OSPHARM non chargée' });
      return;
    }
    if (!(await gotoGenericSellout())) {
      say('error', { error: "Vue « Audit génériques → Mes ventes » introuvable" });
      return;
    }

    const months = [];
    for (let m = 1; m <= 12; m++) {
      say('progress', { done: m - 1, total: 12, label: `${year}-${two(m)}` });
      if (!(await setMonth(year, m))) {
        console.warn(TAG, 'période non appliquée', year, m);
        continue;
      }
      await sleep(1500);                       // laisse la table se re-remplir
      const t = table();
      let rows = [];
      try { rows = t ? t.data.serialize() : []; } catch (_) { rows = []; }
      const last = new Date(year, m, 0).getDate();
      months.push({
        year, month: m,
        period_start: `${year}-${two(m)}-01`,
        period_end: `${year}-${two(m)}-${two(last)}`,
        rows,
      });
      console.log(TAG, `${year}-${two(m)} : ${rows.length} ligne(s)`);
    }

    const total = months.reduce((s, x) => s + x.rows.length, 0);
    if (!total) {
      say('error', { error: 'Aucune ligne lue — vérifiez que vos ventes s’affichent sur DATASTAT' });
      return;
    }
    say('progress', { done: 12, total: 12, label: '' });
    say('result', { months, total });
  }

  window.addEventListener('message', (e) => {
    if (e.source !== window) return;
    const d = e.data;
    if (!d || d.source !== 'bp-ext') return;
    // « ping » : ospharm.js (monde isolé) ne peut pas lire nos variables ; c'est
    // sa seule façon de savoir que ce fichier est bien injecté. Il sonde jusqu'à
    // obtenir une réponse, ce qui rend l'ordre de chargement des deux scripts
    // indifférent (le « ready » initial peut partir avant qu'il n'écoute).
    if (d.cmd === 'ping') { say('ready'); return; }
    if (d.cmd === 'collect') collect(d.year);
  });

  console.log(TAG, 'collecteur de page chargé');
  say('ready');
})();
