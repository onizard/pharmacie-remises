"""
Supabase helpers — vérification JWT + lecture/écriture de user_state
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request

SUPA_URL    = "https://api.break-pharma.fr"
SUPA_KEY    = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYW5vbiIsImlzcyI6InN1cGFiYXNlLXNlbGYiLCJpYXQiOjE3ODM1NDU0MjV9.Ga5ubKMU5mnlcBncdb1TUgprBHxuDkRw0LBmGP81XwM"
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Clé HMAC-SHA256 utilisée par GoTrue pour signer les JWT utilisateurs.
# JAMAIS de valeur par défaut ici : le secret ne doit PAS vivre dans le dépôt public.
# Il est fourni par la variable d'environnement GOTRUE_JWT_SECRET (Render).
_JWT_SECRET = os.environ.get("GOTRUE_JWT_SECRET", "")


def _b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _verify_jwt_local(token: str) -> dict:
    """Vérifie la signature HMAC-SHA256 du JWT GoTrue localement — sans appel réseau."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("JWT mal formé")
        header_b64, payload_b64, sig_b64 = parts
        expected_sig = hmac.new(
            _JWT_SECRET.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
        ).digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            raise ValueError("Signature JWT invalide")
        payload = json.loads(_b64url_decode(payload_b64))
        exp = payload.get("exp")
        if exp and time.time() > exp:
            raise ValueError("JWT expiré")
        return payload
    except (ValueError, KeyError, Exception) as e:
        raise ValueError(f"JWT invalide : {e}")


async def verify_token(token: str) -> str:
    """Vérifie le token JWT GoTrue et retourne le user_id (sub)."""
    if not token:
        raise ValueError("Token manquant")
    payload = _verify_jwt_local(token)
    uid = payload.get("sub")
    if not uid:
        raise ValueError("Token invalide — aucun sub (user_id)")
    return uid


def _supa_key() -> str:
    # SERVICE_KEY ne doit être utilisé que si c'est un JWT valide (commence par eyJ).
    # Les clés cloud Supabase au format sb_secret_... ne sont pas des JWT et ne
    # fonctionnent pas avec le PostgREST self-hosted → on revient sur la clé anon.
    if SERVICE_KEY and SERVICE_KEY.startswith("eyJ"):
        return SERVICE_KEY
    return SUPA_KEY


def _get_state_sync(user_id: str, user_token: str = "") -> dict:
    key   = _supa_key()
    bearer = user_token or key
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}&select=state_json&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey":        key,
        "Authorization": f"Bearer {bearer}",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        rows = json.loads(resp.read())
    return rows[0]["state_json"] if rows else {}


def _patch_state_sync(user_id: str, state: dict, user_token: str = ""):
    key    = _supa_key()
    bearer = user_token or key
    url  = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}"
    body = json.dumps({"state_json": state}).encode()
    req  = urllib.request.Request(url, data=body, method="PATCH", headers={
        "apikey":        key,
        "Authorization": f"Bearer {bearer}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    })
    # Payload can be large (job rows) — retry up to 3 times with increasing timeouts
    last_err = None
    for attempt, timeout in enumerate((30, 60, 90), 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return
        except Exception as e:
            last_err = e
            print(f"  [warn] Supabase update failed (attempt {attempt}/3) : {e}", flush=True)
    raise RuntimeError(f"Supabase patch failed after 3 attempts : {last_err}")


def _upsert_connector_sync(user_id: str, connector: str, user: str, password: str, connected: bool,
                           user_token: str = ""):
    """Atomic upsert via RPC — no read-modify-write, no race conditions."""
    key    = _supa_key()
    bearer = user_token or key
    url  = f"{SUPA_URL}/rest/v1/rpc/upsert_connector"
    body = json.dumps({
        "p_user_id":   user_id,
        "p_connector": connector,
        "p_login":     user,
        "p_pass":      password,
        "p_connected": connected,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey":        key,
        "Authorization": f"Bearer {bearer}",
        "Content-Type":  "application/json",
    })
    with urllib.request.urlopen(req, timeout=15):
        pass


def _get_connectors_sync(user_id: str, user_token: str = "") -> dict:
    """Read only the connectors column (lighter than full state_json)."""
    key    = _supa_key()
    bearer = user_token or key
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{user_id}&select=connectors&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": key, "Authorization": f"Bearer {bearer}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return (rows[0].get("connectors") or {}) if rows else {}


async def get_user_creds_for(user_id: str, connector: str, user_token: str = "") -> dict:
    loop = asyncio.get_event_loop()
    # Try new connectors column first, fall back to state_json for legacy rows
    conns = await loop.run_in_executor(None, lambda: _get_connectors_sync(user_id, user_token))
    conn  = conns.get(connector, {})
    if not conn.get("user") or not conn.get("pass"):
        # Legacy fallback
        state = await loop.run_in_executor(None, lambda: _get_state_sync(user_id, user_token))
        conn  = state.get("connectors", {}).get(connector, {})
    if not conn.get("user") or not conn.get("pass"):
        raise ValueError(
            f"Identifiants {connector.upper()} manquants — "
            "configure-les dans break-pharma.fr → CONNECTEURS"
        )
    return {"user": conn["user"], "pass": conn["pass"]}


async def save_user_creds(user_id: str, connector: str, user: str, password: str, connected: bool,
                          user_token: str = ""):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, lambda: _upsert_connector_sync(user_id, connector, user, password, connected, user_token)
    )


async def patch_connector_connected(user_id: str, connector: str, connected: bool):
    # Read current pass to preserve it, then upsert with updated connected flag
    loop  = asyncio.get_event_loop()
    conns = await loop.run_in_executor(None, lambda: _get_connectors_sync(user_id))
    conn  = conns.get(connector, {})
    if not conn.get("user"):
        state = await loop.run_in_executor(None, lambda: _get_state_sync(user_id))
        conn  = state.get("connectors", {}).get(connector, {})
    await loop.run_in_executor(
        None, lambda: _upsert_connector_sync(
            user_id, connector, conn.get("user", ""), conn.get("pass", ""), connected
        )
    )


async def patch_conn_test(user_id: str, connector: str, ok: bool, message: str,
                          user_token: str = ""):
    loop  = asyncio.get_event_loop()
    state = await loop.run_in_executor(None, lambda: _get_state_sync(user_id, user_token))
    state.setdefault("conn_test", {})[connector] = {
        "status":  "ok" if ok else "fail",
        "message": message,
    }
    await loop.run_in_executor(None, lambda: _patch_state_sync(user_id, state, user_token))


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
        # Supabase returns 400 with statusCode:409 in body when bucket already exists
        body = e.read()
        is_duplicate = e.code == 409 or b"already" in body.lower() or b"duplicate" in body.lower()
        if not is_duplicate:
            raise


def upload_file_sync(user_id: str, connector: str, filename: str, data: bytes, content_type: str) -> str:
    """Upload file to Supabase Storage, return storage path."""
    _ensure_bucket_sync()
    key  = _supa_key()
    path = f"{user_id}/{connector}/{filename}"
    url  = f"{SUPA_URL}/storage/v1/object/{STORAGE_BUCKET}/{path}"
    hdrs = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": content_type}
    # POST with x-upsert; some content-types (xlsx) fail upsert → fall back to PUT
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={**hdrs, "x-upsert": "true"})
    try:
        with urllib.request.urlopen(req, timeout=120): pass
    except urllib.request.HTTPError as e:
        body = e.read()
        if e.code in (400, 409) and b"Duplicate" in body:
            req2 = urllib.request.Request(url, data=data, method="PUT", headers=hdrs)
            with urllib.request.urlopen(req2, timeout=120): pass
        else:
            raise
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
    # Supabase retourne "signedURL" (avec URL en majuscules) — fallback sur les variantes
    signed = (result.get("signedURL") or result.get("signedUrl")
              or result.get("signed_url") or "")
    if not signed:
        raise ValueError(f"Supabase Storage sign: réponse inattendue {result}")
    return f"{SUPA_URL}{signed}" if signed.startswith("/") else signed


async def patch_job_status(user_id: str, job_key: str, status: str, message: str, data: list,
                           file_url: str = "", period_start: str = "", period_end: str = "",
                           user_token: str = ""):
    loop  = asyncio.get_event_loop()
    state = await loop.run_in_executor(None, lambda: _get_state_sync(user_id, user_token))
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
    await loop.run_in_executor(None, lambda: _patch_state_sync(user_id, state, user_token))
