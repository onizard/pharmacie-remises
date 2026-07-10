"""
grossiste_parse.py — Parseurs du justificatif répartiteur (XLSX CERP).

Module PARTAGÉ entre l'API Render (main.py) et le runner GitHub Actions
(run_job_digi_batch.py) : ne doit importer ni FastAPI ni le reste de l'app.
Dépendance : openpyxl uniquement.
"""

# ── Grossiste helpers ──────────────────────────────────────────────────────────

_GROSSISTE_LABO_MAP = {
    "biogaran": "BIOGARAN", "teva": "TEVA", "mylan": "MYLAN",
    "viatris": "VIATRIS", "zydus": "ZYDUS", "sandoz": "SANDOZ",
    "zentiva": "ZENTIVA", "arrow": "ARROW", "cristers": "CRISTERS",
    "eg labo": "EG LABO", "eg labs": "EG LABO", "evolupharm": "EVOLUPHARM",
    "ranbaxy": "RANBAXY", "actavis": "ACTAVIS", "aurobindo": "AUROBINDO",
    "intas": "INTAS", "almus": "ALMUS",
}

def _norm_grossiste_labo(raw: str) -> str:
    import re
    n = (raw or "").lower()
    for kw, canon in _GROSSISTE_LABO_MAP.items():
        if kw in n:
            return canon
    m = re.match(r"([A-Z][A-Z\-\']+)", (raw or "").strip())
    return m.group(1) if m else (raw or "").upper().split()[0] if raw else "?"


def _parse_grossiste_bytes(xlsx_bytes: bytes) -> dict:
    """Parse feuille 'Récap par mois' → {year-MM: [{labo, qty, total_ht, ca_brut,
    paliers: [{taux, qty, brut, remise, net}]}]}.

    Le justificatif répartiteur ventile chaque labo par 'Tx Rem' (= palier RSF :
    0 / 2,5 / 5 / 10 / 20 / 25 / 30 / 40). On conserve ce détail par palier
    (montant remise = RSF effectivement obtenu) en plus des totaux par labo.
    """
    import io, re, openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    if "Récap par mois" not in wb.sheetnames:
        return {}
    ws = wb["Récap par mois"]

    month_acc: dict[str, dict] = {}
    current_month = None
    for row in ws.iter_rows(values_only=True):
        if not any(v is not None for v in row):
            continue
        cell0 = str(row[0] or "")
        if "Mois comptable" in cell0:
            m = re.search(r"(\d{4})\s+(\d{2})", cell0)
            if m:
                current_month = f"{m.group(1)}-{m.group(2)}"
                month_acc.setdefault(current_month, {})
            continue
        if current_month is None:
            continue
        # Cols : Rep/Dep | nom | Tx Rem | qtes | Mt Vente Brut HT | Montant remise | CA net HT
        rep_dep, labo_raw, taux, qty, ca_brut_raw, remise_raw, ca_net = (list(row) + [None]*7)[:7]
        if rep_dep == "Rep G" and labo_raw and qty:
            labo = _norm_grossiste_labo(labo_raw)
            acc  = month_acc[current_month].setdefault(
                labo, {"qty": 0, "total_ht": 0.0, "ca_brut": 0.0, "paliers": {}})
            q = int(qty or 0)
            b = float(ca_brut_raw or 0)
            r = float(remise_raw or 0)
            n = float(ca_net or 0)
            acc["qty"]      += q
            acc["total_ht"] += n
            acc["ca_brut"]  += b
            try:
                tx = round(float(taux), 2)
            except (TypeError, ValueError):
                tx = None
            if tx is not None:
                p = acc["paliers"].setdefault(tx, {"qty": 0, "brut": 0.0, "remise": 0.0, "net": 0.0})
                p["qty"]    += q
                p["brut"]   += b
                p["remise"] += r
                p["net"]    += n

    wb.close()
    return {
        mk: sorted(
            [{"labo": l, "qty": d["qty"], "total_ht": round(d["total_ht"], 2), "ca_brut": round(d["ca_brut"], 2),
              "paliers": sorted(
                  [{"taux": tx, "qty": p["qty"], "brut": round(p["brut"], 2),
                    "remise": round(p["remise"], 2), "net": round(p["net"], 2)}
                   for tx, p in d["paliers"].items()],
                  key=lambda x: x["taux"])}
             for l, d in labos.items()],
            key=lambda r: r["labo"],
        )
        for mk, labos in sorted(month_acc.items())
    }


def _parse_grossiste_detail_bytes(xlsx_bytes: bytes) -> dict:
    """Feuille 'Détail par mois' du justificatif → achats PAR RÉFÉRENCE :
    {year-MM: {LABO: [[cip13, taux, qty, brut], …]}} (listes compactes).

    C'est la source de précision ultime pour la vérification RDP : les remises
    labo s'appliquent aux ACHATS — ce détail permet de calculer l'attendu par
    référence (exceptions par CIP comprises), là où le récap n'agrège que par
    palier. Lignes 'Rep G' uniquement (comme le récap)."""
    import io, re, openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    if "Détail par mois" not in wb.sheetnames:
        wb.close()
        return {}
    ws = wb["Détail par mois"]

    out: dict[str, dict] = {}
    cur = None
    for row in ws.iter_rows(values_only=True):
        cells = (list(row) + [None] * 11)[:11]
        m = re.search(r"Mois comptable\s*:\s*(\d{4})\s+(\d{2})", str(cells[0] or ""))
        if m:
            cur = f"{m.group(1)}-{m.group(2)}"
            out.setdefault(cur, {})
            continue
        if cur is None:
            continue
        # Cols : Cod.Artic | CIP/ACL 13 | Libellé | Rep/Dep | Partenariat | Tx Rem |
        #        Prix fact ht | qtes | Mt Vente Brut ht | Montant remise | CA net HT
        cip = str(cells[1] or "").strip()
        if not re.fullmatch(r"\d{13}", cip) or str(cells[3] or "") != "Rep G":
            continue
        labo = _norm_grossiste_labo(cells[4] or "")
        if not labo:
            continue
        try:
            taux = round(float(cells[5]), 2)
        except (TypeError, ValueError):
            continue
        try:
            qty  = int(cells[7] or 0)
            brut = round(float(cells[8] or 0), 2)
        except (TypeError, ValueError):
            continue
        # Agrégat par (cip, taux) dans le mois (une référence peut avoir plusieurs lignes).
        rows_l = out[cur].setdefault(labo, [])
        for r in rows_l:
            if r[0] == cip and r[1] == taux:
                r[2] += qty
                r[3] = round(r[3] + brut, 2)
                break
        else:
            rows_l.append([cip, taux, qty, brut])
    wb.close()
    return {mk: labos for mk, labos in out.items() if labos}


def _merge_paliers(a: list, b: list) -> list:
    """Fusion additive de deux listes de paliers [{taux, qty, brut, remise, net}]."""
    acc: dict = {}
    for src in (a or []), (b or []):
        for p in src:
            tx = p.get("taux")
            d  = acc.setdefault(tx, {"qty": 0, "brut": 0.0, "remise": 0.0, "net": 0.0})
            d["qty"]    += p.get("qty", 0)
            d["brut"]   += p.get("brut", 0)
            d["remise"] += p.get("remise", 0)
            d["net"]    += p.get("net", 0)
    return sorted(
        [{"taux": tx, "qty": d["qty"], "brut": round(d["brut"], 2),
          "remise": round(d["remise"], 2), "net": round(d["net"], 2)}
         for tx, d in acc.items()],
        key=lambda x: (x["taux"] if x["taux"] is not None else -1))


def _merge_grossiste_stats(existing: dict, new_stats: dict) -> dict:
    """Fusion additive : mois distincts → union ; mois communs → addition par labo
    (y compris la ventilation par palier)."""
    merged = dict(existing)
    for mk, new_rows in new_stats.items():
        if mk not in merged:
            merged[mk] = new_rows
        else:
            labo_map = {r["labo"]: dict(r) for r in merged[mk]}
            for nr in new_rows:
                if nr["labo"] in labo_map:
                    ex = labo_map[nr["labo"]]
                    ex["qty"]      += nr["qty"]
                    ex["total_ht"]  = round(ex["total_ht"] + nr["total_ht"], 2)
                    ex["ca_brut"]   = round(ex.get("ca_brut", 0) + nr.get("ca_brut", 0), 2)
                    ex["paliers"]   = _merge_paliers(ex.get("paliers"), nr.get("paliers"))
                else:
                    labo_map[nr["labo"]] = dict(nr)
            merged[mk] = sorted(labo_map.values(), key=lambda r: r["labo"])
    return merged
