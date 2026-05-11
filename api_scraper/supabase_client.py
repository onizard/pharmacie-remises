"""
Supabase helpers — vérification JWT + récupération des creds DIGIPHARMACIE
"""

import json
import os
import urllib.request

SUPA_URL = "https://fmterazwesiwpwjpkyqi.supabase.co"
SUPA_KEY = "sb_publishable_F5yfQriBSH3KY7elhyXhLQ_rQ_9P92w"


async def verify_token(token: str) -> str:
    """
    Appelle /auth/v1/user avec le token de l'utilisateur.
    Retourne le user_id (UUID) ou lève ValueError.
    """
    url = f"{SUPA_URL}/auth/v1/user"
    req = urllib.request.Request(
        url,
        headers={
            "apikey":        SUPA_KEY,
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        uid = data.get("id")
        if not uid:
            raise ValueError("Token invalide — aucun user_id retourné")
        return uid
    except urllib.request.HTTPError as e:
        raise ValueError(f"Token refusé par Supabase : HTTP {e.code}")


async def get_user_creds(token: str, user_id: str) -> dict:
    """
    Lit state_json depuis la table user_state pour l'utilisateur donné.
    Retourne {"user": "...", "pass": "..."} pour DIGIPHARMACIE.
    """
    url = (
        f"{SUPA_URL}/rest/v1/user_state"
        f"?user_id=eq.{user_id}&select=state_json&limit=1"
    )
    req = urllib.request.Request(
        url,
        headers={
            "apikey":        SUPA_KEY,
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        rows = json.loads(resp.read())

    if not rows:
        raise ValueError("Aucun état utilisateur trouvé dans Supabase")

    state      = rows[0]["state_json"]
    connectors = state.get("connectors", {})
    digi       = connectors.get("digipharmacie", {})

    if not digi.get("user") or not digi.get("pass"):
        raise ValueError(
            "Identifiants DIGIPHARMACIE manquants — "
            "configure-les dans break-pharma.fr → CONNECTEUR"
        )

    return {"user": digi["user"], "pass": digi["pass"]}
