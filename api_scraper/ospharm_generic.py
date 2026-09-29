"""
ospharm_generic.py — Traitement des ventes génériques OSPHARM capturées par
l'extension navigateur, depuis « Audit génériques → Mes ventes ».

POURQUOI CETTE SOURCE
---------------------
« Analyse des ventes → Toutes les ventes », que scrapait run_job_ospharm.py, est
cassée chez OSPHARM : la vue se charge, les colonnes sont là, et aucune ligne ne
remonte (constaté côté robot sur toutes les périodes, puis confirmé par le
titulaire dans son propre navigateur, 23/09/2026). OSPHARM a par ailleurs déplacé
son authentification derrière le portail ophicine.ospharm.org.

La vue « Audit génériques → Mes ventes » (#!/top/generic.sellout), elle,
fonctionne. Sa table `dt_sellout_resume_productname` porte le détail par
présentation générique — 459 lignes sur un mois — avec le laboratoire, la
quantité, le PFHT unitaire et LA REMISE RÉELLEMENT OBTENUE.

C'est l'extension navigateur qui la lit, dans la session de l'utilisateur, et la
poste ici : aucun mot de passe OSPHARM n'a plus besoin d'être stocké côté serveur
— ce qui referme définitivement le risque d'une fuite d'identifiant, et rend
indifférent le fait qu'OSPHARM conserve les mots de passe en clair sans permettre
de les changer.

DIFFÉRENCE DE SENS À CONNAÎTRE
------------------------------
L'ancienne source donnait le CA de vente tous produits, et les remises étaient
RECALCULÉES depuis les conditions labo enregistrées (RSF théorique + remise 2).
Cette source donne le périmètre GÉNÉRIQUE et la remise SUR FACTURE réellement
appliquée. Elle est donc plus fidèle au réel, mais elle ne contient pas la
remise différée (RDP) : `r2_ca` et `r2_pond_pct` valent 0 ici, et le vérificateur
reste la seule source pour la remise 2.
"""

import re
import unicodedata

# Normalisation des raisons sociales vers les noms courts utilisés partout
# ailleurs dans l'application (conditions labo, rsf_history, justificatif CERP).
_LABO_MAP = [
    ("biogaran",   "BIOGARAN"),
    ("arrow",      "ARROW"),
    ("teva",       "TEVA"),
    ("ratiopharm", "TEVA"),
    ("viatris",    "VIATRIS"),
    ("mylan",      "VIATRIS"),
    ("sandoz",     "SANDOZ"),
    ("zentiva",    "ZENTIVA"),
    ("zydus",      "ZYDUS"),
    ("cristers",   "CRISTERS"),
    ("eg labo",    "EG LABO"),
    ("eg ",        "EG LABO"),
    ("evolupharm", "EVOLUPHARM"),
    ("almus",      "ALMUS"),
    ("aurobindo",  "AUROBINDO"),
]

# Champs susceptibles de porter un code produit, par ordre de préférence.
# On ne sait pas encore si la table OSPHARM en expose un : l'extension enverra
# les lignes telles quelles, et on prend le premier qui ressemble à un CIP13.
_CIP_KEYS = ("cip13", "cip", "code_ean", "codeean", "ean13", "ean",
             "code_cip", "cip7", "product_code")


def norm_labo(raw) -> str:
    """« ARROW GENERIQUES » → « ARROW », « TEVA SANTE » → « TEVA »…"""
    s = str(raw or "").strip()
    low = s.lower()
    for kw, canon in _LABO_MAP:
        if kw in low:
            return canon
    return s.upper()


def _num(v) -> float:
    """Parse un nombre depuis les formats OSPHARM : 2.99, « 2,99 », « 14 290 € »,
    « 5.3819999999999997 ». Renvoie 0.0 si illisible."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"<[^>]+>", "", str(v))              # balises éventuelles
    s = s.replace(" ", "").replace(" ", "").replace(" ", "")
    s = s.replace("€", "").replace("%", "").strip()
    s = s.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else 0.0


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def extract_cip(row: dict) -> str:
    """Cherche un CIP13 dans la ligne, quel que soit le nom du champ.
    Renvoie "" si aucun code plausible — auquel cas la résolution se fera par
    libellé (cf. resolve_cips)."""
    for k in _CIP_KEYS:
        if k in row:
            d = _digits(row[k])
            if len(d) == 13:
                return d
            if len(d) == 7:                          # CIP7 historique
                return ("3400" + d.zfill(9))[:13]
    # Balayage large : un champ dont le nom contient cip/ean et la valeur 13 chiffres
    for k, v in row.items():
        kl = k.lower()
        if "cip" in kl or "ean" in kl:
            d = _digits(v)
            if len(d) == 13:
                return d
    return ""


def presentation_key(row: dict) -> str:
    """Clé stable d'une présentation : « ACEBUTOLOL 200 mg|30.0|BIOGARAN ».
    Sert de repli quand aucun code produit n'est disponible, et de clé de
    rapprochement avec le catalogue references_pharmacie."""
    name = str(row.get("product_name") or row.get("groupe_gener") or "").strip()
    pack = str(row.get("package") or "").strip()
    labo = norm_labo(row.get("manufacturer_name"))
    return f"{name}|{pack}|{labo}"


def normalize_rows(raw_rows, year: int, month: int) -> list:
    """Lignes brutes de dt_sellout_resume_productname → lignes normalisées.

    Une ligne de sortie porte tout ce dont l'application a besoin :
      cip13  (éventuellement vide, à résoudre plus tard)
      key    présentation|conditionnement|labo
      labo   nom court
      qty    quantité vendue
      puht   PFHT unitaire (prix tarif, avant remise)
      net    total net remisé HT
      rem_pct taux de remise sur facture
      year, month
    """
    out = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        qty = _num(r.get("quantity"))
        if qty <= 0:
            continue
        puht = _num(r.get("sellin_price_ht"))
        net  = _num(r.get("sum_sellin_ht"))
        # Repli : si le net n'est pas fourni, on le reconstruit depuis le prix
        # unitaire remisé, puis depuis le taux de remise.
        if net <= 0:
            unit_net = _num(r.get("product_discount_price_ht"))
            if unit_net > 0:
                net = unit_net * qty
            else:
                net = puht * qty * (1 - _num(r.get("product_discount")) / 100.0)
        out.append({
            "cip13":   extract_cip(r),
            "key":     presentation_key(r),
            "labo":    norm_labo(r.get("manufacturer_name")),
            "libelle": str(r.get("product_name") or "").strip(),
            "package": str(r.get("package") or "").strip(),
            "qty":     int(round(qty)),
            "puht":    round(puht, 4),
            "net":     round(net, 2),
            "rem_pct": round(_num(r.get("product_discount")), 2),
            "year":    int(year),
            "month":   int(month),
        })
    return out


def build_month_stats(rows: list) -> dict:
    """Agrège par (mois, labo) → même forme que _build_month_stats du scraper,
    pour que le front n'ait rien à changer.

    ca_brut       Σ PFHT × quantité        (prix tarif, avant remise)
    remise_totale ca_brut − Σ net          (remise sur facture réellement obtenue)
    pa_net        Σ net
    pond_pct      remise_totale / ca_brut

    rsf_* reprend ces mêmes valeurs : la remise de cette source EST la remise sur
    facture. r2_* reste à zéro — la remise différée n'apparaît pas ici, elle est
    du ressort du vérificateur.
    """
    by = {}
    for r in rows:
        labo = r.get("labo") or ""
        if not labo:
            continue
        k = (r["year"], r["month"], labo)
        a = by.setdefault(k, {"qty": 0, "ca": 0.0, "net": 0.0})
        a["qty"] += r["qty"]
        a["ca"]  += r["puht"] * r["qty"]
        a["net"] += r["net"]

    result = {}
    for (year, month, labo), v in by.items():
        mk = f"{year}-{month:02d}"
        ca_b = round(v["ca"], 2)
        net  = round(v["net"], 2)
        rem  = round(ca_b - net, 2)
        pond = round(rem / ca_b * 100, 2) if ca_b > 0 else 0.0
        result.setdefault(mk, []).append({
            "labo":          labo,
            "qty":           int(v["qty"]),
            "ca_brut":       ca_b,
            "pond_pct":      pond,
            "rsf_pond_pct":  pond,
            "rsf_ca":        ca_b,
            "r2_pond_pct":   0.0,
            "r2_ca":         0.0,
            "remise_totale": rem,
            "pa_net":        net,
        })
    for mk in result:
        result[mk].sort(key=lambda x: x["ca_brut"], reverse=True)
    return result


def compact_rows(rows: list) -> list:
    """Lignes compactes pour ospharm_job.rows, consommées par le comparateur.
    On garde `key` en plus du cip13 : sans lui, une ligne non résolue serait
    définitivement perdue, alors qu'un import de catalogue ultérieur pourrait
    la rattacher."""
    return [{"cip13": r["cip13"], "key": r["key"], "qty": r["qty"],
             "puht": r["puht"], "year": r["year"], "month": r["month"]}
            for r in rows]


_STOP = re.compile(r"[^A-Z0-9]+")


def _libelle_tokens(s: str) -> set:
    """Jeu de mots normalisés d'un libellé, pour le rapprochement au catalogue.

    On sépare TOUJOURS chiffres et lettres : OSPHARM écrit « ACEBUTOLOL 200 mg »
    là où le catalogue écrit « ACEBUTOLOL 200MG 30 CP ». Sans cette séparation,
    « 200MG » et « 200 » + « MG » ne se rencontrent jamais et aucun rapprochement
    n'aboutit.
      « ACEBUTOLOL 200 mg »        → {ACEBUTOLOL, 200, MG}
      « ACEBUTOLOL 200MG 30 CP »   → {ACEBUTOLOL, 200, MG, 30, CP}
    """
    s = unicodedata.normalize("NFD", str(s or "").upper())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"(\d)([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z])(\d)", r"\1 \2", s)
    return {t for t in _STOP.split(s) if t}


def resolve_cips(rows: list, catalog: list) -> dict:
    """Rattache un cip13 aux lignes qui n'en ont pas, via le catalogue
    references_pharmacie (cip13, labo, libelle).

    Règle : même laboratoire, et tous les mots du libellé OSPHARM présents dans
    le libellé catalogue. On n'accepte QUE les rapprochements sans ambiguïté —
    un libellé qui correspond à plusieurs références du même labo est laissé non
    résolu plutôt que rattaché au hasard : une erreur d'affectation fausserait
    les remises, donc de l'argent.

    Renvoie un compte-rendu {total, deja, resolus, ambigus, echecs}.
    """
    by_labo = {}
    for c in (catalog or []):
        lab = norm_labo(c.get("labo"))
        by_labo.setdefault(lab, []).append(c)

    rep = {"total": len(rows), "deja": 0, "resolus": 0, "ambigus": 0, "echecs": 0}
    for r in rows:
        if r.get("cip13"):
            rep["deja"] += 1
            continue
        want = _libelle_tokens(r.get("libelle"))
        if not want:
            rep["echecs"] += 1
            continue
        # Le conditionnement lève l'essentiel des ambiguïtés : « ACEBUTOLOL
        # 200 mg » existe en boîte de 30 et de 90, deux références distinctes du
        # même laboratoire. Le catalogue les distingue justement par ce nombre
        # (« … 30 CP » / « … 90 CP »).
        pack = _digits(str(r.get("package") or "").split(".")[0])
        if pack:
            want = want | {pack}
        hits = []
        for c in by_labo.get(r.get("labo") or "", []):
            if want <= _libelle_tokens(c.get("libelle")):
                hits.append(c)
        uniq = {str(h.get("cip13")) for h in hits}
        if len(uniq) == 1:
            r["cip13"] = uniq.pop()
            rep["resolus"] += 1
            continue
        if not uniq:
            rep["echecs"] += 1
            continue

        # PLUSIEURS CANDIDATS. Le catalogue contient des libellés strictement
        # identiques sous deux codes — 15,5 % des références Biogaran/Arrow — et
        # ces jumeaux ne sont PAS interchangeables : « ACEBUTOLOL 200MG 30 CPR »
        # existe à 5 € / RSF −2,5 % et à 3 € / RSF −20 %. Choisir au hasard
        # déplacerait de l'argent.
        #
        # Le PFHT d'OSPHARM tranche : c'est le même prix tarif des deux côtés.
        # On ne retient le plus proche que si l'écart est FRANC — moins de 5 %
        # d'écart, et au moins deux fois mieux que le suivant. Sinon on laisse
        # non résolu : une ligne sans code se rattrapera, une ligne mal
        # rattachée fausserait silencieusement les remises.
        ref_puht = float(r.get("puht") or 0)
        scored = []
        for h in hits:
            try:
                cp = float(h.get("puht") or 0)
            except (TypeError, ValueError):
                cp = 0.0
            if cp > 0 and ref_puht > 0:
                scored.append((abs(cp - ref_puht) / ref_puht, str(h.get("cip13"))))
        scored.sort()
        if (len(scored) >= 2 and scored[0][0] <= 0.05
                and scored[0][0] * 2 <= scored[1][0]):
            r["cip13"] = scored[0][1]
            rep["resolus"] += 1
            rep["par_prix"] = rep.get("par_prix", 0) + 1
        else:
            rep["ambigus"] += 1
    return rep


def parse_payload(payload: dict) -> tuple:
    """Valide et normalise le corps envoyé par l'extension.

    Forme attendue :
      {"months": [{"year": 2025, "month": 1,
                   "period_start": "2025-01-01", "period_end": "2025-01-31",
                   "rows": [ … lignes brutes … ]}, …]}

    Renvoie (rows_normalisées, month_meta). Lève ValueError si le corps est
    inexploitable — mieux vaut un refus net qu'un import silencieusement vide,
    qui écraserait des données valides.
    """
    months = (payload or {}).get("months")
    if not isinstance(months, list) or not months:
        raise ValueError("corps invalide : « months » absent ou vide")

    all_rows, meta = [], []
    for m in months:
        if not isinstance(m, dict):
            continue
        try:
            year  = int(m.get("year"))
            month = int(m.get("month"))
        except (TypeError, ValueError):
            continue
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            continue
        rows = normalize_rows(m.get("rows"), year, month)
        all_rows.extend(rows)
        meta.append({
            "year": year, "month": month,
            "period_start": str(m.get("period_start") or ""),
            "period_end":   str(m.get("period_end") or ""),
            "rows": len(rows),
            "file_url": "",
            "source": "extension/generic.sellout",
        })
    if not all_rows:
        raise ValueError("aucune ligne exploitable dans le corps reçu")
    meta.sort(key=lambda x: (x["year"], x["month"]))
    return all_rows, meta
