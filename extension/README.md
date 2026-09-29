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
2. **Renommez `manifest.firefox.json` en `manifest.json`** (en écrasant l'existant).
3. Ouvrez `about:debugging#/runtime/this-firefox`.
4. Cliquez **« Charger un module complémentaire temporaire… »** et sélectionnez le
   fichier **`manifest.json`** du dossier dézippé.
5. L'extension **break-pharma connect** apparaît.

> Pourquoi deux manifestes ? Chrome exige `background.service_worker` et refuse
> `background.scripts` (« requires manifest version 2 or lower »), alors que Firefox
> n'active pas encore les service workers d'arrière-plan par défaut et attend
> `background.scripts`. Un seul fichier ne peut pas satisfaire les deux ; le
> `manifest.json` du dépôt vise Chrome, `manifest.firefox.json` vise Firefox.

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
5. Pour OSPHARM : ouvrez [datastat.ospharm.org](https://datastat.ospharm.org) (connecté
   via le portail ophicine). Une bulle apparaît en bas à droite et vous dit ce qui se
   passe. La lecture des douze mois prend une dizaine de minutes. Le popup propose aussi
   **« Lire mes ventes OSPHARM »**, qui force une lecture immédiate.

## Dépannage — rien ne s'affiche sur DATASTAT

La bulle en bas à droite doit **toujours** apparaître : elle dit soit ce qu'elle fait,
soit pourquoi elle ne fait rien. Si vous ne voyez rien du tout :

| Ce qui s'affiche | Ce que ça veut dire |
|---|---|
| « connectez-vous… » | L'extension n'a pas de jeton break-pharma : ouvrez son popup et connectez-vous. |
| « déjà synchronisées il y a N h » | Verrou de 20 h. **Cliquez la bulle** pour relancer tout de suite. |
| « le collecteur ne s'est pas chargé » | Rechargez DATASTAT avec Ctrl+Maj+R. Le monde `MAIN` exige Chrome 111 ou plus. |
| *aucune bulle* | Le script de contenu n'est pas injecté : rechargez l'extension dans `chrome://extensions`, puis rechargez l'onglet DATASTAT. |

La console du navigateur (F12) trace chaque décision sous l'étiquette `[bp-ospharm]`.

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

    node extension/test_ospharm_main.js          # collecteur, contre un faux DATASTAT
    node extension/test_ospharm_bridge.js        # pont : aucun abandon silencieux
    python3 api_scraper/test_ospharm_generic.py  # traitement serveur
