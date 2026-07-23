// break-pharma connect — service worker.
// Détient le jeton break-pharma (login GoTrue + rafraîchissement) et relaie les
// imports vers l'API. Les host_permissions du manifeste autorisent ces requêtes
// cross-origin depuis le worker (pas de blocage CORS).

const SUPA_URL = 'https://api.break-pharma.fr';
const SUPA_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYW5vbiIsImlzcyI6InN1cGFiYXNlLXNlbGYiLCJpYXQiOjE3ODM1NDU0MjV9.Ga5ubKMU5mnlcBncdb1TUgprBHxuDkRw0LBmGP81XwM';
const API_URL  = 'https://pharmacie-remises.onrender.com';

async function storeSession(d) {
  await chrome.storage.local.set({
    access_token:  d.access_token,
    refresh_token: d.refresh_token,
    expires_at:    Date.now() + ((d.expires_in || 3600) * 1000),
    user_email:    (d.user && d.user.email) || '',
  });
}

async function refreshSession(rt) {
  const res = await fetch(`${SUPA_URL}/auth/v1/token?grant_type=refresh_token`, {
    method: 'POST',
    headers: { apikey: SUPA_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: rt }),
  });
  if (!res.ok) throw new Error('refresh KO');
  const d = await res.json();
  await storeSession(d);
  return d.access_token;
}

async function getToken() {
  const s = await chrome.storage.local.get(['access_token', 'refresh_token', 'expires_at']);
  if (!s.access_token) return null;
  // Rafraîchit si expiré (ou proche : marge d'1 min).
  if (s.expires_at && Date.now() > s.expires_at - 60000) {
    if (!s.refresh_token) return null;
    try { return await refreshSession(s.refresh_token); }
    catch (e) { return null; }
  }
  return s.access_token;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'bp-login') {
    (async () => {
      try {
        const res = await fetch(`${SUPA_URL}/auth/v1/token?grant_type=password`, {
          method: 'POST',
          headers: { apikey: SUPA_KEY, 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: msg.email, password: msg.password }),
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) {
          sendResponse({ ok: false, error: d.error_description || d.msg || d.error || ('HTTP ' + res.status) });
          return;
        }
        await storeSession(d);
        sendResponse({ ok: true, email: (d.user && d.user.email) || msg.email });
      } catch (e) { sendResponse({ ok: false, error: e.message }); }
    })();
    return true;
  }

  if (msg.type === 'bp-status') {
    (async () => {
      const s = await chrome.storage.local.get(['user_email', 'access_token']);
      sendResponse({ loggedIn: !!s.access_token, email: s.user_email || '' });
    })();
    return true;
  }

  if (msg.type === 'bp-logout') {
    (async () => {
      await chrome.storage.local.remove(['access_token', 'refresh_token', 'expires_at', 'user_email']);
      sendResponse({ ok: true });
    })();
    return true;
  }

  if (msg.type === 'bp-import') {
    (async () => {
      const token = await getToken();
      if (!token) { sendResponse({ ok: false, needLogin: true }); return; }
      try {
        const res = await fetch(`${API_URL}/import/digi-json`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ invoices: msg.invoices }),
        });
        if (res.status === 401) { sendResponse({ ok: false, needLogin: true }); return; }
        if (!res.ok) { sendResponse({ ok: false, error: 'API break-pharma ' + res.status }); return; }
        const d = await res.json();
        sendResponse({ ok: true, queued: d.queued, received: d.received });
      } catch (e) { sendResponse({ ok: false, error: e.message }); }
    })();
    return true;
  }
});

// ── Synchro AUTOMATIQUE en arrière-plan (chrome.alarms) ──────────────────────
// La lecture des factures doit se faire DANS une page Digipharmacie (session +
// franchissement Cloudflare). Pour synchroniser sans que l'utilisateur reste sur
// la page, un réveil périodique ouvre app.digipharmacie.fr/factures dans un onglet
// EN ARRIÈRE-PLAN (inactif), déclenche la synchro dans son content script, puis
// referme l'onglet (seulement celui qu'on a ouvert). Verrou 20 h côté content.js.
const SYNC_ALARM = 'bp-daily-sync';
const DIGI_FACTURES = 'https://app.digipharmacie.fr/factures';
const SYNC_MIN_MS = 20 * 3600 * 1000;

function _armAlarm() {
  // Toutes les 6 h : au moins un réveil tombera pendant que l'ordinateur est allumé.
  chrome.alarms.create(SYNC_ALARM, { delayInMinutes: 2, periodInMinutes: 360 });
}
chrome.runtime.onInstalled.addListener(_armAlarm);
chrome.runtime.onStartup.addListener(_armAlarm);
chrome.alarms.onAlarm.addListener((a) => { if (a.name === SYNC_ALARM) backgroundAutoSync(); });

async function backgroundAutoSync() {
  try {
    // Verrou anti-répétition (même horloge que la synchro manuelle, verrou 20 h).
    const { bp_last_sync } = await chrome.storage.local.get('bp_last_sync');
    if (bp_last_sync && Date.now() - bp_last_sync < SYNC_MIN_MS) return;
    // Connecté à break-pharma ? (jeton rafraîchi si besoin) — sinon inutile d'ouvrir.
    const token = await getToken();
    if (!token) return;
    // Onglet Digipharmacie déjà ouvert ? on lui demande de synchroniser (on ne le
    // ferme pas : c'est un onglet de l'utilisateur).
    let tabs = [];
    try { tabs = await chrome.tabs.query({ url: 'https://app.digipharmacie.fr/*' }); } catch (_) {}
    if (tabs.length) {
      try { chrome.tabs.sendMessage(tabs[0].id, { type: 'bp-sync-now', full: false }); } catch (_) {}
      return;
    }
    // Aucun onglet Digi : on en ouvre un EN ARRIÈRE-PLAN, marqué #bp-autosync. Son
    // content script synchronisera puis demandera lui-même la fermeture (bp-sync-done)
    // → aucun setTimeout côté worker (robuste face au cycle de vie MV3).
    chrome.tabs.create({ url: DIGI_FACTURES + '#bp-autosync', active: false });
  } catch (_) {}
}

// Fermeture de l'onglet d'auto-synchro une fois la synchro terminée (le message
// réveille le service worker au besoin). On ne ferme QUE l'onglet émetteur.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === 'bp-sync-done') {
    if (sender.tab && sender.tab.id != null) { try { chrome.tabs.remove(sender.tab.id); } catch (_) {} }
    sendResponse({ ok: true });
    return true;
  }
});
