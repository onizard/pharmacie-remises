// break-pharma connect — popup : connexion / déconnexion break-pharma.
// Toute la logique jeton vit dans le service worker (background.js) ; le popup
// se contente de lui envoyer des messages.

const $ = (id) => document.getElementById(id);

function send(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

function show(loggedIn, email) {
  $('login').style.display  = loggedIn ? 'none'  : 'block';
  $('logged').style.display = loggedIn ? 'block' : 'none';
  if (loggedIn) $('who').textContent = email || 'Connecté';
}

async function refresh() {
  const s = await send({ type: 'bp-status' });
  show(!!(s && s.loggedIn), s && s.email);
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
  if (r && r.ok) {
    msg.textContent = ''; $('pass').value = '';
    show(true, r.email);
  } else {
    msg.textContent = 'Échec : ' + ((r && r.error) || 'identifiants invalides'); msg.className = 'err';
  }
});

$('pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btnLogin').click(); });

$('btnLogout').addEventListener('click', async () => {
  await send({ type: 'bp-logout' });
  $('msg').textContent = ''; $('msg').className = '';
  show(false);
});

refresh();
