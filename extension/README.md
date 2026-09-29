# break-pharma connect — extension navigateur

Récupère vos factures/avoirs **Digipharmacie** et vos ventes génériques **OSPHARM**,
et les envoie vers **break-pharma.fr** depuis vos propres sessions.

## Pourquoi une extension ?

Deux sites, une même raison.

**Digipharmacie** protège son site par un anti-bot (Cloudflare) : un serveur ne peut
pas s'y connecter à votre place.

**OSPHARM** a déplacé son authentification derrière le portail `ophicine.ospharm.org`
(septembre 2026). Là encore, un robot ne peut plus s'y connecter — et c'est une bonne
nouvelle : **plus aucun mot de passe OSPHARM n'a besoin d'être conservé sur nos
serveurs**. D'autant qu'OSPHARM stocke les mots de passe en clair et ne permet pas de
les changer soi-même.

Dans les deux cas, **votre navigateur, lui, est déjà connecté**. L'extension lit les
données *dans votre session*, puis les envoie à break-pharma, qui fait le travail
lourd côté serveur. **Le traitement continue même si vous fermez l'onglet.**

### Ce que l'extension lit chez OSPHARM

*Audit génériques → Mes ventes* (`datastat.ospharm.org`), onglet « PAR PRESENTATION
GENERIQUE » : une ligne par présentation générique, avec laboratoire, quantité, PFHT
unitaire et remise obtenue — sur les douze mois de l'année n-1, celle qu'analyse le
comparateur.

> Pourquoi pas « Analyse des ventes → Toutes les ventes » ? Parce que cette vue est
> cassée chez OSPHARM depuis septembre 2026 : elle s'affiche, mais ne remonte aucune
> ligne. Constaté côté robot sur toutes les périodes, puis confirmé dans le navigateur
> du titulaire.

## Installation (Chrome / Edge / Brave — ordinateur)

1. Téléchargez le dossier `extension/` de ce dépôt sur votre ordinateur.
2. Ouvrez `chrome://extensions` (ou `edge://extensions`).
3. Activez le **Mode développeur** (coin haut-droit).
4. Cliquez **« Charger l'extension non empaquetée »** et sélectionnez le dossier `extension/`.
5. L'icône **break-pharma connect** apparaît dans la barre d'extensions.

## Installation (Firefox — ordinateur)

1. Téléchargez et dézippez le dossier `extension/`.
2. Ouvrez `about:debugging#/runtime/this-firefox`.
3. Cliquez **« Charger un module complémentaire temporaire… »** et sélectionnez le
   fichier **`manifest.json`** du dossier dézippé.
4. L'extension **break-pharma connect** apparaît.

> Note Firefox : une extension chargée ainsi est **temporaire** (retirée à la
> fermeture de Firefox) — rechargez-la de la même façon au besoin. Une version
> signée (installation permanente) pourra être fournie plus tard via addons.mozilla.org.

## Utilisation

1. Cliquez l'icône de l'extension → **connectez-vous** avec vos identifiants break-pharma.fr.
2. Ouvrez [app.digipharmacie.fr](https://app.digipharmacie.fr) et connectez-vous normalement.
3. C'est tout : vos nouvelles factures se **synchronisent automatiquement** (environ une
   fois par jour), sans aucun bouton à cliquer. Une bulle de confirmation apparaît puis
   disparaît. Un bouton **« Synchroniser maintenant »** reste disponible dans le popup.
4. break-pharma analyse les factures en arrière-plan ; vos remises se mettent à jour.

## Ce que l'extension voit / ne voit pas

- Elle lit **uniquement** la liste de vos factures Digipharmacie (`/api/v1/invoices/`)
  et le tableau de vos ventes génériques sur DATASTAT.
- Elle n'enregistre **jamais** vos identifiants Digipharmacie ni OSPHARM.
- Le jeton break-pharma est stocké **localement** dans le navigateur (jamais partagé).
- Les seules destinations réseau autorisées sont `digipharmacie.fr`, `ospharm.org`,
  `break-pharma.fr` et l'API de traitement (voir `manifest.json` → `host_permissions`).

## Note technique — pourquoi deux scripts pour OSPHARM

Les données de DATASTAT vivent dans les composants **Webix** de la page. Un script de
contenu ordinaire s'exécute dans un monde *isolé* : il voit le DOM mais pas les
variables JavaScript de la page, donc pas `webix`. `ospharm_main.js` est donc déclaré
en `"world": "MAIN"` pour lire les données, et `ospharm.js` — qui, lui, a accès à
`chrome.runtime` — les transmet. Ils dialoguent par `window.postMessage`.

## Tests

    node extension/test_ospharm_main.js        # collecteur, contre un faux DATASTAT
    python3 api_scraper/test_ospharm_generic.py  # traitement serveur
