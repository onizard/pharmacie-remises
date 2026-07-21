# Brief — Scraper Digipharmacie côté serveur (brique 2)

Tu tournes sur le **VPS Hetzner** (178.104.40.21) du projet break-pharma.fr.
Objectif : écrire et faire tourner un scraper serveur qui récupère les factures
Digipharmacie et les dépose dans la table `digi_files` en `pending`, pour que le
batch parser existant (`api_scraper/run_job_digi_batch.py`) les traite.

**Fais TOUT de bout en bout, sans demander de copier-coller à l'utilisateur.**
Lance les scripts toi-même, lis leur sortie, itère, commit + push.

## État acquis (déjà validé — NE PAS refaire)

- **Tunnel SOCKS résidentiel** : service systemd `digi-nas-tunnel.service` = autossh
  VPS→NAS Synology, proxy SOCKS sur `127.0.0.1:1080`. Egress = IP RÉSIDENTIELLE du
  NAS (193.253.63.114). Vérifier : `systemctl is-active digi-nas-tunnel.service`.
- **Cloudflare** : Digipharmacie est derrière Cloudflare qui BLOQUE les IP datacenter.
  Seule combo qui passe (PROUVÉE) = **camoufox écran virtuel** (`VIRTUAL=1`, Xvfb) **+
  proxy SOCKS résidentiel** (`PROXY=socks5://127.0.0.1:1080`, `geoip=True`). urllib=403,
  curl_cffi=403, camoufox headless simple=403.
- Camoufox déjà installé (`python3 -m camoufox fetch` fait).
- **Login = OK** : `POST /auth/login/` avec `{email, password}` JSON renvoie
  `{"key": "<token>"}` (auth dj-rest-auth par TOKEN). L'API exige ensuite le header
  `Authorization: Token <key>` (le cookie seul renvoie le HTML de la SPA).
- Identifiants dans l'env : `$DIGI_USER` / `$DIGI_PASS` (jamais en dur, jamais commit).

## Étape 1 — Découverte des endpoints (DÉJÀ PRÊTE)

`api_scraper/digi_discover_probe.py` se logue, récupère le token, ÉCOUTE le trafic
réseau `/api` pendant que la SPA charge (les vraies routes ne sont PAS
`/api/v1/invoices/` — ça renvoie le HTML fourre-tout), puis dumpe la structure d'une
facture. Lance-le et LIS la sortie :

```bash
VIRTUAL=1 PROXY=socks5://127.0.0.1:1080 python3 api_scraper/digi_discover_probe.py
```

Ce qu'il faut en tirer :
- le bloc `===== Endpoints /api observés =====` → la VRAIE route des factures ;
- les CHAMPS d'une facture (fournisseur, date, montant HT, id…) ;
- **comment télécharger le PDF** : URL directe dans le JSON ? id → endpoint
  `/…/<id>/download/` ? (point clé). Si la piste PDF n'est pas évidente, ré-écoute
  le réseau en cliquant sur une facture dans la SPA pour capter la requête de download.

## Étape 2 — Écrire le scraper

- Login camoufox (réutilise le flow du probe : goto `/login/`, attendre que le titre
  CF se vide, `POST /auth/login/`, extraire `key`).
- Boucler sur la vraie route factures avec pagination, du plus récent au plus ancien.
- Télécharger chaque PDF **via le contexte navigateur camoufox** (donc à travers le
  proxy — ne PAS faire de requête hors camoufox, Cloudflare rebloquerait).
- Insérer dans `digi_files` en statut `pending`. Regarde comment
  `run_job_digi_batch.py` et l'extension écrivent déjà dans `digi_files` (colonnes,
  storage MinIO vs base64, `user_id`) et respecte EXACTEMENT ce schéma.
- **Idempotence OBLIGATOIRE** : ne jamais ré-insérer une facture déjà présente
  (clé = numéro/id Digi). ⚠️ RÈGLE CRITIQUE : les lignes produits Digi sont ADDITIVES,
  pas idempotentes (`_merge_digi_stats` cumule). Ne JAMAIS repasser un fichier `done`
  en `pending` → double comptage. N'insérer que du NOUVEAU.
- Commencer sur LE COMPTE ADMIN uniquement. Multi-users = plus tard.
- Idéalement : un cron nightly + un déclenchement depuis le connecteur break-pharma.

## Accès base (Supabase self-hosted)

- API : `https://api.break-pharma.fr` (PostgREST `/rest/v1/`, GoTrue `/auth/v1/`).
- SERVICE_KEY dans l'env (`.env` / env VPS, jamais en dur). `api_scraper/supabase_client.py`
  a déjà le client configuré — réutilise-le.

## Règles projet OBLIGATOIRES

- **Double branche** : commit sur `master` ET `claude/confident-newton-0scy4n`.
  ```
  git add -A && git commit -m "…"
  git branch -f claude/confident-newton-0scy4n master
  git push origin master && git push origin claude/confident-newton-0scy4n
  ```
- **Jamais de secret dans le dépôt** (public) : pas de mot de passe / clé / token en dur.
- Trailer de commit :
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01T9wpQQ4Pn8UVEVqF4pDyUk
  ```
- Valider avant push : `python3 -m py_compile` sur chaque .py touché.
- Lis `CLAUDE.md` à la racine = contexte projet complet.

## Réserve honnête

Une seule IP résidentielle (le NAS) ne passera pas à l'échelle pour TOUS les users
(Cloudflare finit par flagger). L'extension navigateur reste la base multi-users
propre ; ce scraper NAS+VPS est le « bonus » d'auto-scraping. Fais marcher le compte
admin de bout en bout d'abord.
