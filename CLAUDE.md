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
- Base de données : **Supabase** (table `references_pharmacie`, table `synonymes_libelles`)
- Authentification : Supabase Auth (email/password)
- Scraping : Python + Playwright + BeautifulSoup

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
| `serveur_pdf.py` | Serveur local pour servir les PDFs |

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

## Identité visuelle
Thème rétro-terminal sombre : fond `#04060f`, cyan `#00e5ff`, amber `#ffab00`, vert `#00ff88`, rouge `#ff3366`. Police Orbitron pour les titres.
