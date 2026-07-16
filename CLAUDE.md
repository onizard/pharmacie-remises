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
| `api_scraper/grossiste_parse.py` | Parseurs justificatif répartiteur CERP (feuilles « Récap par mois » + « Détail par mois »). **Colonnes lues à l'EN-TÊTE, pas codées en dur** : le gabarit CERP décale ses colonnes d'un cran sur les mois anciens (colonne vide en 3e position, ex. jan.–avr. 2026) → sinon taux=None (paliers ignorés) et ca_brut lu sur la colonne des quantités. `_merge_grossiste_stats` REMPLACE le mois re-déposé (pas d'addition → re-dépôt idempotent). |
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
- **Exceptions par référence, PAR MOIS** : dans « mes conditions labo », un panneau repliable (en-tête `.lab-tag` façon « TABLEAU DES REMISES », `_renderRefExceptions`) sous le tableau des remises remplace l'ancienne case « exception » par palier. Recherche labo-wide (tous paliers, `_refExcSearch`) → par réf on saisit remise 2 (RDP), remise 3 (si coop) et un **override de palier RSF**. **Modèle mois = celui des onglets conditions** : `labPage.refExceptionsByMonth[year][month]` = liste COMPLÈTE en vigueur ce mois-là ; un mois sans clé **hérite** du dernier mois antérieur qui en a une (`_refExcEffective`). Éditer/ajouter/supprimer sur un mois **clone d'abord la liste héritée dans ce mois** (`_refExcOwnList`) puis la modifie → on peut avoir 5 exceptions janv.–nov. puis 3 en déc. sans toucher janv.–nov. Pas de sélecteur de date (le mois vient de l'onglet `activeMon2026/2025`). Re-render au changement d'onglet via `_rerenderRefExc26/25`. Sérialisé via `user_state` (`refExceptionsByMonth` + flag `refExcMigrated`), PAS écrit dans `rsf_history` (annuel + réécrit à chaque import CGV) ; superposé au calcul via la liste effective du mois : vérificateur (`_refExcRdpOverride` dans `_r2ExactForMonth`) et comparateur (RSF via `_rowRsfOverride`, RDP via `_rowExcOverride`→`getRemise`). Migration idempotente de TOUS les labos au chargement (`_migrateAllLabsExceptions`→`_migrateExceptionsToRefFormat`→`_refExcFlatToByMonth`, résout libellé→cip13 via rsf_history). Rétro-compat : ancien tableau plat `refExceptions` converti en `refExceptionsByMonth` au restore.
- **Simulation** : import CSV d'achats, calcul de remise optimale par labo
- **RSF** : tableau de paliers de remise par laboratoire, avec RSF First
- **Remise 3** : colonne optionnelle (toggle bouton dans le thead)
- **RSF First** : bouton ☆ dans le thead RSF% pour forcer l'application du taux first
- **Jeu Breakout** : animation pendant le chargement, labos en pixel art 5×7, niveaux triés par nb de références croissant, sauvegarde localStorage `bp_game_v2`
- **PDA** : toggle pour inclure/exclure les références PDA dans les paliers RSF
- **Page Recap OSPHARM** : tableau mensuel par labo (qty, CA, remise pondérée). Chargement avec barre de progression `osp-bar-*`, puis START button → brick game 10s → résultats
- **Page Comparaison** : analyse de scénarios. Même pattern de chargement que Recap (barre `osp-bar-*`, START button, brick game 10s)
- **Connecteur Grossiste** : drop XLSX → upload MinIO → parse auto → `_grossisteMonthStats` → sauvegarde cloud
- **Virement manuel (filet de sécurité)** : bouton « ＋ virement manquant » dans la barre du vérificateur. Si le scraper FSE rate un virement (ex. 1 396,29 € du 29/10/25, absent de l'export Webix), on le saisit à la main (labo, type RDP/coop, montant, date, mois d'encaissement). Stocké à part dans `state._fseManualVir` (persisté cloud via `fseManualVir`), ré-injecté de façon idempotente dans `_fseMonthStats` par `_fseApplyManual()` au début de `renderVerificateur()`. Type imposé via `t._mvtype` (prioritaire dans `_fseVtype`). Survit aux re-scrapes ; se rapproche automatiquement comme un virement scrapé (avoir / n° facture / montant au centime). `_fseMonthStatsNoManual()` retire les injections avant sérialisation pour éviter le double stockage.

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

## Règle métier spécifique : contrat Biogaran (marché générique)

### Exclusion de palier = RDP uniquement
Le flag « exclu » d'un palier RSF (ex. 2,5 % / 5 %, exclusion par défaut Biogaran)
n'exclut QUE la remise 2 (RDP). La RSF et la coop (remise3) restent dues sur ces
paliers. Ne jamais filtrer remise3 sur `excluded`.

### Autres règles vérifiées sur données réelles (2026-07)
- Les remises labo portent sur les ACHATS (justificatif répartiteur + factures
  directes CSP), jamais sur les ventes OSPHARM.
- Le barème RDP payé suit l'ANNÉE DU MOIS : 2025 payé au barème 2025 jusqu'à
  décembre inclus, barème 2026 à partir de janvier 2026 (transition indépendante
  de la bascule d'affichage `_y25cut`).
- **RDP vérificateur = hybride** : taux CGV `rsf_history` par CIP par défaut, MAIS si
  l'admin a modifié à la main la RDP de base d'un palier (`remise2Manual` dans les
  conditions labo effectives du mois), sa valeur prime pour les réfs de ce palier
  (`palManual` dans `_r2ExactForMonth`). Les exceptions par référence priment sur tout.
  Le comparateur utilise déjà les conditions (`getRemise`).
- Réalisation du contrat (coef coop) : CA BRUT tarif (répartiteur `ca_brut` +
  achats directs Digi), fenêtre depuis la date de signature, annualisée sur les
  mois écoulés.
- L'avoir RDP agrège par TAUX VERSÉ, pas par palier RSF (les exceptions d'un
  palier sont versées dans la ligne de leur taux).
- Les virements coop sont des sommes rondes (sans décimales) ; décimales ⇒ RDP.
  ⚠️ Cette règle « somme ronde = coop » ne vaut QUE pour un labo À COOP. `_fseVtype(t, laboNorm)`
  prend le labo : pour un contrat direct RDP (ex. Zydus, sans coop), une somme ronde est
  une RDP (sinon la RDP reçue tombait à 0). `_normHasCoop(laboNorm)` tranche.
- SEPT. 2025 (Biogaran) : avoir facturé à des taux HORS barème (20 % au lieu de
  30 % sur palier 10, remise sur palier 15 sans RDP, paliers 20/30 omis) —
  application anticipée d'un projet de loi finalement NON VOTÉ. Retour au barème
  2025 dès octobre (avoirs oct.–déc. à 30/20/10). Sous-facturation à réclamer ;
  la règle « 2025 payé au barème 2025 » reste valable toute l'année.
- Transition fin 2025 : dès la bascule catalogue (_y25cut = sept.), le
  répartiteur regroupe selon la structure 2026 (refs ex-40/15 % → palier 30 %)
  et le labo paie RDP 10 % sur TOUT le palier 30 (override dans
  _r2ExactForMonth, mois ≥ bascule uniquement).

### Généralisation vérificateur : Biogaran → tout labo à contrat direct
Le vérificateur n'est plus verrouillé sur le littéral `'BIOGARAN'`. DEUX notions :
- **Contrat direct signé** (`labPage.contractSigned`/`contractSigned25`) → le labo est
  analysé à fond (RSF + RDP + récap litige, déjà génériques). Couvre Zydus, puis tout
  labo à contrat direct (`_verifIsDirectContract`).
- **A une coop** (`_verifHasCoop` = `coopR3>0` ou un palier avec `remise3`) → applique EN
  PLUS la mécanique coop : coefficient 80 % (`_verifCoopCoef`, ex-`_verifBiogaranCoef`),
  échéancier plafonné (`_verifFillBiogaranCoop`), repli `coopR3`, « somme ronde = coop »
  (`_fseVtype`). Aujourd'hui : Biogaran seul.

Zydus = contrat direct **RDP, sans coop** → réconciliation RSF+RDP, pas de coefficient ni
d'échéancier. Reste spécifique Biogaran (événements historiques, NON généralisés) :
`_p30at10` (transition barème 2025) et l'argument « loi non votée » de septembre 2025.

### Principe (coop — labos à contrat coop, ex. Biogaran)
Biogaran impose un CA annuel contractuel. Le seuil de validation du marché est **80%** de ce CA.
Le CA cible = `caCondition` du palier (condition) avec la remise la plus élevée.

### Remises selon réalisation annuelle
| Réalisation | RSF | Remise 2 (RDP) | Remise 3 (coop) |
|---|---|---|---|
| < 80% | ✓ | ✗ | ✗ |
| ≥ 80% | ✓ | ✓ | 90% immédiat + rattrapage avril N+1 |

**Formule coop effective annuelle** (si réalisation ≥ 80%) :
```
coop_effective = remise3 × (90% + réalisation% × 10%)
```
Exemples : 80% → 98% de remise3 | 90% → 99% | 100% → 100%

Le rattrapage d'avril N+1 = `réalisation% × 10% × remise3` (versé en une fois).

### Calcul de la réalisation

**N-1 (scénario passé)** :
- Source : données grossiste réelles (`_grossisteMonthStats`) — **jamais OSPHARM** (OSPHARM = ventes, grossiste = achats)
- `réalisation = CA grossiste Biogaran N-1 / caCondition_max`

**N (année en cours, au mois échu)** :
- CA mensuel requis = `caCondition_max / 12`
- Règle de déblocage coop mensuel : la coop est **bloquée** tant que la moyenne mensuelle depuis janvier < seuil mensuel (CA_annuel/12)
- Dès que la moyenne mensuelle depuis janvier ≥ seuil mensuel → coop débloquée (versée à 90%)
- Exemple : pharmacie en retard en mars, rattrapée en juin → coop débloquée en juin, versée sur juin+rattrapage des mois bloqués
- Le 10% restant reste toujours en avril N+1, proratisé à la réalisation finale

### Implémentation dans le code
- Champ concerné : `remise3` dans `rsfValues` (colonne Remise 3 du tableau RSF)
- Labo concerné : `'Biogaran'` uniquement
- `caCondition_max` = max(`caCondition`) sur toutes les conditions du lab Biogaran
- Dans la simulation : appliquer le coefficient `(0.9 + min(réalisation, 1.0) * 0.1)` à remise3 si réalisation ≥ 0.8, sinon remise2=0, remise3=0
- Source N-1 : `state._grossisteMonthStats` filtré sur année N-1, clé labo = 'Biogaran' ou alias
- Source N : même source filtrée sur mois de l'année N jusqu'au mois échu

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
