"""
API FastAPI — Scraper DIGIPHARMACIE + extraction PDF

Endpoints :
  POST /scrape          → lance le job en arrière-plan, retourne {job_id}
  GET  /status/{job_id} → retourne le statut + les données extraites quand terminé

Authentification : Bearer token Supabase (JWT de l'utilisateur break-pharma.fr)
"""

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from scraper import run_scraper
from supabase_client import get_user_creds, verify_token

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

# ── Job store (in-memory) ──────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}
JOB_TTL   = 3600        # 1 h — nettoyage automatique
_executor = ThreadPoolExecutor(max_workers=3)


def _cleanup_jobs():
    cutoff = time.time() - JOB_TTL
    stale  = [jid for jid, j in _jobs.items() if j.get("created", 0) < cutoff]
    for jid in stale:
        del _jobs[jid]


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/scrape")
async def start_scrape(
    background_tasks: BackgroundTasks,
    authorization: str = Header(default=""),
):
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token manquant")

    try:
        user_id = await verify_token(token)
        creds   = await get_user_creds(token, user_id)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    _cleanup_jobs()

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status":  "running",
        "message": "Connexion à DIGIPHARMACIE…",
        "created": time.time(),
        "invoices": [],
    }

    background_tasks.add_task(_run_job_async, job_id, creds)
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job inconnu ou expiré")
    return job


# ── Background job ─────────────────────────────────────────────────────────────

async def _run_job_async(job_id: str, creds: dict):
    loop = asyncio.get_event_loop()
    try:
        invoices = await loop.run_in_executor(
            _executor,
            lambda: run_scraper(creds, lambda msg: _update_job(job_id, msg)),
        )
        _jobs[job_id].update({"status": "done", "invoices": invoices, "message": "Terminé"})
    except Exception as e:
        _jobs[job_id].update({"status": "error", "error": str(e)})


def _update_job(job_id: str, message: str):
    if job_id in _jobs:
        _jobs[job_id]["message"] = message
