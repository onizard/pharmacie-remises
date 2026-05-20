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
- Base de données : **Supabase** (table `references_pharmacie`, table `synonymes_libelles`, table `user_state`, table `rsf_defaults`)
- Authentification : Supabase Auth (email/password)
- Scraping : Python + Playwright (sync_api) + openpyxl
- API scraper : FastAPI sur Render (`api_scraper/main.py`), service ID `srv-d81ktm3tqb8s73ehk7mg`
- OSPHARM scraping : GitHub Actions (`scraper_ospharm.yml`), 7 GB RAM, user_id=`1c371798-c33e-475c-84de-224f5559fee7`

## Déploiement
```bash
git add -A && git commit -m "description" && git push
# Le site se met à jour automatiquement via GitHub Pages
```
Alias disponible : `git wip` (add + commit "wip" + push en une commande)

## Scripts Python
| Fichier | Rôle |
|---|---|
| `scraper_astera.py` | Scrape les PDFs de remises depuis agora.cerp.fr → Excel |
| `scraper_puht.py` | Scrape les prix PU HT depuis webuy.astera.coop/ART412 |
| `extraire_excel.py` | Extrait les données de remise des PDFs → Excel normalisé |
| `sync_supabase.py` | Pousse les données Excel vers Supabase |
| `serveur_pdf.py` | Serveur local (port 5050) pour servir les PDFs et extraire CGV |
| `api_scraper/main.py` | API FastAPI — endpoints /connect, /run, /status par connecteur |
| `api_scraper/run_job_ospharm.py` | Scraper OSPHARM mensuel (GitHub Actions) |

Credentials dans `.env` (non versionné) :
```
ASTERA_USER=...
ASTERA_PASSWORD=...
SUPABASE_URL=...
SUPABASE_KEY=...
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

## Architecture OSPHARM scraper (run_job_ospharm.py)
- Boucle Jan N-1 → mois courant, scraping incrémental (mois déjà en base réutilisés)
- Interface Webix 6 — pas d'inputs texte dans le date picker → navigation par clic cellules calendrier (Approche A2)
- Export via `page.expect_download(timeout=30_000)` — si pas de download en 30s, fallback `webix.toExcel()`
- Données compactées : `{cip13, qty, puht, year, month}` stockées dans `user_state.ospharm_job.rows`
- `month_meta` : liste `{year, month, period_start, period_end, rows, file_url}` par mois
- `month_stats` : agrégats par `{year-MM: [{labo, qty, ca_brut, pond_pct, remise_totale, pa_net}]}`
- PUHT calculé depuis montant catalogue / quantité, synchronisé vers `references_pharmacie`

## Identité visuelle
Thème rétro-terminal sombre : fond `#04060f`, cyan `#00e5ff`, amber `#ffab00`, vert `#00ff88`, rouge `#ff3366`. Police Orbitron pour les titres.

## WIP — 2026-05-20
- Fix timeout `expect_download` : 180s → 30s (évitait blocage 3min/mois si pas de download)
- Page comparaison : ajout barre de chargement + START button + brick game 10s
