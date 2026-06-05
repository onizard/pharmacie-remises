"""
API FastAPI — Break-Pharma Scraper Service

Endpoints :
  GET  /health               → sanity check
  POST /connect/{connector}  → teste les identifiants, met à jour connected dans Supabase
  POST /run/{connector}      → lance le scraping en arrière-plan
  GET  /status/{job_id}      → retourne le statut du job

Authentification : Bearer token Supabase (JWT de l'utilisateur break-pharma.fr)
Connecteurs supportés : ospharm, digipharmacie
"""

import asyncio
import json
import os
import re as _re
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, Path, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from supabase_client import (
    get_user_creds_for,
    patch_conn_test,
    patch_connector_connected,
    patch_job_status,
    save_user_creds,
    verify_token,
)

GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO  = "onizard/pharmacie-remises"
GH_WORKFLOW      = "scraper_ospharm.yml"
GH_DIGI_WORKFLOW = "scraper.yml"
GH_TEST_WORKFLOW = "test_connector.yml"


class ConnectBody(BaseModel):
    user: str
    password: str

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Break-Pharma Scraper API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://break-pharma.fr",
        "https://onizard.github.io",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

SUPPORTED_CONNECTORS = {"ospharm", "digipharmacie"}

# ── Job store (in-memory) ──────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}
JOB_TTL   = 3600
_executor = ThreadPoolExecutor(max_workers=3)


def _cleanup_jobs():
    cutoff = time.time() - JOB_TTL
    stale  = [jid for jid, j in _jobs.items() if j.get("created", 0) < cutoff]
    for jid in stale:
        del _jobs[jid]


def _extract_token(authorization: str) -> str:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token manquant")
    return token


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/connect/{connector}")
async def connect_connector(
    background_tasks: BackgroundTasks,
    body: ConnectBody,
    connector: str = Path(...),
    authorization: str = Header(default=""),
):
    """Enregistre les identifiants, lance le test en arrière-plan, retourne immédiatement."""
    if connector not in SUPPORTED_CONNECTORS:
        raise HTTPException(status_code=400, detail=f"Connecteur inconnu : {connector}")

    token = _extract_token(authorization)
    try:
        user_id = await verify_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    creds = {"user": body.user, "pass": body.password}
    background_tasks.add_task(_run_conn_test_async, user_id, connector, creds)
    return {"status": "testing"}


@app.post("/run/{connector}")
async def run_connector(
    background_tasks: BackgroundTasks,
    connector: str = Path(...),
    authorization: str = Header(default=""),
):
    """Lance le scraping. OSPHARM → GitHub Actions. DIGIPHARMACIE → local."""
    if connector not in SUPPORTED_CONNECTORS:
        raise HTTPException(status_code=400, detail=f"Connecteur inconnu : {connector}")

    token = _extract_token(authorization)
    try:
        user_id = await verify_token(token)
        creds   = await get_user_creds_for(user_id, connector)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    _cleanup_jobs()
    job_id = str(uuid.uuid4())

    if connector == "ospharm":
        background_tasks.add_task(_dispatch_gh_ospharm, user_id)
        return {"job_id": job_id, "mode": "github_actions"}

    # DIGIPHARMACIE : dispatch GitHub Actions (self-hosted, proxy résidentiel, camoufox)
    background_tasks.add_task(_dispatch_gh_digi, user_id)
    return {"job_id": job_id, "mode": "github_actions"}


async def _dispatch_gh_digi(user_id: str):
    """Déclenche scraper.yml sur GitHub Actions (self-hosted, proxy résidentiel)."""
    await patch_job_status(user_id, "verif_job", "running",
                           "Job en attente de démarrage…", [])
    if not GH_TOKEN:
        await patch_job_status(user_id, "verif_job", "error",
                               "GH_TOKEN manquant sur le serveur — contacter l'admin", [])
        return
    url  = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{GH_DIGI_WORKFLOW}/dispatches"
    body = json.dumps({"ref": "master", "inputs": {"user_id": user_id}}).encode()
    req  = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type":  "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"  [gh-dispatch] HTTP {r.status} — scraper digi déclenché pour {user_id[:8]}")
    except Exception as e:
        print(f"  [gh-dispatch] ERREUR digi: {e}")
        await patch_job_status(user_id, "verif_job", "error",
                               f"Impossible de lancer le workflow GitHub: {e}", [])


async def _dispatch_gh_ospharm(user_id: str):
    """Déclenche le workflow GitHub Actions scraper_ospharm.yml."""
    # Marque immédiatement le job comme "running" dans Supabase
    await patch_job_status(user_id, "ospharm_job", "running",
                           "Chargement des données en cours…", [])

    if not GH_TOKEN:
        await patch_job_status(user_id, "ospharm_job", "error",
                               "GH_TOKEN manquant sur le serveur — contacter l'admin", [])
        return

    url  = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{GH_WORKFLOW}/dispatches"
    body = json.dumps({"ref": "master", "inputs": {"user_id": user_id}}).encode()
    req  = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type":  "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"  [gh-dispatch] HTTP {r.status} — workflow ospharm déclenché pour {user_id[:8]}")
    except Exception as e:
        print(f"  [gh-dispatch] ERREUR: {e}")
        await patch_job_status(user_id, "ospharm_job", "error",
                               f"Impossible de lancer le workflow GitHub: {e}", [])


@app.get("/status/{job_id}")
def get_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job inconnu ou expiré")
    return job


# ── Parse grossiste XLSX ───────────────────────────────────────────────────────

class ParseGrossisteBody(BaseModel):
    storage_path: str  # chemin dans le bucket 'grossiste', ex: "user_id/ts_filename.xlsx"

@app.post("/parse/grossiste")
async def parse_grossiste(
    body: ParseGrossisteBody,
    authorization: str = Header(default=""),
):
    """Télécharge le XLSX grossiste depuis Supabase Storage, parse et sauvegarde grossiste_month_stats."""
    token = _extract_token(authorization)
    try:
        user_id = await verify_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        lambda: _parse_grossiste_sync(user_id, body.storage_path)
    )
    return result


def _parse_grossiste_sync(user_id: str, storage_path: str) -> dict:
    import io, re, openpyxl

    from supabase_client import SUPA_URL, SERVICE_KEY
    HEADERS = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}

    _LABO_MAP = {
        "biogaran": "BIOGARAN", "teva": "TEVA", "mylan": "MYLAN",
        "viatris": "VIATRIS", "zydus": "ZYDUS", "sandoz": "SANDOZ",
        "zentiva": "ZENTIVA", "arrow": "ARROW", "cristers": "CRISTERS",
        "eg labo": "EG LABO", "eg labs": "EG LABO", "evolupharm": "EVOLUPHARM",
        "ranbaxy": "RANBAXY", "actavis": "ACTAVIS", "aurobindo": "AUROBINDO",
        "intas": "INTAS", "almus": "ALMUS",
    }

    def norm_labo(raw):
        n = (raw or "").lower()
        for kw, canon in _LABO_MAP.items():
            if kw in n:
                return canon
        m = re.match(r"([A-Z][A-Z\-\']+)", (raw or "").strip())
        return m.group(1) if m else (raw or "").upper().split()[0] if raw else "?"

    # 1. Télécharger le XLSX depuis Storage
    dl_url = f"{SUPA_URL}/storage/v1/object/grossiste/{storage_path}"
    req = urllib.request.Request(dl_url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        xlsx_bytes = r.read()

    # 2. Parser la feuille "Récap par mois"
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    if "Récap par mois" not in wb.sheetnames:
        raise HTTPException(status_code=422, detail="Feuille 'Récap par mois' introuvable dans le fichier.")
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
        rep_dep, labo_raw, _, qty, _, _, ca_net = (list(row) + [None]*7)[:7]
        if rep_dep == "Rep G" and labo_raw and qty:
            labo = norm_labo(labo_raw)
            acc  = month_acc[current_month].setdefault(labo, {"qty": 0, "total_ht": 0.0})
            acc["qty"]      += int(qty or 0)
            acc["total_ht"] += float(ca_net or 0)

    grossiste_stats = {
        mk: sorted(
            [{"labo": l, "qty": d["qty"], "total_ht": round(d["total_ht"], 2)}
             for l, d in labos.items()],
            key=lambda r: r["labo"],
        )
        for mk, labos in sorted(month_acc.items())
    }
    if not grossiste_stats:
        raise HTTPException(status_code=422, detail="Aucune donnée extractible — vérifie le format du fichier.")

    # 3. Lire l'état courant et patcher grossiste_month_stats
    state_url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}&select=state_json&limit=1"
    req2 = urllib.request.Request(state_url, headers=HEADERS)
    with urllib.request.urlopen(req2, timeout=15) as r:
        rows = json.loads(r.read())
    state = (rows[0]["state_json"] if rows else {}) or {}
    # Fusion additive : mois distincts → union ; mois communs → addition par labo
    existing = state.get("grossiste_month_stats") or {}
    merged = dict(existing)
    for mk, new_rows in grossiste_stats.items():
        if mk not in merged:
            merged[mk] = new_rows
        else:
            labo_map = {r["labo"]: dict(r) for r in merged[mk]}
            for nr in new_rows:
                if nr["labo"] in labo_map:
                    labo_map[nr["labo"]]["qty"]      += nr["qty"]
                    labo_map[nr["labo"]]["total_ht"]  = round(labo_map[nr["labo"]]["total_ht"] + nr["total_ht"], 2)
                else:
                    labo_map[nr["labo"]] = dict(nr)
            merged[mk] = sorted(labo_map.values(), key=lambda r: r["labo"])
    state["grossiste_month_stats"] = merged

    patch_body = json.dumps({"state_json": state}).encode()
    patch_req  = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}",
        data=patch_body, method="PATCH",
        headers={**HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"},
    )
    with urllib.request.urlopen(patch_req, timeout=15):
        pass

    months = sorted(grossiste_stats)
    total_q  = sum(r["qty"]      for rows in grossiste_stats.values() for r in rows)
    total_ht = sum(r["total_ht"] for rows in grossiste_stats.values() for r in rows)
    return {
        "status":  "done",
        "months":  months,
        "labos":   len({r["labo"] for rows in grossiste_stats.values() for r in rows}),
        "qty":     total_q,
        "total_ht": round(total_ht, 2),
        "grossiste_month_stats": grossiste_stats,
    }


# ── Parse PDF DIGI (upload direct) ───────────────────────────────────────────

@app.post("/parse/digi-pdf")
async def parse_digi_pdf(
    file: UploadFile = File(...),
    authorization: str = Header(default=""),
):
    """Upload direct d'un PDF DIGI (invoice, RDP, presta) → extraction → digi_month_stats."""
    token = _extract_token(authorization)
    try:
        user_id = await verify_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    name = (file.filename or "").lower()
    if not name.endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Fichier PDF requis (.pdf)")

    pdf_bytes = await file.read()
    if len(pdf_bytes) < 200:
        raise HTTPException(status_code=422, detail="PDF trop court ou vide")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        lambda: _parse_digi_pdf_sync(user_id, pdf_bytes, file.filename or "upload.pdf"),
    )
    return result


def _parse_digi_pdf_sync(user_id: str, pdf_bytes: bytes, filename: str) -> dict:
    import os as _os, sys, tempfile
    from pathlib import Path as _Path

    sys.path.insert(0, _os.path.dirname(__file__))
    from pdf_extractor import extract_invoice_lines
    from run_job import _compute_digi_month_stats, _merge_digi_stats

    from supabase_client import SERVICE_KEY, SUPA_URL
    HEADERS = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}

    # Écrire le PDF dans un fichier temp, extraire les lignes
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = _Path(tmp.name)
    try:
        provider = filename.rsplit(".", 1)[0][:80]
        lines    = extract_invoice_lines(tmp_path, provider, "")
    finally:
        tmp_path.unlink(missing_ok=True)

    if not lines:
        raise HTTPException(status_code=422, detail="Aucune donnée extractible depuis ce PDF — format non reconnu ou labo hors cible.")

    new_stats = _compute_digi_month_stats(lines)

    # Charger l'état courant et fusionner
    req = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}&select=state_json&limit=1",
        headers=HEADERS,
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    state   = (rows[0]["state_json"] if rows else {}) or {}
    existing = state.get("digi_month_stats") or {}
    merged   = _merge_digi_stats(existing, new_stats)
    state["digi_month_stats"] = merged

    patch_body = json.dumps({"state_json": state}).encode()
    patch_req  = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}",
        data=patch_body, method="PATCH",
        headers={**HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"},
    )
    with urllib.request.urlopen(patch_req, timeout=15):
        pass

    months = sorted(new_stats)
    n_rdp    = sum(1 for l in lines if l.get("type") == "rdp")
    n_presta = sum(1 for l in lines if l.get("type") == "presta")
    n_prod   = len(lines) - n_rdp - n_presta
    return {
        "status":          "done",
        "months":          months,
        "lines":           len(lines),
        "product_lines":   n_prod,
        "rdp_avoirs":      n_rdp,
        "presta_avoirs":   n_presta,
        "digi_month_stats": merged,
    }


# ── Parse XLSX FSE Banque (HTP+OI) ───────────────────────────────────────────
#
# Format export OSPHARM FSE Banque → XLSX avec colonnes :
#   Date (DD/MM/YYYY) | Libellé | Montant (TTC, euros)
#
# Libellé format labo : "VIR BIOGARAN - 9006671913 - 2000065455 - Emetteur 300030154000020476361"
# Montant en TTC (virements bancaires incluent TVA si applicable).
# RDP (R2) : hors TVA → montant = HT = TTC
# Presta (R3) : TVA 20% → montant TTC

class ParseFseBankBody(BaseModel):
    storage_path: str  # chemin dans bucket 'fse-bank', ex: "user_id/ts_filename.xlsx"

@app.post("/parse/fse-bank")
async def parse_fse_bank(
    body: ParseFseBankBody,
    authorization: str = Header(default=""),
):
    """Parse le XLSX d'export FSE Banque (HTP+OI) et sauvegarde fse_month_stats."""
    token = _extract_token(authorization)
    try:
        user_id = await verify_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        lambda: _parse_fse_bank_sync(user_id, body.storage_path),
    )
    return result


def _parse_fse_bank_sync(user_id: str, storage_path: str) -> dict:
    import io, re as _re, datetime as _dt
    import openpyxl

    from supabase_client import SERVICE_KEY, SUPA_URL
    HEADERS = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}

    # ── Labos génériqueurs connus ──────────────────────────────────────────────
    _LABO_KEYS = [
        ("BIOGARAN", "BIOGARAN"), ("TEVA", "TEVA"), ("VIATRIS", "VIATRIS"),
        ("MYLAN", "VIATRIS"), ("SANDOZ", "SANDOZ"), ("ZENTIVA", "ZENTIVA"),
        ("ARROW", "ARROW"), ("CRISTERS", "CRISTERS"), ("ZYDUS", "ZYDUS"),
        ("EG LABO", "EG LABO"), ("EG LABS", "EG LABO"), ("EVOLUPHARM", "EVOLUPHARM"),
        ("RANBAXY", "RANBAXY"), ("AUROBINDO", "AUROBINDO"), ("INTAS", "INTAS"),
        ("ALMUS", "ALMUS"), ("QUALIMED", "QUALIMED"),
        # Dépositaires qui virent au nom du labo
        ("MOVIANTO", "MOVIANTO"), ("ALLOGA", "ALLOGA"), ("CEGEDIM", "CEGEDIM"),
    ]

    def _identify_labo(libelle: str) -> str | None:
        """Extrait le labo depuis le libellé VIR {NOM} - {REF} - ..."""
        lib = libelle.upper().strip()
        # Extraire tout ce qui est entre "VIR " et le premier " - "
        m = _re.match(r'VIR\s+(.+?)\s+-\s', lib)
        if not m:
            return None
        name_part = m.group(1)  # ex: "BIOGARAN", "SAS BIOGARAN", "EG LABO"
        for key, canon in _LABO_KEYS:
            if name_part.startswith(key) or key in name_part:
                return canon
        return None

    def _extract_ref(libelle: str) -> str:
        """Extrait la première référence après le nom du labo."""
        m = _re.match(r'VIR\s+.+?\s+-\s+(\S+)', libelle, _re.IGNORECASE)
        return m.group(1) if m else ""

    def _parse_date(val) -> str | None:
        """Convertit DD/MM/YYYY ou datetime en YYYY-MM-DD."""
        if isinstance(val, (_dt.date, _dt.datetime)):
            return val.strftime("%Y-%m-%d")
        s = str(val or "").strip()
        m = _re.match(r'(\d{2})/(\d{2})/(\d{4})', s)
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

    def _parse_amount(val) -> float | None:
        """Parse '2 161,74' ou '2161.74' ou '2 994€' → float."""
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val or "").replace('\xa0', '').replace(' ', '').replace('€', '').replace(',', '.').strip()
        try:
            return float(s) if s else None
        except ValueError:
            return None

    # 1. Télécharger le XLSX depuis Storage (bucket 'fse-bank')
    dl_url = f"{SUPA_URL}/storage/v1/object/fse-bank/{storage_path}"
    req = urllib.request.Request(dl_url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        xlsx_bytes = r.read()

    # 2. Parser le XLSX
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    ws = wb.active

    # Détecter la ligne d'en-tête (cherche "Date" + "Libellé"/"Libelle" + "Montant")
    hdr_row_idx = 0
    col_date, col_lib, col_amt = 0, 1, 2  # valeurs par défaut
    for ri, row in enumerate(ws.iter_rows(max_row=10, values_only=True)):
        cells = [str(c or '').lower() for c in row]
        if any('date' in c for c in cells) and any('libell' in c for c in cells):
            hdr_row_idx = ri
            for ci, c in enumerate(cells):
                if 'date' in c and col_date == 0: col_date = ci
                if 'libell' in c:                  col_lib  = ci
                if 'montant' in c:                 col_amt  = ci
            break

    # Accumulateur : {month_key: {labo: {montant_ttc, count, virements}}}
    acc: dict[str, dict] = {}
    virements_list: list[dict] = []  # virements individuels pour rapprochement
    row_num = 0
    for row in ws.iter_rows(min_row=hdr_row_idx + 2, values_only=True):
        if not any(c for c in row):
            continue
        date_str = _parse_date(row[col_date] if col_date < len(row) else None)
        libelle  = str(row[col_lib] if col_lib < len(row) else '') or ''
        amount   = _parse_amount(row[col_amt] if col_amt < len(row) else None)
        if not date_str or not libelle or amount is None or amount <= 0:
            continue
        labo = _identify_labo(libelle)
        if not labo:
            continue
        ref = _extract_ref(libelle)
        mk  = date_str[:7]  # YYYY-MM
        acc.setdefault(mk, {}).setdefault(labo, {"montant_ttc": 0.0, "count": 0, "refs": []})
        acc[mk][labo]["montant_ttc"] += amount
        acc[mk][labo]["count"]       += 1
        if ref:
            acc[mk][labo]["refs"].append(ref)
        virements_list.append({
            "date":        date_str,
            "mois":        mk,
            "labo":        labo,
            "montant_ttc": amount,
            "ref":         ref,
            "libelle":     libelle[:100],
        })
        row_num += 1

    if not acc:
        raise HTTPException(status_code=422, detail="Aucun virement de labo génériqueur trouvé — vérifiez les filtres (HTP+OI, Tous les virements).")

    fse_stats = {
        mk: sorted(
            [{"labo":       labo,
              "montant_ttc": round(d["montant_ttc"], 2),
              "count":       d["count"],
              "refs":        d["refs"][:10]}   # garder les 10 premières refs
             for labo, d in labos.items()],
            key=lambda r: r["labo"],
        )
        for mk, labos in sorted(acc.items())
    }

    # 3. Fusionner avec l'état existant
    req2 = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}&select=state_json&limit=1",
        headers=HEADERS,
    )
    with urllib.request.urlopen(req2, timeout=15) as r:
        rows2 = json.loads(r.read())
    state = (rows2[0]["state_json"] if rows2 else {}) or {}
    existing = state.get("fse_month_stats") or {}
    merged = dict(existing)
    for mk, new_rows in fse_stats.items():
        if mk not in merged:
            merged[mk] = new_rows
        else:
            lm = {r["labo"]: dict(r) for r in merged[mk]}
            for nr in new_rows:
                if nr["labo"] in lm:
                    lm[nr["labo"]]["montant_ttc"]  = round(lm[nr["labo"]]["montant_ttc"] + nr["montant_ttc"], 2)
                    lm[nr["labo"]]["count"]        += nr["count"]
                    lm[nr["labo"]]["refs"]          = (lm[nr["labo"]]["refs"] + nr["refs"])[:20]
                else:
                    lm[nr["labo"]] = dict(nr)
            merged[mk] = sorted(lm.values(), key=lambda r: r["labo"])
    state["fse_month_stats"] = merged

    patch = json.dumps({"state_json": state}).encode()
    preq  = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}",
        data=patch, method="PATCH",
        headers={**HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"},
    )
    with urllib.request.urlopen(preq, timeout=15):
        pass

    months = sorted(fse_stats)
    total_ttc = sum(r["montant_ttc"] for rows in fse_stats.values() for r in rows)
    return {
        "status":          "done",
        "months":          months,
        "rows_parsed":     row_num,
        "total_ttc":       round(total_ttc, 2),
        "fse_month_stats": merged,
        "virements":       virements_list[:500],  # max 500 pour le payload
    }


# ── Conn test (async wrapper) ──────────────────────────────────────────────────

async def _run_conn_test_async(user_id: str, connector: str, creds: dict):
    if connector == "digipharmacie":
        # curl_cffi seul ne bypass pas Cloudflare Bot Management → camoufox (vrai Firefox)
        # requis. On dispatch sur Hetzner (runner self-hosted) avec proxy résidentiel.
        await save_user_creds(user_id, connector, creds["user"], creds["pass"], False)
        await _dispatch_gh_conn_test(user_id, connector)
        return

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_executor, lambda: _test_connector(connector, creds, user_id))
        await save_user_creds(user_id, connector, creds["user"], creds["pass"], True)
        await patch_conn_test(user_id, connector, True, "Connexion réussie")
    except Exception as e:
        await patch_conn_test(user_id, connector, False, str(e))


async def _dispatch_gh_conn_test(user_id: str, connector: str):
    """Déclenche test_connector.yml sur GitHub Actions (self-hosted, IP non bloquée)."""
    if not GH_TOKEN:
        await patch_conn_test(user_id, connector, False,
                              "GH_TOKEN manquant sur le serveur — contacter l'admin")
        return

    url  = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{GH_TEST_WORKFLOW}/dispatches"
    body = json.dumps({
        "ref": "master",
        "inputs": {"user_id": user_id, "connector": connector},
    }).encode()
    req  = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type":  "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"  [gh-test] HTTP {r.status} — test {connector} pour {user_id[:8]}")
    except Exception as e:
        print(f"  [gh-test] ERREUR: {e}")
        await patch_conn_test(user_id, connector, False,
                              f"Impossible de lancer le workflow GitHub: {e}")


# ── Test connector (synchronous, called from executor) ─────────────────────────

def _test_connector(connector: str, creds: dict, user_id: str = ""):
    if connector == "ospharm":
        # sync_playwright nécessite une boucle non-démarrée dans le thread
        asyncio.set_event_loop(asyncio.new_event_loop())
        from test_connector import test_ospharm
        test_ospharm(creds)
    elif connector == "digipharmacie":
        # Chemin rapide : curl_cffi en-processus (~5-10s, pas de navigateur)
        try:
            from test_connector import test_digi_curl
            test_digi_curl(creds)
            return  # succès
        except RuntimeError:
            raise  # mauvais credentials
        except Exception as curl_err:
            if GH_TOKEN or os.environ.get("PROXY_URL"):
                # Proxy configuré mais toujours bloqué — camoufox subprocess ne servira à rien
                raise RuntimeError(f"Cloudflare bloque malgré le proxy : {curl_err}")
            pass  # pas de proxy → fallback subprocess camoufox

        # Fallback : subprocess camoufox avec hard timeout 180s
        _run_digi_test_subprocess(user_id, creds)


def _run_digi_test_subprocess(user_id: str, creds: dict):
    import subprocess
    import sys
    env = dict(os.environ)
    env["CONNECTOR"]  = "digipharmacie"
    env["USER_ID"]    = user_id
    env["DIGI_USER"]  = creds.get("user", "")
    env["DIGI_PASS"]  = creds.get("pass", "")
    try:
        proc = subprocess.run(
            [sys.executable, "test_connector.py"],
            env=env,
            timeout=180,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Timeout (>180s) — Digipharmacie inaccessible depuis ce serveur "
            "(Cloudflare bloque les IPs Render). Contactez le support."
        )
    if proc.returncode != 0:
        out = (proc.stdout + "\n" + proc.stderr).strip()
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        raise RuntimeError(lines[-1] if lines else "Test Digipharmacie échoué")


# ── Background job ─────────────────────────────────────────────────────────────

async def _run_job_async(job_id: str, user_id: str, connector: str, job_key: str, creds: dict):
    loop = asyncio.get_event_loop()

    def progress(msg: str):
        if job_id in _jobs:
            _jobs[job_id]["message"] = msg

    try:
        result = await loop.run_in_executor(
            _executor,
            lambda: _scrape(connector, user_id, creds, progress),
        )
        if isinstance(result, (tuple, list)):
            rows       = result[0]
            file_url   = result[1] if len(result) > 1 else ""
            period_start = result[2] if len(result) > 2 else ""
            period_end   = result[3] if len(result) > 3 else ""
        else:
            rows, file_url, period_start, period_end = result, "", "", ""
        # Pour OSPHARM : compacter à {cip13, qty, libelle} avant stockage Supabase
        # (réduit ~5 Mo → ~400 Ko ; ospharmRowsToCsvData() sur le front gère les deux formats)
        stored_rows = _compact_osp_rows(rows) if connector == "ospharm" else rows
        msg = f"{len(rows)} lignes extraites"
        _jobs[job_id].update({
            "status":   "done",
            "message":  msg,
            "rows":     stored_rows,
            "total":    len(rows),
            "file_url": file_url,
        })
        await patch_job_status(user_id, job_key, "done", msg, stored_rows, file_url,
                               period_start=period_start, period_end=period_end)
    except Exception as e:
        _jobs[job_id].update({"status": "error", "message": str(e), "error": str(e)})
        await patch_job_status(user_id, job_key, "error", str(e), [])


def _compact_osp_rows(rows: list[dict]) -> list[dict]:
    """Convertit les lignes OSPHARM brutes (24 cols) en {cip13, qty, libelle}.
    Réduit ~5 Mo → ~400 Ko pour le stockage dans Supabase.
    Même logique que ospharmRowsToCsvData() côté frontend.
    """
    if not rows:
        return []

    def _n(k):
        s = (k or "").lower()
        for a, b in [("é","e"),("è","e"),("ê","e"),("à","a"),("ù","u"),("î","i"),("ô","o")]:
            s = s.replace(a, b)
        return _re.sub(r"[^a-z0-9]", "", s)

    keys = list(rows[0].keys())
    cip_k = next((k for k in keys if _n(k) == "codeean"), None) or \
            next((k for k in keys if any(p in _n(k) for p in ("cip", "ean", "acl"))), None)
    qty_k = next((k for k in keys if _n(k) == "quantite"), None) or \
            next((k for k in keys if any(p in _n(k) for p in ("qte", "qty"))
                  and "n1" not in _n(k) and "evo" not in _n(k)), None)
    lib_k = next((k for k in keys if _n(k) == "libelleproduit"), None) or \
            next((k for k in keys if "produit" in _n(k)), None) or \
            next((k for k in keys if "libelle" in _n(k)), None)

    if not cip_k or not qty_k:
        return rows  # fallback: renvoyer les données brutes si colonnes non trouvées

    result = []
    for r in rows:
        raw = _re.sub(r"\D", "", str(r.get(cip_k) or ""))
        cip13 = raw if len(raw) == 13 else ("340000" + raw if len(raw) == 7 else None)
        try:
            qty = float(str(r.get(qty_k) or 0).replace(",", "."))
        except (ValueError, TypeError):
            qty = 0.0
        if not cip13 or qty <= 0:
            continue
        result.append({
            "cip13":   cip13,
            "qty":     qty,
            "libelle": str(r.get(lib_k) or "").strip() if lib_k else "",
        })
    return result


def _scrape(connector: str, user_id: str, creds: dict, progress):
    if connector == "digipharmacie":
        # async camoufox — asyncio.run() crée sa propre boucle
        from scraper import run_scraper
        return run_scraper(creds, progress)
    elif connector == "ospharm":
        asyncio.set_event_loop(asyncio.new_event_loop())
        from run_job_ospharm import run_ospharm
        return run_ospharm(creds, progress, user_id=user_id)
    raise RuntimeError(f"Connecteur inconnu : {connector}")
