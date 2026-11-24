#!/usr/bin/env python3
"""
Exporte TOUTES les conditions labo d'un compte break-pharma en un brief lisible,
destiné à être donné à Claude (ou à un humain) au début d'une conversation.

Pourquoi ce script existe
-------------------------
Les conditions vivent dans `user_state.state_json`, réparties sur quatre niveaux :
conditions de base par année, overrides par mois, exceptions par palier (listes de
libellés) et exceptions par référence et par mois. Les recopier à la main est long
et faux ; les coller brutes en JSON est illisible et pèse des centaines de Ko.
Ce script les met à plat, résout les CIP en libellés, et n'affiche des overrides
mensuels QUE ce qui diffère de la base.

Authentification — jamais d'identifiant dans ce fichier
-------------------------------------------------------
Le dépôt est PUBLIC. Les identifiants viennent de l'environnement :

    export BP_EMAIL="vous@pharmacie.fr"
    export BP_PASSWORD="..."
    python3 export_conditions.py

Le serveur visé est `https://api.break-pharma.fr` ; BP_SUPA_URL le remplace au besoin.
Le script ignore volontairement SUPABASE_URL et SUPABASE_ANON_KEY : ces noms
génériques pointent souvent un ancien projet encore présent dans l'environnement.

ou, pour un accès d'assistance (voir « Assistance » dans CLAUDE.md) :

    export BP_ASSIST_EMAIL="contact@break-pharma.fr"
    export BP_ASSIST_CODE="le-code-affiché-une-fois"
    python3 export_conditions.py

Le fichier produit contient vos conditions commerciales : il ne doit PAS finir
dans le dépôt. Le script refuse d'écrire à l'intérieur du dépôt git (--force
passe outre, à vos risques).

    python3 export_conditions.py --out ~/conditions.md
    python3 export_conditions.py --json --out ~/conditions.json
    python3 export_conditions.py --annee 2026 --labo Biogaran
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date

try:
    import requests
except ImportError:
    sys.exit("Ce script a besoin de « requests » : pip install requests")

# Variables DÉDIÉES (préfixe BP_) et non SUPABASE_URL / SUPABASE_ANON_KEY : ces
# noms génériques traînent souvent dans un environnement, pointant un ancien projet.
# Constaté le 24/11/2026 : SUPABASE_URL valait encore l'ancien projet Supabase,
# supprimé depuis — l'export partait silencieusement vers un serveur mort.
SUPA_URL = os.environ.get("BP_SUPA_URL", "https://api.break-pharma.fr").rstrip("/")
API_URL = os.environ.get("BP_API_URL", "https://pharmacie-remises.onrender.com").rstrip("/")
# Clé anon : publique par nature (elle part déjà dans le navigateur). Ce n'est pas
# un secret — contrairement à la service_role, qui n'a rien à faire ici.
ANON_KEY = os.environ.get(
    "BP_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJyb2xlIjoiYW5vbiIsImlzcyI6InN1cGFiYXNlLXNlbGYiLCJpYXQiOjE3ODM1NDU0MjV9."
    "Ga5ubKMU5mnlcBncdb1TUgprBHxuDkRw0LBmGP81XwM",
)
TIMEOUT = 300


# ── Lecture des nombres ─────────────────────────────────────────────────────
def num(v, default=0.0):
    """Les taux sont stockés en TEXTE et parfois saisis avec une virgule
    (« 7,5 »). parseFloat côté JS s'y arrêtait et lisait 7 — ne pas refaire
    l'erreur ici."""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace(",", "."))
    except ValueError:
        return default


def fnum(v, suffix=""):
    """Formate un taux sans décimale inutile : 2.5 → « 2,5 », 20.0 → « 20 »."""
    if v in (None, ""):
        return "—"
    f = num(v)
    s = f"{f:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",") + suffix


def feur(v):
    return f"{num(v):,.0f} €".replace(",", " ")


# ── Authentification ────────────────────────────────────────────────────────
def login():
    """Renvoie (jeton, user_id). Mot de passe normal si BP_EMAIL/BP_PASSWORD,
    sinon code d'assistance."""
    email, pwd = os.environ.get("BP_EMAIL"), os.environ.get("BP_PASSWORD")
    if email and pwd:
        r = requests.post(
            f"{SUPA_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": ANON_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": pwd}, timeout=TIMEOUT)
        if r.status_code != 200:
            sys.exit(f"Connexion refusée ({r.status_code}) : {r.text[:200]}")
        d = r.json()
        return d["access_token"], (d.get("user") or {}).get("id")

    a_email = os.environ.get("BP_ASSIST_EMAIL")
    a_code = os.environ.get("BP_ASSIST_CODE")
    if a_email and a_code:
        r = requests.post(f"{API_URL}/assist/login",
                          json={"email": a_email, "password": a_code}, timeout=TIMEOUT)
        if r.status_code != 200:
            sys.exit(f"Accès d'assistance refusé ({r.status_code}) : {r.text[:200]}")
        d = r.json()
        tok = d.get("access_token")
        if not tok:
            sys.exit("Réponse d'assistance sans jeton.")
        me = requests.get(f"{SUPA_URL}/auth/v1/user",
                          headers={"apikey": ANON_KEY, "Authorization": f"Bearer {tok}"},
                          timeout=TIMEOUT)
        return tok, (me.json() or {}).get("id") if me.status_code == 200 else None

    sys.exit("Aucun identifiant. Renseignez BP_EMAIL + BP_PASSWORD, "
             "ou BP_ASSIST_EMAIL + BP_ASSIST_CODE (voir l'en-tête du script).")


def fetch_state(token, user_id):
    h = {"apikey": ANON_KEY, "Authorization": f"Bearer {token}"}
    params = {"select": "state_json"}
    if user_id:
        params["user_id"] = f"eq.{user_id}"
    r = requests.get(f"{SUPA_URL}/rest/v1/user_state", params=params, headers=h, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        sys.exit("Aucun état trouvé pour ce compte.")
    return rows[0]["state_json"]


def fetch_libelles(token, cips):
    """CIP → libellé. Sans cela les exceptions par référence sont illisibles."""
    out = {}
    cl = sorted({c for c in cips if c})
    if not cl:
        return out
    h = {"apikey": ANON_KEY, "Authorization": f"Bearer {token}"}
    for i in range(0, len(cl), 80):          # URL courte : PostgREST n'aime pas les in.() géants
        batch = cl[i:i + 80]
        try:
            r = requests.get(f"{SUPA_URL}/rest/v1/references_pharmacie",
                             params={"cip13": f"in.({','.join(batch)})",
                                     "select": "cip13,libelle,labo"},
                             headers=h, timeout=TIMEOUT)
            if r.status_code == 200:
                for x in r.json():
                    if x.get("libelle") and x["cip13"] not in out:
                        out[x["cip13"]] = x["libelle"]
        except requests.RequestException:
            pass
    return out


# ── Mise à plat ─────────────────────────────────────────────────────────────
COND_FIELDS = ("caCondition", "remiseGlobale", "coopR3", "showR3", "forceFirst")


def cond_signature(c):
    """Empreinte comparable d'une condition, pour ne montrer d'un override
    mensuel que ce qui CHANGE réellement par rapport à la base."""
    rv = {str(r.get("rsf_pct")): (str(r.get("remise2")), str(r.get("remise3")),
                                  bool(r.get("excluded")), bool(r.get("hasExcept")))
          for r in (c.get("rsfValues") or [])}
    exc = {k: (str(v.get("remise2")), str(v.get("remise3")),
               tuple(sorted(l.get("text", "") for l in (v.get("libelles") or []))))
           for k, v in (c.get("remise2Except") or {}).items()}
    return {"base": {f: c.get(f) for f in COND_FIELDS}, "paliers": rv, "exceptions": exc}


def diff_conditions(base_list, over_list):
    """Différences lisibles entre la liste de base et un override mensuel."""
    out = []
    if len(base_list) != len(over_list):
        out.append(f"nombre de conditions : {len(base_list)} → {len(over_list)}")
    for i, oc in enumerate(over_list):
        bc = base_list[i] if i < len(base_list) else {}
        bs, os_ = cond_signature(bc), cond_signature(oc)
        for f in COND_FIELDS:
            if bs["base"].get(f) != os_["base"].get(f):
                out.append(f"condition {i + 1} · {f} : {bs['base'].get(f)} → {os_['base'].get(f)}")
        for pal in sorted(set(bs["paliers"]) | set(os_["paliers"]), key=lambda x: num(x)):
            b, o = bs["paliers"].get(pal), os_["paliers"].get(pal)
            if b != o:
                if b is None:
                    out.append(f"condition {i + 1} · palier {pal} ajouté")
                elif o is None:
                    out.append(f"condition {i + 1} · palier {pal} retiré")
                else:
                    if b[0] != o[0]:
                        out.append(f"condition {i + 1} · palier {pal} · RDP {b[0]} → {o[0]}")
                    if b[1] != o[1]:
                        out.append(f"condition {i + 1} · palier {pal} · coop {b[1]} → {o[1]}")
                    if b[2] != o[2]:
                        out.append(f"condition {i + 1} · palier {pal} · exclu {b[2]} → {o[2]}")
        for k in sorted(set(bs["exceptions"]) | set(os_["exceptions"]), key=lambda x: num(x)):
            if bs["exceptions"].get(k) != os_["exceptions"].get(k):
                out.append(f"condition {i + 1} · exception du palier {k} modifiée")
    return out


def eff_exceptions(lp, year, month):
    """Liste en vigueur au mois donné : un mois sans clé HÉRITE du dernier mois
    antérieur qui en a une (même règle que _refExcEffective côté front)."""
    mm = (lp.get("refExceptionsByMonth") or {}).get(str(year)) or {}
    best, bm = [], -1
    for k, v in mm.items():
        try:
            m = int(k)
        except (TypeError, ValueError):
            continue
        if m <= month and m > bm:
            bm, best = m, v
    return best or []


# ── Rendu Markdown ──────────────────────────────────────────────────────────
def render(state, libs, annees, labo_filter):
    L = []
    w = L.append
    today = date.today().isoformat()
    labs = [p for p in (state.get("labPages") or []) if p.get("selectedLab")]
    if labo_filter:
        want = labo_filter.lower()
        labs = [p for p in labs if p["selectedLab"].lower() == want]
        if not labs:
            sys.exit(f"Laboratoire « {labo_filter} » absent de ce compte.")

    w("# Conditions laboratoires — break-pharma")
    w("")
    w(f"Export du {today}. Années couvertes : {', '.join(str(a) for a in annees)}.")
    w("")
    w("> À donner à Claude en début de conversation. Les taux sont **négatifs** pour les")
    w("> paliers RSF (convention de la base : `rsf_pct = -20` = palier 20 %) et positifs")
    w("> pour les remises. « RDP » = remise 2 (remise différée de performance),")
    w("> « coop » = remise 3 (coopération commerciale).")
    w("")

    w("## Vue d'ensemble")
    w("")
    w("| Labo | Contrat 2026 | Contrat 2025 | Répartiteur | CA cible | Remise globale | Coop |")
    w("|---|:-:|:-:|:-:|---:|---:|---:|")
    for lp in labs:
        c26 = (lp.get("conditions") or [{}])
        camax = max([num(c.get("caCondition")) for c in c26] or [0])
        rg = max([num(c.get("remiseGlobale")) for c in c26] or [0])
        coop = max([num(r.get("remise3")) for c in c26 for r in (c.get("rsfValues") or [])] or [0])
        sg = lambda f, d: ("✅ " + (lp.get(d) or "")) if lp.get(f) else "—"
        w(f"| **{lp['selectedLab']}** | {sg('contractSigned', 'contractDate')} "
          f"| {sg('contractSigned25', 'contractDate25')} "
          f"| {'oui' if lp.get('contratRepartiteur') else 'non'} "
          f"| {feur(camax) if camax else '—'} | {fnum(rg, ' %') if rg else '—'} "
          f"| {fnum(coop, ' %') if coop else '—'} |")
    w("")

    for lp in labs:
        lab = lp["selectedLab"]
        w("---")
        w("")
        w(f"## {lab}")
        w("")
        for year in annees:
            key = "conditions" if year != 2025 else "conditions2025"
            conds = lp.get(key) or []
            if not conds:
                continue
            signed = lp.get("contractSigned" if year != 2025 else "contractSigned25")
            date_c = lp.get("contractDate" if year != 2025 else "contractDate25")
            w(f"### {lab} — {year}")
            w("")
            w(f"Contrat direct signé : **{'oui' if signed else 'non'}**"
              + (f" (depuis le {date_c})" if signed and date_c else "")
              + f" · contrat répartiteur : "
                f"**{'oui' if lp.get('contratRepartiteur' if year != 2025 else 'contratRepartiteur25') else 'non'}**")
            w("")
            # Beaucoup de labos déclinent le MÊME tableau de paliers sur plusieurs
            # seuils de CA (Viatris : 14 conditions, une seule grille ; EG : 6 pour
            # une ; Teva : 3 pour une). L'échelle se joue alors sur la remise globale,
            # pas sur la grille. On imprime donc la grille UNE fois, suivie des seuils
            # qui la partagent — sinon le brief est illisible et pèse quatre fois plus.
            groupes = {}
            for c in conds:
                sig = json.dumps(
                    {"paliers": cond_signature(c)["paliers"],
                     "exceptions": cond_signature(c)["exceptions"],
                     "rsfOnly": sorted(l.get("text", "")
                                       for l in ((c.get("rsfOnly") or {}).get("libelles") or []))
                                if (c.get("rsfOnly") or {}).get("enabled") else None},
                    ensure_ascii=False, sort_keys=True)
                groupes.setdefault(sig, []).append(c)

            for gi, groupe in enumerate(groupes.values(), 1):
                c = groupe[0]
                if len(groupe) > 1:
                    w(f"**Grille {gi}** — partagée par {len(groupe)} seuils de CA :")
                    w("")
                    w("| CA cible | Remise globale | Coop R3 | RSF first |")
                    w("|---:|---:|---:|:-:|")
                    for g in sorted(groupe, key=lambda x: -num(x.get("caCondition"))):
                        w(f"| {feur(g.get('caCondition')) if num(g.get('caCondition')) else '_sans seuil_'} "
                          f"| {fnum(g.get('remiseGlobale'), ' %')} "
                          f"| {fnum(g.get('coopR3'), ' %') if num(g.get('coopR3')) else '—'} "
                          f"| {'✓' if g.get('forceFirst') else ''} |")
                    w("")
                else:
                    head = [f"CA cible **{feur(c.get('caCondition'))}**" if num(c.get("caCondition"))
                            else "sans CA cible"]
                    if c.get("remiseGlobale") not in (None, ""):
                        head.append(f"remise globale {fnum(c.get('remiseGlobale'), ' %')}")
                    if num(c.get("coopR3")):
                        head.append(f"coopR3 {fnum(c.get('coopR3'), ' %')}")
                    if c.get("forceFirst"):
                        head.append("**RSF first forcé**")
                    w(f"**Condition {gi}** — " + " · ".join(head))
                    w("")

                rv = sorted(c.get("rsfValues") or [], key=lambda r: num(r.get("rsf_pct")))
                if rv:
                    vide = any(r.get("remise2") in (None, "") and not r.get("excluded") for r in rv)
                    w("| Palier RSF | RDP (remise 2) | Coop (remise 3) | Exclu | Exception | RDP manuelle |")
                    w("|---:|---:|---:|:-:|:-:|:-:|")
                    for r in rv:
                        r2 = r.get("remise2")
                        # Une RDP VIDE n'est pas zéro : elle est calculée depuis la
                        # remise globale (remiseGlobale − |RSF|), sauf palier exclu.
                        # C'est par là que joue l'échelle des seuils quand la grille
                        # est commune à plusieurs CA cibles.
                        note = ""
                        if r2 in (None, "") and not r.get("excluded"):
                            note = " _(calculée depuis la remise globale)_" if len(groupe) > 1 else (
                                f" _(calculée : {fnum(max(0, num(c.get('remiseGlobale')) - abs(num(r.get('rsf_pct')))))})_"
                                if c.get("remiseGlobale") not in (None, "") else "")
                        w(f"| {fnum(r.get('rsf_pct'), ' %')} | "
                          f"{'—' if r2 in (None, '') else fnum(r2, ' %')}{note} | "
                          f"{'—' if r.get('remise3') in (None, '') else fnum(r.get('remise3'), ' %')} | "
                          f"{'✓' if r.get('excluded') else ''} | {'✓' if r.get('hasExcept') else ''} | "
                          f"{'✓' if r.get('remise2Manual') else ''} |")
                    w("")
                    if vide and len(groupe) > 1:
                        w("> RDP « — » = **calculée**, pas nulle : `remise globale − |palier RSF|`,")
                        w("> plancher à 0, et toujours 0 sur un palier exclu. C'est ce qui fait")
                        w("> varier la RDP d'un seuil de CA à l'autre à grille identique.")
                        w("")
                for pal, e in sorted((c.get("remise2Except") or {}).items(), key=lambda x: num(x[0])):
                    libelles = [l.get("text", "") for l in (e.get("libelles") or [])]
                    w(f"- Exception du palier **{fnum(pal, ' %')}** → RDP **{fnum(e.get('remise2'), ' %')}**"
                      + (f", coop {fnum(e.get('remise3'), ' %')}" if e.get("remise3") not in (None, "") else "")
                      + f" sur {len(libelles)} référence(s) :")
                    for lb in libelles:
                        w(f"  - {lb}")
                ro = c.get("rsfOnly") or {}
                if ro.get("enabled"):
                    libelles = [l.get("text", "") for l in (ro.get("libelles") or [])]
                    w(f"- **RSF seule** (aucune RDP ni coop) sur {len(libelles)} référence(s) :")
                    for lb in libelles:
                        w(f"  - {lb}")
                w("")

            # Overrides mensuels : uniquement les différences
            mm = (lp.get("condsByMonth") or {}).get(str(year)) or {}
            if mm:
                w(f"**Overrides mensuels {year}** — mois porteurs : "
                  + ", ".join(sorted(mm, key=lambda x: int(x))))
                w("")
                prev = None
                for m in sorted(mm, key=lambda x: int(x)):
                    d = diff_conditions(conds, mm[m] or [])
                    if not d:
                        continue
                    sig = json.dumps(d, ensure_ascii=False)
                    if sig == prev:                     # mois identique au précédent : on ne répète pas
                        w(f"- mois {m} : identique au mois précédent")
                        continue
                    prev = sig
                    w(f"- **mois {m}** par rapport à la base :")
                    for x in d:
                        w(f"  - {x}")
                w("")

            # Exceptions par référence : l'état en vigueur en décembre, puis les bascules
            mmx = (lp.get("refExceptionsByMonth") or {}).get(str(year)) or {}
            if mmx:
                dec = eff_exceptions(lp, year, 12)
                w(f"**Exceptions par référence {year}** — {len(dec)} en vigueur en décembre "
                  f"(mois porteurs : {', '.join(sorted(mmx, key=lambda x: int(x)))})")
                w("")
                if dec:
                    w("| CIP | Référence | Palier forcé | RDP | Coop |")
                    w("|---|---|---:|---:|---:|")
                    for e in dec:
                        cip = e.get("cip13") or ""
                        w(f"| {cip} | {libs.get(cip, '_(libellé inconnu)_')} | "
                          f"{fnum(e.get('rsfPct'), ' %') if e.get('rsfPct') is not None else '—'} | "
                          f"{fnum(e.get('remise2'), ' %') if e.get('remise2') not in (None, '') else '—'} | "
                          f"{fnum(e.get('remise3'), ' %') if e.get('remise3') not in (None, '') else '—'} |")
                    w("")
                # Bascules en cours d'année : ce qui change d'un mois porteur au suivant
                mois = sorted((int(k) for k in mmx), key=int)
                for a, b in zip(mois, mois[1:]):
                    ea = {x.get("cip13"): x for x in eff_exceptions(lp, year, a)}
                    eb = {x.get("cip13"): x for x in eff_exceptions(lp, year, b)}
                    lignes = []
                    for cip in sorted(set(ea) | set(eb)):
                        if cip not in eb:
                            lignes.append(f"{cip} retirée")
                        elif cip not in ea:
                            lignes.append(f"{cip} ajoutée")
                        elif (ea[cip].get("rsfPct"), ea[cip].get("remise2"), ea[cip].get("remise3")) != \
                             (eb[cip].get("rsfPct"), eb[cip].get("remise2"), eb[cip].get("remise3")):
                            lignes.append(
                                f"{cip} : palier {fnum(ea[cip].get('rsfPct'))}→{fnum(eb[cip].get('rsfPct'))}, "
                                f"RDP {fnum(ea[cip].get('remise2'))}→{fnum(eb[cip].get('remise2'))}")
                    if lignes:
                        w(f"- bascule mois {a} → {b} : " + " ; ".join(lignes))
                w("")

    # Règles de lecture : elles ne se déduisent PAS des tableaux ci-dessus, et les
    # ignorer conduit à des calculs faux. Toutes ont été vérifiées sur données
    # réelles (voir CLAUDE.md du dépôt).
    w("---")
    w("")
    w("## Comment lire ces conditions")
    w("")
    w("1. **Une RDP vide n'est pas une RDP nulle.** Si `remise2` est vide et que le palier")
    w("   n'est pas exclu, la RDP vaut `remise globale − |palier RSF|`, plancher à 0.")
    w("2. **« Exclu » n'exclut QUE la RDP.** Le RSF et la coop restent dus sur un palier")
    w("   exclu. Ne jamais filtrer la remise 3 sur ce drapeau.")
    w("3. **Les exceptions par référence priment sur tout**, puis vient une RDP de palier")
    w("   saisie à la main (`RDP manuelle`), puis le barème de la CGV.")
    w("4. **Les overrides mensuels remplacent la base pour le mois concerné**, et un mois")
    w("   sans override hérite du dernier mois antérieur qui en a un. Même règle pour les")
    w("   exceptions par référence.")
    w("5. **Le barème suit l'année du mois d'achat** : un mois de 2025 se paie au barème")
    w("   2025, même réglé en 2026.")
    w("6. **La clause des 80 % (ni RDP ni coop en dessous) est propre au contrat coop")
    w("   Biogaran.** Ne pas l'étendre aux autres labos : leurs seuils de CA chiffrent")
    w("   déjà la condition de volume.")
    w("7. **La réalisation se mesure depuis la date de signature**, pas sur l'année civile,")
    w("   avec un CA cible proratisé sur les mois couverts.")
    w("8. Les remises portent sur les **achats** (justificatif répartiteur + factures")
    w("   directes), jamais sur les ventes.")
    w("")
    w("_Export produit par `export_conditions.py` — relancer le script après toute")
    w("modification des conditions dans l'application._")
    return "\n".join(L).rstrip() + "\n"


# ── Garde-fou : ne pas écrire dans un dépôt git ─────────────────────────────
def inside_repo(path):
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True,
                              cwd=os.path.dirname(os.path.abspath(path)) or ".").stdout.strip()
        return bool(root) and os.path.abspath(path).startswith(root + os.sep)
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Exporte les conditions labo break-pharma.")
    ap.add_argument("--out", default=os.path.expanduser(f"~/conditions_labos_{date.today()}.md"),
                    help="fichier de sortie (défaut : ~/conditions_labos_AAAA-MM-JJ.md)")
    ap.add_argument("--json", action="store_true", help="sortie JSON brute au lieu du Markdown")
    ap.add_argument("--annee", type=int, action="append",
                    help="limiter à une année (répétable) ; défaut : 2025 et 2026")
    ap.add_argument("--labo", help="limiter à un laboratoire")
    ap.add_argument("--force", action="store_true",
                    help="autoriser l'écriture à l'intérieur d'un dépôt git")
    args = ap.parse_args()

    if inside_repo(args.out) and not args.force:
        sys.exit(f"Refus d'écrire dans un dépôt git : {args.out}\n"
                 "Ce fichier contient vos conditions commerciales et le dépôt est public.\n"
                 "Choisissez un chemin hors du dépôt (--out ~/…) ou passez --force.")

    token, user_id = login()
    state = fetch_state(token, user_id)
    annees = sorted(set(args.annee or [2025, 2026]))

    labs = [p for p in (state.get("labPages") or []) if p.get("selectedLab")]
    if args.json:
        payload = {"exporte_le": date.today().isoformat(), "annees": annees, "labPages": labs}
        out = json.dumps(payload, ensure_ascii=False, indent=1)
    else:
        cips = {e.get("cip13")
                for lp in labs
                for mm in [(lp.get("refExceptionsByMonth") or {})]
                for y in mm.values()
                for lst in (y or {}).values()
                for e in (lst or [])}
        out = render(state, fetch_libelles(token, cips), annees, args.labo)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(out)
    n_cond = sum(len(p.get("conditions") or []) + len(p.get("conditions2025") or []) for p in labs)
    print(f"✓ {len(labs)} laboratoires, {n_cond} conditions → {args.out} "
          f"({len(out):,} caractères)".replace(",", " "))
    print("  Ce fichier contient vos conditions commerciales : ne le versionnez pas.")


if __name__ == "__main__":
    main()
