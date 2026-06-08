# BREAK-PHARMA — contexte projet

## C'est quoi
Outil de simulation de remises sur génériques pour pharmaciens.
Site web statique hébergé sur GitHub Pages à l'adresse **break-pharma.fr** (Cloudflare DNS).
Dépôt GitHub : https://github.com/onizard/pharmacie-remises

## Fichier principal
`index.html` — application monopage complète (HTML + CSS + JS inline).
Pas de framework, pas de build. On édite `index.html` et on `git push`.

## Stack
- Frontend : HTML/CSS/JS vanilla, polices Orbitron + Share Tech Mono
- Base de données : **self-hosted PostgreSQL + PostgREST** sur Hetzner VPS (178.104.40.21)
- Authentification : **GoTrue v2.151.0** self-hosted sur Hetzner VPS
- Storage : **MinIO** sur NAS Synology via tunnel autossh → Hetzner
- Emails auth : **Resend** (`noreply@break-pharma.fr`, domaine vérifié)
- Scraping : Python + Playwright (sync_api) + openpyxl
- API scraper : FastAPI sur Render (`api_scraper/main.py`), service ID `srv-d81ktm3tqb8s73ehk7mg`
- OSPHARM scraping : GitHub Actions (`scraper_ospharm.yml`), 7 GB RAM

## Endpoints API (api.break-pharma.fr → nginx → services internes)
| Route nginx | Service | Port |
|---|---|---|
| `/auth/v1/` | GoTrue | 9999 |
| `/verify` | GoTrue (liens email reset) | 9999 |
| `/rest/v1/` | PostgREST | 3000 |
| `/storage/v1/` | MinIO (tunnel NAS) | 19000 |

## Storage MinIO
- **Endpoint public** : `storage.break-pharma.fr` (nginx → tunnel port 19000 → MinIO NAS)
- **Buckets** : `bp-files`, `grossiste`, `fse-bank`
- **Tunnel** : autossh sur NAS → Hetzner VPS, clé SSH `nas_tunnel_key` (non versionnée)

## Déploiement
```bash
git add -A && git commit -m "description" && git push
# Le site se met à jour automatiquement via GitHub Pages
```
Alias disponible : `git wip` (add + commit "wip" + push en une commande)
⚠️ `git wip` committe tout — vérifier que `.env` et `nas_tunnel_key` sont bien dans `.gitignore`

## Scripts Python
| Fichier | Rôle |
|---|---|
| `scraper_astera.py` | Scrape les PDFs de remises depuis agora.cerp.fr → Excel |
| `scraper_puht.py` | Scrape les prix PU HT depuis webuy.astera.coop/ART412 |
| `extraire_excel.py` | Extrait les données de remise des PDFs → Excel normalisé |
| `sync_supabase.py` | Pousse les données Excel vers Supabase |
| `serveur_pdf.py` | Serveur local (port 5050) pour servir les PDFs et extraire CGV |
| `api_scraper/main.py` | API FastAPI — endpoints /connect, /run, /status, /parse/* |
| `api_scraper/run_job_ospharm.py` | Scraper OSPHARM mensuel (GitHub Actions) |

Credentials dans `.env` (non versionné) :
```
ASTERA_USER=...
ASTERA_PASSWORD=...
SUPABASE_URL=https://api.break-pharma.fr
SUPABASE_KEY=<anon JWT self-hosted>
RESEND_API_KEY=<send-only>
RESEND_FULL_ACCESS=<full access>
CLOUDFLARE_API_KEY=...
```

## Structure de index.html
- Lignes 1–490 : CSS complet
- Lignes 490–695 : Écrans auth (login, signup, forgot password, reset)
- Lignes 695–780 : Header, stepbar, layout principal
- Lignes 780–860 : State management, constantes (SUPA_URL, SUPA_KEY)
- Lignes 860–1200 : Logique labos, conditions, chargement RSF
- Lignes 1200–1600 : Rendu tableau RSF, câblage inputs
- Lignes 1600–1790 : Simulation (launchSimulation, parsing CSV)
- Lignes 1790–2060 : Jeu Breakout (startBrickGame — animation pendant le chargement)
- Lignes 2060+ : Résultats de simulation, export

## Fonctionnalités clés
- **Simulation** : import CSV d'achats, calcul de remise optimale par labo
- **RSF** : tableau de paliers de remise par laboratoire, avec RSF First
- **Remise 3** : colonne optionnelle (toggle bouton dans le thead)
- **RSF First** : bouton ☆ dans le thead RSF% pour forcer l'application du taux first
- **Jeu Breakout** : animation pendant le chargement, labos en pixel art 5×7, niveaux triés par nb de références croissant, sauvegarde localStorage `bp_game_v2`
- **PDA** : toggle pour inclure/exclure les références PDA dans les paliers RSF
- **Page Recap OSPHARM** : tableau mensuel par labo (qty, CA, remise pondérée). Chargement avec barre de progression `osp-bar-*`, puis START button → brick game 10s → résultats
- **Page Comparaison** : analyse de scénarios. Même pattern de chargement que Recap (barre `osp-bar-*`, START button, brick game 10s)
- **Connecteur Grossiste** : drop XLSX → upload MinIO → parse auto → `_grossisteMonthStats` → sauvegarde cloud

## Architecture OSPHARM scraper (run_job_ospharm.py)
- Boucle Jan N-1 → mois courant, scraping incrémental (mois déjà en base réutilisés)
- Interface Webix 6 — pas d'inputs texte dans le date picker → navigation par clic cellules calendrier (Approche A2)
- Export via `page.expect_download(timeout=30_000)` — si pas de download en 30s, fallback `webix.toExcel()`
- Données compactées : `{cip13, qty, puht, year, month}` stockées dans `user_state.ospharm_job.rows`
- `month_meta` : liste `{year, month, period_start, period_end, rows, file_url}` par mois
- `month_stats` : agrégats par `{year-MM: [{labo, qty, ca_brut, pond_pct, remise_totale, pa_net}]}`
- PUHT calculé depuis montant catalogue / quantité, synchronisé vers `references_pharmacie`

## Architecture Render API (api_scraper/)
- `supabase_client.py` : SUPA_URL = `https://api.break-pharma.fr`, SERVICE_KEY depuis env Render
- `verify_token()` : GET `/auth/v1/user` → valide le JWT GoTrue de l'utilisateur
- `camoufox fetch` : téléchargé lazily au premier scraping Digipharmacie (pas au build Docker)
- `python-multipart` requis dans requirements.txt pour les endpoints UploadFile

## Identité visuelle
Thème rétro-terminal sombre : fond `#04060f`, cyan `#00e5ff`, amber `#ffab00`, vert `#00ff88`, rouge `#ff3366`. Police Orbitron pour les titres.

## WIP — 2026-06-08 (master)
```
 api_scraper/test_annee_lissee.py | 262 ++++++++++++
 api_scraper/test_full_ospharm.py |  53 +++++++
 api_scraper/test_nav_ospharm.py  | 261 ++++++++++++
 3 files changed, 576 insertions(+)
```

### Derniers changements significatifs
- **GitHub Actions OSPHARM** : dispatch depuis Render, scraping sur runners 7 GB. Fichier : `.github/workflows/scraper_ospharm.yml`, script : `api_scraper/run_job_ospharm.py`.
- **Popup connecteurs** : se ferme automatiquement et ouvre le comparateur quand OSPHARM réussit + données reconnues.
- **Comparateur — CA CIBLE** : affiche le seuil du 1er palier même si non atteint (en rouge avec ✗).
- **Comparateur — Top 3** : seuls gagnant + 2 suivants visibles par défaut ; bouton "AFFICHER LES N AUTRES SCÉNARIOS" pour le reste.
