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
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Path
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
    await save_user_creds(user_id, connector, body.user, body.password, False)
    background_tasks.add_task(_run_conn_test_async, user_id, connector, creds)
    return {"status": "testing"}


@app.post("/run/{connector}")
async def run_connector(
    background_tasks: BackgroundTasks,
    connector: str = Path(...),
    authorization: str = Header(default=""),
):
    """Lance le scraping en arrière-plan. Retourne {job_id}."""
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
    job_key = f"{connector}_job"
    _jobs[job_id] = {
        "status":  "running",
        "message": f"Démarrage {connector.upper()}…",
        "created": time.time(),
        "rows":    [],
        "total":   0,
    }

    background_tasks.add_task(_run_job_async, job_id, user_id, connector, job_key, creds)
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job inconnu ou expiré")
    return job


# ── Conn test (async wrapper) ──────────────────────────────────────────────────

async def _run_conn_test_async(user_id: str, connector: str, creds: dict):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_executor, lambda: _test_connector(connector, creds))
        await save_user_creds(user_id, connector, creds["user"], creds["pass"], True)
        await patch_conn_test(user_id, connector, True, "Connexion réussie")
    except Exception as e:
        await patch_conn_test(user_id, connector, False, str(e))


# ── Test connector (synchronous, called from executor) ─────────────────────────

def _test_connector(connector: str, creds: dict):
    if connector == "ospharm":
        from test_connector import test_ospharm
        test_ospharm(creds)
    elif connector == "digipharmacie":
        from test_connector import test_digipharmacie
        test_digipharmacie(creds)


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
        rows, file_url = result if isinstance(result, tuple) else (result, "")
        msg = f"{len(rows)} lignes extraites"
        _jobs[job_id].update({
            "status":   "done",
            "message":  msg,
            "rows":     rows,
            "total":    len(rows),
            "file_url": file_url,
        })
        await patch_job_status(user_id, job_key, "done", msg, rows, file_url)
    except Exception as e:
        _jobs[job_id].update({"status": "error", "message": str(e), "error": str(e)})
        await patch_job_status(user_id, job_key, "error", str(e), [])


def _scrape(connector: str, user_id: str, creds: dict, progress):
    if connector == "digipharmacie":
        from scraper import run_scraper
        return run_scraper(creds, progress)
    elif connector == "ospharm":
        from run_job_ospharm import run_ospharm
        return run_ospharm(creds, progress, user_id=user_id)
    raise RuntimeError(f"Connecteur inconnu : {connector}")
