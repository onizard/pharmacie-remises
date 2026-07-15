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


def _match_recap_header(row) -> dict | None:
    """Repère les colonnes d'un en-tête 'Récap par mois' par leur libellé.
    Renvoie {labo, taux, qty, brut, remise, net} ou None si la ligne n'est pas
    un en-tête. Robuste au décalage de colonnes du gabarit CERP (cf. ci-dessous)."""
    cols = {}
    for i, v in enumerate(row):
        s = str(v or "").strip().lower()
        if not s:
            continue
        if   s.startswith("nom partenariat"): cols["labo"]   = i
        elif s.startswith("tx rem"):          cols["taux"]   = i
        elif s.startswith("qtes"):            cols["qty"]    = i
        elif s.startswith("mt vente brut"):   cols["brut"]   = i
        elif s.startswith("montant remise"):  cols["remise"] = i
        elif s.startswith("ca net"):          cols["net"]    = i
    return cols if {"labo", "taux", "qty", "brut"} <= set(cols) else None


def _parse_grossiste_bytes(xlsx_bytes: bytes) -> dict:
    """Parse feuille 'Récap par mois' → {year-MM: [{labo, qty, total_ht, ca_brut,
    paliers: [{taux, qty, brut, remise, net}]}]}.

    Le justificatif répartiteur ventile chaque labo par 'Tx Rem' (= palier RSF :
    0 / 2,5 / 5 / 10 / 20 / 25 / 30 / 40). On conserve ce détail par palier
    (montant remise = RSF effectivement obtenu) en plus des totaux par labo.

    ⚠️ Le gabarit CERP a CHANGÉ en cours d'année 2026 : les mois anciens
    (jan.–avr.) portent une colonne VIDE en 3e position qui décale toutes les
    colonnes d'un cran par rapport aux mois récents (mai+). On lit donc la
    position des colonnes à CHAQUE ligne d'en-tête (« nom partenariat … Tx Rem …
    ») au lieu de les coder en dur — sinon, pour ces mois, le taux tombe sur une
    cellule vide (palier ignoré) et le ca_brut est lu sur la colonne des
    quantités (CA divisé par ~4, réalisation faussée, coop bloquée à tort)."""
    import io, re, openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    if "Récap par mois" not in wb.sheetnames:
        return {}
    ws = wb["Récap par mois"]

    # Positions par défaut = gabarit « récent » (mai 2026+). Réécrites à chaque
    # en-tête rencontré. Repli sûr si un bloc n'a pas d'en-tête.
    idx = {"labo": 1, "taux": 2, "qty": 3, "brut": 4, "remise": 5, "net": 6}
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
        hdr = _match_recap_header(row)
        if hdr:                       # (re)cale les colonnes pour le bloc courant
            idx = {**idx, **hdr}
            continue
        if current_month is None:
            continue
        cell = lambda k: row[idx[k]] if len(row) > idx[k] else None
        rep_dep     = row[0] if row else None
        labo_raw    = cell("labo")
        taux        = cell("taux")
        qty         = cell("qty")
        ca_brut_raw = cell("brut")
        remise_raw  = cell("remise")
        ca_net      = cell("net")
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
    palier. Lignes 'Rep G' uniquement (comme le récap).

    Colonnes lues à l'en-tête (« CIP/ACL … Partenariat … Tx Rem … ») et non
    codées en dur, par prudence : le gabarit CERP décale ses colonnes sur les
    mois anciens (cf. _parse_grossiste_bytes)."""
    import io, re, openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    if "Détail par mois" not in wb.sheetnames:
        wb.close()
        return {}
    ws = wb["Détail par mois"]

    def _match_detail_header(row):
        cols = {}
        for i, v in enumerate(row):
            s = str(v or "").strip().lower()
            if not s:
                continue
            if   s.startswith("cip/acl") or s.startswith("cip"): cols["cip"]  = i
            elif s.startswith("rep/dep"):                        cols["rep"]  = i
            elif s.startswith("partenariat"):                    cols["part"] = i
            elif s.startswith("tx rem"):                         cols["taux"] = i
            elif s.startswith("qtes"):                           cols["qty"]  = i
            elif s.startswith("mt vente brut"):                  cols["brut"] = i
        return cols if {"cip", "rep", "part", "taux", "qty", "brut"} <= set(cols) else None

    # Positions par défaut = gabarit habituel ; réécrites à chaque en-tête.
    idx = {"cip": 1, "rep": 3, "part": 4, "taux": 5, "qty": 7, "brut": 8}
    out: dict[str, dict] = {}
    cur = None
    for row in ws.iter_rows(values_only=True):
        cells = (list(row) + [None] * 12)[:12]
        m = re.search(r"Mois comptable\s*:\s*(\d{4})\s+(\d{2})", str(cells[0] or ""))
        if m:
            cur = f"{m.group(1)}-{m.group(2)}"
            out.setdefault(cur, {})
            continue
        hdr = _match_detail_header(row)
        if hdr:
            idx = {**idx, **hdr}
            continue
        if cur is None:
            continue
        get = lambda k: cells[idx[k]] if len(cells) > idx[k] else None
        cip = str(get("cip") or "").strip()
        if not re.fullmatch(r"\d{13}", cip) or str(get("rep") or "") != "Rep G":
            continue
        labo = _norm_grossiste_labo(get("part") or "")
        if not labo:
            continue
        try:
            taux = round(float(get("taux")), 2)
        except (TypeError, ValueError):
            continue
        try:
            qty  = int(get("qty") or 0)
            brut = round(float(get("brut") or 0), 2)
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
    """Fusion à la maille MOIS : un justificatif « par taux » contient ses mois EN
    ENTIER → le mois re-déposé REMPLACE l'ancien (mêmes semantics que le détail par
    CIP, cf. main.py). Les mois absents du nouveau lot sont conservés.

    ⚠️ NE PAS revenir à une addition par labo : re-déposer un fichier qui recouvre
    des mois déjà en base (cas normal d'une ré-analyse) doublait alors leur CA — et
    comme le back-end écrit en snake_case (prioritaire au chargement front), le
    doublon survivait au rechargement. La ventilation par palier est déjà complète
    dans chaque parse, il n'y a rien à additionner entre deux dépôts."""
    merged = dict(existing or {})
    merged.update(new_stats or {})   # mois présents dans le nouveau lot → remplacés
    return merged
