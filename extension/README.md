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

## Utilisation

1. Cliquez l'icône de l'extension → **connectez-vous** avec vos identifiants break-pharma.fr.
2. Ouvrez [app.digipharmacie.fr](https://app.digipharmacie.fr) et connectez-vous normalement.
3. Un bouton **« ⇪ Envoyer à break-pharma »** apparaît en bas à droite. Cliquez dessus.
4. C'est fini : les factures partent en file d'attente, break-pharma les analyse en
   arrière-plan. Vos remises se mettent à jour sur break-pharma.fr.

## Ce que l'extension voit / ne voit pas

- Elle lit **uniquement** la liste de vos factures Digipharmacie (`/api/v1/invoices/`).
- Elle n'enregistre **jamais** vos identifiants Digipharmacie.
- Le jeton break-pharma est stocké **localement** dans le navigateur (jamais partagé).
- Les seules destinations réseau autorisées sont `digipharmacie.fr`, `break-pharma.fr`
  et l'API de traitement (voir `manifest.json` → `host_permissions`).
