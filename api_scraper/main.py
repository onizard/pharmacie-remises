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
GH_FSE_WORKFLOW  = "scraper_fse.yml"


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

SUPPORTED_CONNECTORS = {"ospharm", "digipharmacie", "concentrateur"}

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


@app.post("/run/fse-export")
async def run_fse_export(
    background_tasks: BackgroundTasks,
    authorization: str = Header(default=""),
):
    """Déclenche le scraper FSE Banque via GitHub Actions."""
    token = _extract_token(authorization)
    try:
        user_id = await verify_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    background_tasks.add_task(_dispatch_gh_fse, user_id)
    return {"status": "dispatched"}


async def _dispatch_gh_fse(user_id: str):
    """Déclenche scraper_fse.yml sur GitHub Actions (dates auto : jan N-1 → aujourd'hui)."""
    import datetime as _dt
    today     = _dt.date.today()
    date_from = f"{today.year - 1}-01-01"
    date_to   = today.strftime("%Y-%m-%d")

    # Mise à jour statut
    try:
        state = _supa_get_state_for(user_id)
        state["fse_job"] = {"status": "running", "message": f"Export FSE lancé ({date_from} → {date_to})…", "error": ""}
        _supa_patch_state_for(user_id, state)
    except Exception:
        pass

    if not GH_TOKEN:
        try:
            state = _supa_get_state_for(user_id)
            state["fse_job"] = {"status": "error", "message": "", "error": "GH_TOKEN manquant sur le serveur"}
            _supa_patch_state_for(user_id, state)
        except Exception:
            pass
        return

    url  = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{GH_FSE_WORKFLOW}/dispatches"
    body = json.dumps({"ref": "master", "inputs": {
        "user_id":   user_id,
        "date_from": date_from,
        "date_to":   date_to,
    }}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization":        f"Bearer {GH_TOKEN}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type":         "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"  [gh-dispatch] HTTP {r.status} — fse-export déclenché pour {user_id[:8]} ({date_from}→{date_to})")
    except Exception as e:
        print(f"  [gh-dispatch] ERREUR fse: {e}")
        try:
            state = _supa_get_state_for(user_id)
            state["fse_job"] = {"status": "error", "message": "", "error": str(e)}
            _supa_patch_state_for(user_id, state)
        except Exception:
            pass


def _supa_get_state_for(user_id: str) -> dict:
    from supabase_client import SERVICE_KEY, SUPA_URL
    headers = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}&select=state_json&limit=1"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return (rows[0]["state_json"] if rows else {}) or {}


def _supa_patch_state_for(user_id: str, state: dict):
    from supabase_client import SERVICE_KEY, SUPA_URL
    headers = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    url  = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}"
    body = json.dumps({"state_json": state}).encode()
    req  = urllib.request.Request(url, data=body, method="PATCH", headers=headers)
    with urllib.request.urlopen(req, timeout=15):
        pass


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

    # Charger toutes les lignes d'un coup (max 5000 lignes)
    all_rows = list(ws.iter_rows(max_row=5000, values_only=True))

    # ── Détection des colonnes ─────────────────────────────────────────────────
    # Approche 1 : chercher la ligne d'en-tête par nom de colonne
    _DATE_KW   = ('date',)
    _LIB_KW    = ('libell', 'libellé', 'description', 'opération', 'operation', 'désignation')
    _AMT_KW    = ('montant', 'crédit', 'credit', 'débit', 'debit', 'valeur', 'amount')
    hdr_idx    = -1
    col_date, col_lib, col_amt = 0, 1, 2  # defaults

    for ri, row in enumerate(all_rows[:15]):
        cells = [str(c or '').lower().strip() for c in row]
        has_date = any(any(k in c for k in _DATE_KW) for c in cells)
        has_lib  = any(any(k in c for k in _LIB_KW)  for c in cells)
        if has_date and has_lib:
            hdr_idx = ri
            for ci, c in enumerate(cells):
                if any(k in c for k in _DATE_KW) and 'mise' not in c and 'update' not in c:
                    col_date = ci
                if any(k in c for k in _LIB_KW):
                    col_lib  = ci
                if any(k in c for k in _AMT_KW):
                    col_amt  = ci
            break

    # Approche 2 : détection par contenu — chercher la colonne avec "VIR " dans les cellules
    if hdr_idx < 0:
        for ri, row in enumerate(all_rows[:30]):
            for ci, c in enumerate(row):
                s = str(c or '').upper().strip()
                if s.startswith('VIR ') and len(s) > 6:
                    # data starts here; header probably 1 row above
                    hdr_idx  = max(0, ri - 1)
                    col_lib  = ci
                    col_date = max(0, ci - 1)   # date probablement à gauche
                    col_amt  = ci + 1            # montant probablement à droite
                    break
            if hdr_idx >= 0:
                break

    data_start = hdr_idx + 2  # 0-indexed; openpyxl min_row est 1-indexed donc +1

    # ── Parcours des lignes de données ─────────────────────────────────────────
    acc: dict[str, dict] = {}
    virements_list: list[dict] = []
    row_num   = 0
    skipped_lib: list[str] = []   # libellés non reconnus (debug)

    for row in all_rows[data_start:]:
        if not any(c for c in row):
            continue
        n = len(row)
        date_str = _parse_date(row[col_date] if col_date < n else None)
        libelle  = str(row[col_lib] if col_lib < n else '') or ''
        amount   = _parse_amount(row[col_amt] if col_amt < n else None)

        # Si montant ≤ 0, essayer les colonnes adjacentes (débit/crédit séparés)
        if (amount is None or amount <= 0) and col_amt + 1 < n:
            amount = _parse_amount(row[col_amt + 1])

        if not date_str or not libelle:
            continue
        if amount is None or amount <= 0:
            continue
        if not libelle.upper().strip().startswith('VIR '):
            continue  # ignorer les lignes qui ne sont pas des virements

        labo = _identify_labo(libelle)
        if not labo:
            if len(skipped_lib) < 10:
                skipped_lib.append(libelle[:60])
            continue

        ref = _extract_ref(libelle)
        mk  = date_str[:7]
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
        # Collecter infos de debug : premières lignes + colonnes détectées
        sample = [[str(c)[:25] for c in r if c] for r in all_rows[max(0,hdr_idx):hdr_idx+5] if any(c for c in r)]
        debug  = (
            f"Colonnes détectées : date={col_date}, libellé={col_lib}, montant={col_amt}. "
            f"Ligne header={hdr_idx}. "
            f"Premières lignes : {sample[:3]}. "
            f"Libellés VIR non reconnus : {skipped_lib[:5]}"
        )
        raise HTTPException(status_code=422, detail=debug)

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
    loop = asyncio.get_event_loop()

    if connector == "digipharmacie":
        # 1. Tenter curl_cffi directement sur Render (rapide, pas de runner)
        try:
            await loop.run_in_executor(_executor, lambda: _test_digi_curl_only(creds))
            await save_user_creds(user_id, connector, creds["user"], creds["pass"], True)
            await patch_conn_test(user_id, connector, True, "Connexion réussie")
            return
        except RuntimeError as e:
            # Mauvais credentials → fail immédiat, pas besoin du runner
            await patch_conn_test(user_id, connector, False, str(e))
            return
        except Exception:
            pass  # Cloudflare bloque Render → fallback runner self-hosted

        # 2. Cloudflare bloque → dispatch vers runner self-hosted (IP résidentielle)
        await save_user_creds(user_id, connector, creds["user"], creds["pass"], False)
        await _dispatch_gh_conn_test(user_id, connector)
        return

    if connector in ("ospharm", "concentrateur"):
        # Playwright Chromium ne peut pas tourner sur Render (512 MB) → dispatch GH Actions
        await save_user_creds(user_id, connector, creds["user"], creds["pass"], False)
        await _dispatch_gh_conn_test(user_id, connector)
        return

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

def _test_digi_curl_only(creds: dict):
    """Test Digipharmacie via curl_cffi uniquement — RuntimeError si mauvais credentials,
    autre Exception si Cloudflare bloque (le caller dispatch alors vers le runner self-hosted)."""
    from test_connector import test_digi_curl
    test_digi_curl(creds)


def _test_connector(connector: str, creds: dict, user_id: str = ""):
    if connector in ("ospharm", "concentrateur"):
        asyncio.set_event_loop(asyncio.new_event_loop())
        if connector == "ospharm":
            from test_connector import test_ospharm
            test_ospharm(creds)
        else:
            from test_connector import test_concentrateur
            test_concentrateur(creds)
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


# ── Exploration Digipharmacie Espaces clients (endpoint temporaire) ────────────

@app.get("/explore/digi-espace-client")
async def explore_digi_espace_client(authorization: str = Header(default="")):
    """Navigue vers Digipharmacie > Achats > Espaces clients, capture API + HTML."""
    token = _extract_token(authorization)
    try:
        user_id = await verify_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        lambda: _explore_digi_espace_client_sync(user_id),
    )
    return result


def _explore_digi_espace_client_sync(user_id: str) -> dict:
    import asyncio as _asyncio
    from supabase_client import SERVICE_KEY, SUPA_URL

    creds = {}
    try:
        url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}&select=connectors&limit=1"
        req = urllib.request.Request(url, headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read())
        conns = (rows[0].get("connectors") or {}) if rows else {}
        cred  = conns.get("digipharmacie", {})
        creds = {"user": cred.get("user", ""), "pass": cred.get("pass", "")}
    except Exception as e:
        return {"error": f"Impossible de lire les credentials: {e}"}

    if not creds.get("user"):
        return {"error": "Pas de credentials Digipharmacie en base"}

    _asyncio.set_event_loop(_asyncio.new_event_loop())
    return _asyncio.get_event_loop().run_until_complete(_explore_async(creds, user_id))


async def _explore_async(creds: dict, user_id: str) -> dict:
    import os as _os
    from camoufox.async_api import AsyncCamoufox
    from supabase_client import SERVICE_KEY, SUPA_URL

    BASE = "https://app.digipharmacie.fr"
    PROXY_URL = _os.environ.get("PROXY_URL", "")
    api_calls = []
    pages_visited = []

    proxy_cfg = None
    if PROXY_URL:
        import urllib.parse as _up
        _p = _up.urlparse(PROXY_URL)
        proxy_cfg = {"server": f"{_p.scheme}://{_p.hostname}:{_p.port}",
                     "username": _p.username or "", "password": _p.password or ""}

    # curl_cffi login
    session_cookies: dict = {}
    try:
        from curl_cffi import requests as cffi_requests
        proxy_kw = {"proxy": PROXY_URL} if PROXY_URL else {}
        session = cffi_requests.Session(impersonate="chrome124")
        r = session.get(f"{BASE}/login/", headers={"Accept": "text/html,*/*", "Accept-Language": "fr-FR,fr;q=0.9"},
                        timeout=25, allow_redirects=True, **proxy_kw)
        csrf = session.cookies.get("csrftoken", "")
        if csrf:
            for ep in ["/api/v1/auth/login/", "/api/auth/login/"]:
                rp = session.post(f"{BASE}{ep}",
                                  json={"email": creds["user"], "password": creds["pass"]},
                                  headers={"Accept": "application/json", "Content-Type": "application/json",
                                           "X-CSRFToken": csrf, "Referer": f"{BASE}/login/", "Origin": BASE},
                                  timeout=15, allow_redirects=False, **proxy_kw)
                if rp.status_code == 200:
                    session_cookies = dict(session.cookies)
                    break
                if rp.status_code in (400, 401):
                    return {"error": "Identifiants DIGIPHARMACIE incorrects"}
            if not session_cookies:
                rp = session.post(f"{BASE}/login/",
                                  data={"email": creds["user"], "password": creds["pass"], "csrfmiddlewaretoken": csrf},
                                  headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": f"{BASE}/login/"},
                                  timeout=15, allow_redirects=True, **proxy_kw)
                if "/login" not in rp.url:
                    session_cookies = dict(session.cookies)
    except Exception:
        pass

    async with AsyncCamoufox(headless=True, geoip=False) as browser:
        ctx  = await browser.new_context(**({"proxy": proxy_cfg} if proxy_cfg else {}))
        if session_cookies:
            await ctx.add_cookies([{"name": k, "value": v, "domain": "app.digipharmacie.fr",
                                    "path": "/", "sameSite": "Lax"} for k, v in session_cookies.items()])

        page = await ctx.new_page()
        page.on("pageerror", lambda e: None)
        page.set_default_timeout(30_000)

        async def on_response(resp):
            if "/api/" in resp.url and resp.status < 400:
                try:
                    body = await resp.json()
                    api_calls.append({"url": resp.url.replace(BASE, ""), "status": resp.status,
                                      "body": body if not isinstance(body, list) else body[:3],
                                      "count": len(body) if isinstance(body, list) else None})
                except Exception:
                    pass
        page.on("response", on_response)

        # Vérifier la session
        await page.goto(f"{BASE}/", timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=10_000)
        pages_visited.append({"label": "home", "url": page.url})

        # Login camoufox si pas de cookies
        if "/login" in page.url and not session_cookies:
            cf_kw = ("just a moment", "checking", "verifying", "cloudflare")
            for _ in range(20):
                title = (await page.title()).lower()
                if not any(k in title for k in cf_kw):
                    break
                await page.wait_for_timeout(3_000)
            await page.fill("input[type=email], input[name=email]", creds["user"])
            await page.fill("input[type=password]", creds["pass"])
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("networkidle", timeout=20_000)

        pages_visited.append({"label": "after_login", "url": page.url})

        # Naviguer vers Espaces clients
        await page.goto(f"{BASE}/achat/espaces-clients/", timeout=20_000)
        await page.wait_for_load_state("networkidle", timeout=10_000)
        pages_visited.append({"label": "espaces_clients", "url": page.url})

        html     = await page.content()
        all_links = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a')).map(a => ({
                text: a.textContent.trim().slice(0, 80), href: a.href
            })).filter(a => a.text && a.href.includes('digipharmacie'))
        """)
        await ctx.close()

    result = {"pages_visited": pages_visited, "api_calls": api_calls,
              "links": all_links[:30], "html": html[:8000]}

    # Sauvegarder en base
    try:
        url  = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}&select=state_json&limit=1"
        req  = urllib.request.Request(url, headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read())
        state = rows[0]["state_json"] if rows else {}
        state["digi_espace_client_explore"] = result
        body = json.dumps({"state_json": state}).encode()
        req2 = urllib.request.Request(f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}",
                                      data=body, method="PATCH",
                                      headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
                                               "Content-Type": "application/json", "Prefer": "return=minimal"})
        with urllib.request.urlopen(req2, timeout=15): pass
    except Exception as e:
        result["save_error"] = str(e)

    return result
