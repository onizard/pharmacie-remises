"""
get_connectors.py — Récupère les identifiants connecteurs de l'utilisateur depuis Supabase.

Le fichier .env doit contenir :
    BP_EMAIL=votre_email_break_pharma
    BP_PASSWORD=votre_mot_de_passe_break_pharma  (optionnel si SUPABASE_SERVICE_KEY présent)
"""
import os
import json
import urllib.request
from pathlib import Path

SUPA_URL     = "https://fmterazwesiwpwjpkyqi.supabase.co"
SUPA_KEY     = "sb_publishable_F5yfQriBSH3KY7elhyXhLQ_rQ_9P92w"


def load_env(filepath=None):
    env_path = Path(filepath) if filepath else Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ[key.strip()] = value


def _fetch_user_id_by_email(email, service_key):
    """Use the Supabase admin API to find a user's UUID by email."""
    url = f"{SUPA_URL}/auth/v1/admin/users?page=1&per_page=1000"
    req = urllib.request.Request(
        url,
        headers={
            "apikey":         service_key,
            "Authorization":  f"Bearer {service_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    users = data if isinstance(data, list) else data.get("users", [])
    for u in users:
        if u.get("email", "").lower() == email.lower():
            return u["id"]
    raise ValueError(f"Aucun utilisateur avec l'email {email!r} dans Supabase.")


def get_connectors():
    """
    Récupère le user_state Supabase et retourne les identifiants connecteurs.

    Tente d'abord une auth par mot de passe (BP_PASSWORD).
    Si elle échoue ou si BP_PASSWORD est absent, utilise SUPABASE_SERVICE_KEY
    pour rechercher l'utilisateur directement via l'API admin.

    Retourne :
        {
            "ospharm":       {"user": "...", "pass": "..."},
            "digipharmacie": {"user": "...", "pass": "..."},
        }
    """
    load_env()

    email       = os.environ.get("BP_EMAIL", "")
    password    = os.environ.get("BP_PASSWORD", "")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")

    if not email:
        raise ValueError("BP_EMAIL doit être défini dans le fichier .env")

    token   = None
    user_id = None

    # ── 1a. Tentative d'auth par mot de passe ─────────────────────────────────
    if password:
        auth_url  = f"{SUPA_URL}/auth/v1/token?grant_type=password"
        auth_body = json.dumps({"email": email, "password": password}).encode()
        auth_req  = urllib.request.Request(
            auth_url, data=auth_body,
            headers={"apikey": SUPA_KEY, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(auth_req, timeout=15) as resp:
                auth_data = json.loads(resp.read())
            token   = auth_data["access_token"]
            user_id = auth_data["user"]["id"]
            print(f"  ✓  Authentifié en tant que {email}")
        except urllib.request.HTTPError:
            pass  # fall through to service-key path

    # ── 1b. Fallback via clé de service ───────────────────────────────────────
    if user_id is None:
        if not service_key:
            raise ValueError(
                "Échec d'authentification Supabase.\n"
                "Vérifie BP_PASSWORD dans .env, ou ajoute SUPABASE_SERVICE_KEY."
            )
        user_id = _fetch_user_id_by_email(email, service_key)
        token   = service_key
        print(f"  ✓  Utilisateur trouvé via clé de service ({email})")

    # ── 2. Récupération du state utilisateur ──────────────────────────────────
    api_key   = service_key if (token == service_key) else SUPA_KEY
    state_url = (f"{SUPA_URL}/rest/v1/user_state"
                 f"?user_id=eq.{user_id}&select=state_json&limit=1")
    state_req = urllib.request.Request(
        state_url,
        headers={"apikey": api_key, "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(state_req, timeout=15) as resp:
        rows = json.loads(resp.read())

    if not rows:
        raise ValueError(
            "Aucun état trouvé pour cet utilisateur dans Supabase.\n"
            "Connecte-toi à break-pharma.fr et sauvegarde tes identifiants "
            "connecteurs dans la modale CONNECTEUR."
        )

    state      = rows[0]["state_json"]
    connectors = state.get("connectors", {})

    ospharm       = connectors.get("ospharm",       {"user": "", "pass": ""})
    digipharmacie = connectors.get("digipharmacie", {"user": "", "pass": ""})

    if not ospharm.get("user") and not digipharmacie.get("user"):
        raise ValueError(
            "Aucun identifiant connecteur trouvé.\n"
            "Remplis-les dans break-pharma.fr → bouton CONNECTEUR."
        )

    return {"ospharm": ospharm, "digipharmacie": digipharmacie}


if __name__ == "__main__":
    try:
        creds = get_connectors()
        print(f"  OSPHARM       : {creds['ospharm']['user'] or '(vide)'}")
        print(f"  DIGIPHARMACIE : {creds['digipharmacie']['user'] or '(vide)'}")
    except Exception as e:
        print(f"❌  {e}")
