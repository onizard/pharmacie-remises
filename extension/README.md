# break-pharma connect — extension navigateur

Récupère vos factures/avoirs **Digipharmacie** et les envoie vers **break-pharma.fr**,
en un clic, depuis votre propre session Digipharmacie.

## Pourquoi une extension ?

Digipharmacie protège son site par un anti-bot (Cloudflare) : un serveur ne peut pas
s'y connecter à votre place. En revanche, **votre navigateur, lui, est déjà connecté**.
L'extension lit la liste de vos factures via l'API Digipharmacie *dans votre session*
(cookies inclus), puis l'envoie à break-pharma, qui télécharge et analyse chaque PDF
côté serveur. Le traitement continue même si vous fermez l'onglet.

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

- Elle lit **uniquement** la liste de vos factures Digipharmacie (`/api/v1/invoices/`).
- Elle n'enregistre **jamais** vos identifiants Digipharmacie.
- Le jeton break-pharma est stocké **localement** dans le navigateur (jamais partagé).
- Les seules destinations réseau autorisées sont `digipharmacie.fr`, `break-pharma.fr`
  et l'API de traitement (voir `manifest.json` → `host_permissions`).
