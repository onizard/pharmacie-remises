"""
get_connectors.py — Récupère les identifiants connecteurs de l'utilisateur depuis Supabase.

Le fichier .env doit contenir :
    BP_EMAIL=votre_email_break_pharma
    BP_PASSWORD=votre_mot_de_passe_break_pharma
"""
import os
import json
import urllib.request
from pathlib import Path

SUPA_URL = "https://fmterazwesiwpwjpkyqi.supabase.co"
SUPA_KEY = "sb_publishable_F5yfQriBSH3KY7elhyXhLQ_rQ_9P92w"


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


def get_connectors():
    """
    Authentifie l'utilisateur sur Supabase avec BP_EMAIL / BP_PASSWORD,
    récupère son user_state et retourne ses identifiants connecteurs.

    Retourne :
        {
            "ospharm":       {"user": "...", "pass": "..."},
            "digipharmacie": {"user": "...", "pass": "..."},
        }
    """
    load_env()

    email    = os.environ.get("BP_EMAIL", "")
    password = os.environ.get("BP_PASSWORD", "")

    if not email or not password:
        raise ValueError(
            "BP_EMAIL et BP_PASSWORD doivent être définis dans le fichier .env\n"
            "  (ce sont vos identifiants de connexion à break-pharma.fr)"
        )

    # ── 1. Authentification Supabase ──────────────────────────────────────────
    auth_url  = f"{SUPA_URL}/auth/v1/token?grant_type=password"
    auth_body = json.dumps({"email": email, "password": password}).encode()
    auth_req  = urllib.request.Request(
        auth_url, data=auth_body,
        headers={"apikey": SUPA_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(auth_req, timeout=15) as resp:
            auth_data = json.loads(resp.read())
    except urllib.request.HTTPError as e:
        body = e.read().decode(errors="ignore")
        raise ValueError(f"Échec d'authentification Supabase : {e.code} — {body}")

    token   = auth_data["access_token"]
    user_id = auth_data["user"]["id"]
    print(f"  ✓  Authentifié en tant que {email}")

    # ── 2. Récupération du state utilisateur ──────────────────────────────────
    state_url = (f"{SUPA_URL}/rest/v1/user_state"
                 f"?user_id=eq.{user_id}&select=state_json&limit=1")
    state_req = urllib.request.Request(
        state_url,
        headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {token}"},
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
