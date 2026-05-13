"""
Supabase helpers — vérification JWT + lecture/écriture de user_state
"""

import asyncio
import json
import os
import urllib.request

SUPA_URL    = "https://fmterazwesiwpwjpkyqi.supabase.co"
SUPA_KEY    = "sb_publishable_F5yfQriBSH3KY7elhyXhLQ_rQ_9P92w"
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


async def verify_token(token: str) -> str:
    url = f"{SUPA_URL}/auth/v1/user"
    req = urllib.request.Request(url, headers={
        "apikey":        SUPA_KEY,
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        uid = data.get("id")
        if not uid:
            raise ValueError("Token invalide — aucun user_id retourné")
        return uid
    except urllib.request.HTTPError as e:
        raise ValueError(f"Token refusé par Supabase : HTTP {e.code}")


def _supa_key() -> str:
    return SERVICE_KEY or SUPA_KEY


def _get_state_sync(user_id: str) -> dict:
    key = _supa_key()
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}&select=state_json&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey":        key,
        "Authorization": f"Bearer {key}",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        rows = json.loads(resp.read())
    return rows[0]["state_json"] if rows else {}


def _patch_state_sync(user_id: str, state: dict):
    key  = _supa_key()
    url  = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}"
    body = json.dumps({"state_json": state}).encode()
    req  = urllib.request.Request(url, data=body, method="PATCH", headers={
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    })
    with urllib.request.urlopen(req, timeout=15):
        pass


async def get_user_creds_for(user_id: str, connector: str) -> dict:
    loop  = asyncio.get_event_loop()
    state = await loop.run_in_executor(None, lambda: _get_state_sync(user_id))
    conn  = state.get("connectors", {}).get(connector, {})
    if not conn.get("user") or not conn.get("pass"):
        raise ValueError(
            f"Identifiants {connector.upper()} manquants — "
            "configure-les dans break-pharma.fr → CONNECTEURS"
        )
    return {"user": conn["user"], "pass": conn["pass"]}


async def save_user_creds(user_id: str, connector: str, user: str, password: str, connected: bool):
    loop  = asyncio.get_event_loop()
    state = await loop.run_in_executor(None, lambda: _get_state_sync(user_id))
    state.setdefault("connectors", {})[connector] = {
        "user":      user,
        "pass":      password,
        "connected": connected,
    }
    await loop.run_in_executor(None, lambda: _patch_state_sync(user_id, state))


async def patch_connector_connected(user_id: str, connector: str, connected: bool):
    loop  = asyncio.get_event_loop()
    state = await loop.run_in_executor(None, lambda: _get_state_sync(user_id))
    state.setdefault("connectors", {}).setdefault(connector, {})["connected"] = connected
    await loop.run_in_executor(None, lambda: _patch_state_sync(user_id, state))


async def patch_conn_test(user_id: str, connector: str, ok: bool, message: str):
    loop  = asyncio.get_event_loop()
    state = await loop.run_in_executor(None, lambda: _get_state_sync(user_id))
    state.setdefault("conn_test", {})[connector] = {
        "status":  "ok" if ok else "fail",
        "message": message,
    }
    await loop.run_in_executor(None, lambda: _patch_state_sync(user_id, state))


STORAGE_BUCKET = "bp-files"


def _ensure_bucket_sync():
    key = _supa_key()
    url = f"{SUPA_URL}/storage/v1/bucket"
    body = json.dumps({"id": STORAGE_BUCKET, "name": STORAGE_BUCKET, "public": False}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10): pass
    except urllib.request.HTTPError as e:
        if e.code != 409:  # 409 = already exists
            raise


def upload_file_sync(user_id: str, connector: str, filename: str, data: bytes, content_type: str) -> str:
    """Upload file to Supabase Storage, return storage path."""
    _ensure_bucket_sync()
    key  = _supa_key()
    path = f"{user_id}/{connector}/{filename}"
    url  = f"{SUPA_URL}/storage/v1/object/{STORAGE_BUCKET}/{path}"
    req  = urllib.request.Request(url, data=data, method="POST", headers={
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": content_type, "x-upsert": "true",
    })
    with urllib.request.urlopen(req, timeout=60): pass
    return path


def get_signed_url_sync(path: str, expires_in: int = 2_592_000) -> str:
    """Create a 30-day signed URL for a file in Supabase Storage."""
    key  = _supa_key()
    url  = f"{SUPA_URL}/storage/v1/object/sign/{STORAGE_BUCKET}/{path}"
    body = json.dumps({"expiresIn": expires_in}).encode()
    req  = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.loads(r.read())
    signed = result.get("signedURL", "")
    return f"{SUPA_URL}{signed}" if signed.startswith("/") else signed


async def patch_job_status(user_id: str, job_key: str, status: str, message: str, data: list,
                           file_url: str = "", period_start: str = "", period_end: str = ""):
    loop  = asyncio.get_event_loop()
    state = await loop.run_in_executor(None, lambda: _get_state_sync(user_id))
    job = {
        "status":   status,
        "message":  message,
        "rows":     data,
        "total":    len(data),
        "invoices": data,
        "error":    "" if status != "error" else message,
        "file_url": file_url,
    }
    if period_start:
        job["period_start"] = period_start
        job["period_end"]   = period_end
    state[job_key] = job
    await loop.run_in_executor(None, lambda: _patch_state_sync(user_id, state))
