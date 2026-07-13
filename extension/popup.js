// break-pharma connect — popup : connexion break-pharma + synchro manuelle.
// La synchro se fait dans l'onglet Digipharmacie (session utilisateur) : le popup
// envoie un message au content script de l'onglet actif.

const $ = (id) => document.getElementById(id);
const send = (msg) => new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));

function show(loggedIn, email) {
  $('login').style.display  = loggedIn ? 'none'  : 'block';
  $('logged').style.display = loggedIn ? 'block' : 'none';
  if (loggedIn) $('who').textContent = email || 'Connecté';
}

function _fmtAgo(ts) {
  if (!ts) return 'jamais';
  const m = Math.floor((Date.now() - ts) / 60000);
  if (m < 1) return "à l'instant";
  if (m < 60) return 'il y a ' + m + ' min';
  const h = Math.floor(m / 60);
  if (h < 24) return 'il y a ' + h + ' h';
  return 'il y a ' + Math.floor(h / 24) + ' j';
}

async function refreshLastSync() {
  try {
    const { bp_last_sync } = await chrome.storage.local.get('bp_last_sync');
    $('lastSync').textContent = 'Dernière synchro : ' + _fmtAgo(bp_last_sync);
  } catch (_) {}
}

async function refresh() {
  const s = await send({ type: 'bp-status' });
  show(!!(s && s.loggedIn), s && s.email);
  refreshLastSync();
}

$('btnLogin').addEventListener('click', async () => {
  const email = $('email').value.trim();
  const password = $('pass').value;
  const msg = $('msg');
  if (!email || !password) { msg.textContent = 'Renseignez e-mail et mot de passe.'; msg.className = 'err'; return; }
  $('btnLogin').disabled = true;
  msg.textContent = 'Connexion…'; msg.className = '';
  const r = await send({ type: 'bp-login', email, password });
  $('btnLogin').disabled = false;
  if (r && r.ok) { msg.textContent = ''; $('pass').value = ''; show(true, r.email); }
  else { msg.textContent = 'Échec : ' + ((r && r.error) || 'identifiants invalides'); msg.className = 'err'; }
});

$('pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btnLogin').click(); });

$('btnLogout').addEventListener('click', async () => {
  await send({ type: 'bp-logout' });
  $('msg').textContent = ''; $('msg').className = '';
  show(false);
});

// « Synchroniser maintenant » : trouve un onglet Digipharmacie et déclenche la synchro.
$('btnSync').addEventListener('click', async () => {
  const msg = $('syncMsg');
  msg.textContent = 'Recherche de l’onglet Digipharmacie…'; msg.className = '';
  let tabs = [];
  try { tabs = await chrome.tabs.query({ url: 'https://app.digipharmacie.fr/*' }); } catch (_) {}
  if (!tabs.length) {
    msg.innerHTML = 'Ouvrez d’abord <a href="https://app.digipharmacie.fr" target="_blank">app.digipharmacie.fr</a> (connecté), puis réessayez.';
    msg.className = 'err';
    return;
  }
  msg.textContent = 'Synchronisation en cours dans l’onglet Digipharmacie…';
  try {
    const r = await chrome.tabs.sendMessage(tabs[0].id, { type: 'bp-sync-now', full: true });
    if (r && r.ok) msg.textContent = r.queued != null ? ('✓ ' + r.queued + ' facture(s) envoyée(s).') : '✓ Terminé.';
    else if (r && r.needLogin) { msg.textContent = 'Reconnectez-vous à break-pharma ci-dessus.'; msg.className = 'err'; }
    else { msg.textContent = 'Voir le détail dans l’onglet Digipharmacie (bulle en bas à droite).'; }
  } catch (_) {
    msg.innerHTML = 'Rechargez l’onglet <a href="https://app.digipharmacie.fr" target="_blank">Digipharmacie</a> puis réessayez.';
    msg.className = 'err';
  }
  refreshLastSync();
});

refresh();
