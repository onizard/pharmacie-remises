# BREAK-PHARMA — Skills & Scripts

Référence de tous les scripts et outils développés depuis le début du projet.

---

## Scripts locaux (à lancer manuellement)

### `scraper_astera.py`
Télécharge les PDFs de remises depuis le portail Astera Pro (agora.cerp.fr).
Login OIDC → section "Offres génériques" → download tous les PDFs disponibles → dossier `pdfs_remises/`.
```bash
python scraper_astera.py
```
**Prérequis .env :** `ASTERA_USER`, `ASTERA_PASSWORD`

---

### `scraper_puht.py`
Récupère les prix PU HT par CIP13 depuis webuy.astera.coop/ART412.
Login via SSO agora.cerp.fr (OIDC). Génère `puht_astera.json` : `{cip13: float}`.
```bash
python scraper_puht.py
```
**Prérequis .env :** `ASTERA_USER`, `ASTERA_PASSWORD`

---

### `extraire_excel.py`
Extrait les données de remise depuis les PDFs Astera → `remises_partenariat.xlsx` normalisé.
Pipeline : DCI + dosage + forme + quantité + PDA. Normalisation des libellés (synonymes).
```bash
python extraire_excel.py
```

---

### `sync_supabase.py`
Pousse `remises_partenariat.xlsx` + `libelle_synonyms.json` vers Supabase (table `references_pharmacie`).
```bash
python sync_supabase.py
```
**Prérequis .env :** `SUPABASE_URL`, `SUPABASE_KEY`

---

### `maj_rsf.py`
Pipeline complet de mise à jour RSF : enchaîne `scraper_astera` → `scraper_puht` → `extraire_excel` → `sync_supabase`.
Alias : taper `maj rsf` dans le terminal.
```bash
python maj_rsf.py
```

---

### `scraper_digipharmacie.py`
Version locale du scraper Digipharmacie. Login camoufox (bypass Cloudflare Turnstile) → API REST paginée
`/api/v1/invoices/` → filtre 2025, génériques → télécharge PDFs signés GCS → dossier `pdf_factures_generiques/`.
```bash
python scraper_digipharmacie.py
```
**Prérequis .env :** `BP_EMAIL`, `BP_PASSWORD`

---

### `scraper_ospharm.py`
Scrape les ventes produits 2025 depuis OSPHARM DATASTAT.
OAuth PKCE → "Analyse des ventes" → filtre Année N-1 → onglet Produits → export CSV.
```bash
python scraper_ospharm.py
```

---

### `serveur_pdf.py`
Serveur HTTP local (port 5050) pour servir les PDFs et extraire les CGV.
Utilisé avec `lancer.py` pour accéder à l'interface locale.
```bash
python serveur_pdf.py
# ou
python lancer.py   # démarre le serveur ET ouvre le navigateur
```

---

### `get_connectors.py`
Lit les identifiants des connecteurs (OSPHARM, DIGIPHARMACIE) depuis Supabase.
Utilisé comme dépendance par `scraper_digipharmacie.py`.
**Prérequis .env :** `BP_EMAIL` (ou `SUPABASE_SERVICE_KEY`)

---

### `maj_connecteurs.py`
Lance en séquence : `scraper_ospharm.py` puis `scraper_digipharmacie.py`.
```bash
python maj_connecteurs.py
```

---

## Scripts GitHub Actions (`api_scraper/`)

### `api_scraper/main.py`
API FastAPI déployée sur Render. Endpoints :
- `POST /connect/{connector}` — teste les identifiants, met à jour `connected` dans Supabase
- `POST /run/{connector}` — déclenche le workflow GitHub Actions
- `GET /status/{job_id}` — retourne le statut du job

Connecteurs supportés : `ospharm`, `digipharmacie`
Service Render ID : `srv-d81ktm3tqb8s73ehk7mg`

---

### `api_scraper/scraper.py`
Scraper Digipharmacie complet (version GitHub Actions).

Stratégie en 2 phases :
1. **curl_cffi** — login rapide (~5s) via `POST /api/v1/auth/login/`
2. **camoufox async** — fallback si Cloudflare bloque, + navigation SPA factures

Flux : login → `/factures/` → interception réponses API → pagination JS fetch →
filtre 2025/2026 génériques → download PDFs via urllib → extraction via `pdf_extractor.py`

Variables d'env : `USER_ID`, `SUPABASE_SERVICE_KEY`, `PROXY_URL`, `LABS_FILTER`, `PDF_DEBUG`

---

### `api_scraper/pdf_extractor.py`
Extrait les lignes produits depuis les PDFs de factures Digipharmacie.

**3 formats supportés :**

| Format | Détection | Données extraites |
|--------|-----------|------------------|
| **ALLOGA FRANCE** | "alloga france" en tête | CIP13, libellé, labo (AU NOM DE), qté, PU brut, remise%, PA net, total HT |
| **VIATRIS / MYLAN** | "viatris santé" ou "mylan" + "c.i.p" | table pdfplumber 6 col, cellules multi-lignes, remise calculée si absente |
| **COOPÉRATION PHARMACEUTIQUE** | "cooperation pharmaceutique" | EAN13, designation, qté livrée/facturée, PU brut/net, montant HT |
| **Fallback texte brut** | aucun format reconnu | regex CIP13 + valeurs numériques |

Filtre final : seules les lignes dont le labo est dans `LABOS_CIBLES` sont retournées.
`LABOS_CIBLES` : biogaran, teva, mylan, viatris, zydus, sandoz, zentiva, arrow, cristers, eg labo

```python
from pdf_extractor import extract_invoice_lines
lines = extract_invoice_lines(pdf_path, provider="ALLOGA", billing_date="2025-03-15")
# → [{"cip", "libelle", "labo", "fournisseur", "billing_date", "quantite", "prix_brut", "remise_pct", "pa_net", "total_ht"}, ...]
```

---

### `api_scraper/run_job.py`
Entry point GitHub Actions pour le scraper Digipharmacie.
Lit les creds depuis Supabase (`connectors` column > `state_json.connectors`) → appelle `scraper.py` →
écrit résultat dans `user_state.verif_job = {status, message, invoices, error}`.
```bash
USER_ID=xxx SUPABASE_SERVICE_KEY=yyy python3 api_scraper/run_job.py
```

---

### `api_scraper/run_job_ospharm.py`
Scraper OSPHARM mensuel (GitHub Actions, 7 GB RAM).
Boucle Jan N-1 → mois courant, incrémental. Interface Webix 6, navigation calendrier par clic.
Stocke dans `user_state` : `ospharm_job.rows` + `month_meta` + `month_stats`.

---

### `api_scraper/discover_digi.py`
Mode découverte : capture la structure brute de l'API Digipharmacie sans télécharger de PDFs.
Stocke dans Supabase : `state_json.digi_discover = {sample, fields, doc_types}`.
Déclenché via le workflow `discover_digi.yml`.

---

### `api_scraper/test_connector.py`
Teste uniquement le login sur OSPHARM ou DIGIPHARMACIE.
Écrit `state_json.conn_test.{connector} = {ok, error}` dans Supabase.
Déclenché via le workflow `test_connector.yml`.

---

### `api_scraper/supabase_client.py`
Helpers Supabase partagés : vérification JWT, lecture/écriture `user_state`, gestion connecteurs.

---

## GitHub Actions Workflows (`.github/workflows/`)

| Fichier | Déclencheur | Rôle |
|---------|-------------|------|
| `scraper.yml` | `workflow_dispatch` (user_id, labs_filter) | Scraper Digipharmacie complet |
| `scraper_ospharm.yml` | `workflow_dispatch` (user_id) | Scraper OSPHARM mensuel |
| `test_connector.yml` | `workflow_dispatch` (user_id, connector) | Test login seul |
| `discover_digi.yml` | `workflow_dispatch` (user_id) | Découverte structure API Digi |

---

## Librairies clés

| Lib | Usage |
|-----|-------|
| `camoufox` | Navigateur furtif (bypass Cloudflare Turnstile / Turnstile CAPTCHA) |
| `curl_cffi` | HTTP avec TLS fingerprint Chrome (bypass bot-detection Cloudflare) |
| `pdfplumber` | Extraction texte + tableaux depuis PDFs (factures, remises) |
| `playwright` | Automatisation navigateur (OSPHARM Webix, Astera OIDC) |
| `FastAPI` | API REST déployée sur Render |
| `openpyxl` | Lecture/écriture Excel (.xlsx) |

---

## Supabase — Structure des données

| Table | Colonnes clés | Usage |
|-------|--------------|-------|
| `references_pharmacie` | cip13, libelle, labo, rsf, pa_net, puht | Base principale des remises |
| `synonymes_libelles` | libelle_raw, libelle_norm | Normalisation des libellés PDF |
| `user_state` | user_id, state_json, connectors | État utilisateur + creds connecteurs |
| `rsf_defaults` | labo, paliers | Valeurs RSF par défaut (admin) |

`user_state.verif_job` : résultat du dernier job Digipharmacie `{status, message, invoices[], error}`
`user_state.ospharm_job` : données OSPHARM `{rows, month_meta, month_stats}`

---

## Variables `.env` (non versionné)

```
ASTERA_USER=...           # Login portail agora.cerp.fr
ASTERA_PASSWORD=...
SUPABASE_URL=...          # URL projet Supabase
SUPABASE_KEY=...          # Clé anon Supabase
BP_EMAIL=...              # Email compte break-pharma.fr (pour get_connectors)
BP_PASSWORD=...           # Mot de passe break-pharma.fr
RENDER_API_KEY=...        # API Render (auto-deploy)
```

**GitHub Secrets** (pour les workflows) :
- `SUPABASE_SERVICE_KEY` — clé service Supabase (accès admin)
- `PROXY_URL` — proxy résidentiel (contournement geo-blocking digipharmacie)
- `GH_TOKEN` — token GitHub (dispatch workflows depuis Render)
