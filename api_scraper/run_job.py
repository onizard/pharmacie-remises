"""
run_job.py — Exécuté par GitHub Actions.

Variables d'environnement requises :
    USER_ID               Supabase user UUID (passé en input du workflow)
    SUPABASE_SERVICE_KEY  Clé de service Supabase (GitHub Secret)
"""

import json
import os
import sys
import urllib.request

# Permettre l'import de scraper.py dans le même dossier
sys.path.insert(0, os.path.dirname(__file__))
from scraper import run_scraper  # noqa: E402

SUPA_URL    = "https://fmterazwesiwpwjpkyqi.supabase.co"
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
USER_ID     = os.environ["USER_ID"]


# ── Supabase helpers ───────────────────────────────────────────────────────────

def _supa_get_state() -> dict:
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=state_json&limit=1"
    req = urllib.request.Request(
        url,
        headers={
            "apikey":        SERVICE_KEY,
            "Authorization": f"Bearer {SERVICE_KEY}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        rows = json.loads(resp.read())
    return rows[0]["state_json"] if rows else {}


def _supa_patch_state(state: dict):
    url  = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}"
    body = json.dumps({"state_json": state}).encode()
    req  = urllib.request.Request(
        url, data=body, method="PATCH",
        headers={
            "apikey":        SERVICE_KEY,
            "Authorization": f"Bearer {SERVICE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=15):
        pass


_LABO_NORMALIZE = {
    "biogaran": "BIOGARAN",
    "teva": "TEVA",
    "mylan": "MYLAN",
    "viatris": "VIATRIS",
    "zydus": "ZYDUS",
    "sandoz": "SANDOZ",
    "zentiva": "ZENTIVA",
    "arrow": "ARROW",
    "cristers": "CRISTERS",
    "eg labo": "EG LABO",
    "eg labs": "EG LABO",
    "evolupharm": "EVOLUPHARM",
}


def _norm_labo(raw: str) -> str:
    n = (raw or "").lower().strip()
    for kw, canonical in _LABO_NORMALIZE.items():
        if kw in n:
            return canonical
    return n.upper()


def _compute_digi_month_stats(lines: list[dict]) -> dict:
    """Agrège les lignes digi → {year-MM: [{labo, qty, total_ht}]}."""
    acc: dict[str, dict] = {}
    for line in lines:
        date = str(line.get("billing_date", ""))
        if len(date) < 7:
            continue
        mk    = date[:7]
        labo  = _norm_labo(line.get("labo") or line.get("fournisseur") or "")
        qty   = int(line.get("quantite") or 0)
        total = float(line.get("total_ht") or 0)
        acc.setdefault(mk, {}).setdefault(labo, {"qty": 0, "total_ht": 0.0})
        acc[mk][labo]["qty"]      += qty
        acc[mk][labo]["total_ht"] += total
    return {
        mk: sorted(
            [{"labo": labo, "qty": d["qty"], "total_ht": round(d["total_ht"], 2)}
             for labo, d in labos.items()],
            key=lambda r: r["labo"],
        )
        for mk, labos in acc.items()
    }


def _update_job(status: str, message: str = "", invoices=None, error: str = ""):
    try:
        state = _supa_get_state()
        state["verif_job"] = {
            "status":   status,
            "message":  message,
            "invoices": invoices or [],
            "error":    error,
        }
        _supa_patch_state(state)
    except Exception as e:
        print(f"  [warn] Supabase update failed : {e}")


def _get_connectors_col() -> dict:
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=connectors&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return (rows[0].get("connectors") or {}) if rows else {}


def _get_creds() -> dict:
    # Priorité 1 : colonne connectors (mise à jour atomique via upsert_connector RPC)
    try:
        conns = _get_connectors_col()
        cred  = conns.get("digipharmacie", {})
        if cred.get("user") and cred.get("pass"):
            return {"user": cred["user"], "pass": cred["pass"]}
    except Exception:
        pass

    # Priorité 2 : state_json.connectors (fallback — peut être périmé si saveCloudState a timeout)
    try:
        state  = _supa_get_state()
        digi   = state.get("connectors", {}).get("digipharmacie", {})
        user   = digi.get("user", "")
        passwd = digi.get("pass", "")
        if user and passwd:
            return {"user": user, "pass": passwd}
    except Exception:
        pass

    raise ValueError(
        "Identifiants DIGIPHARMACIE manquants dans Supabase.\n"
        "Configure-les dans break-pharma.fr → CONNECTEUR."
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def _save_results(lines: list, cache: dict, partial: bool = False):
    """Sauvegarde les lignes + cache dans verif_job. Marque 'done' même si partiel."""
    label = f"Partiel : {len(lines)} lignes ({len(cache)} factures traitées)" if partial \
            else f"{len(lines)} lignes extraites"
    state = _supa_get_state()
    state["verif_job"] = {
        "status":        "done",
        "message":       label,
        "invoices":      lines,
        "invoice_cache": cache,
        "error":         "",
    }
    digi_stats = _compute_digi_month_stats(lines)
    if digi_stats:
        state["digi_month_stats"] = digi_stats
        months = sorted(digi_stats)
        print(f"  📊  digi_month_stats : {len(digi_stats)} mois ({months[0]} → {months[-1]})")
    _supa_patch_state(state)


def main():
    import signal as _sig

    # État partagé — mis à jour après chaque PDF pour que SIGTERM puisse sauver
    _partial: dict = {"lines": [], "cache": {}}

    def _on_partial(lines: list, cache: dict):
        _partial["lines"] = lines
        _partial["cache"] = cache

    # Handler SIGTERM : GitHub Actions tue le job à timeout-minutes
    # Sauve les résultats partiels pour que la prochaine run reprenne en cache
    def _on_sigterm(sig, frame):
        print("\n⚠️  SIGTERM — sauvegarde des résultats partiels…", flush=True)
        try:
            _save_results(_partial["lines"], _partial["cache"], partial=True)
            print(f"  ✓ {len(_partial['lines'])} lignes / {len(_partial['cache'])} factures sauvées", flush=True)
        except Exception as _e:
            print(f"  ✗ Sauvegarde partielle échouée : {_e}", flush=True)
        sys.exit(1)

    _sig.signal(_sig.SIGTERM, _on_sigterm)

    print(f"🚀  Job démarré pour user_id={USER_ID}")
    _update_job("running", "Initialisation…")

    try:
        creds = _get_creds()
        print(f"  → Credentials chargés : user={creds['user'][:4]}*** pass={'ok' if creds.get('pass') else 'VIDE'}")
    except ValueError as e:
        _update_job("error", error=str(e))
        sys.exit(1)

    # Charger le cache incrémental depuis la dernière run
    try:
        _ex_state = _supa_get_state()
        existing_cache = (_ex_state.get("verif_job") or {}).get("invoice_cache") or {}
        print(f"  → Cache incrémental : {len(existing_cache)} factures déjà traitées")
    except Exception:
        existing_cache = {}

    def progress(msg: str):
        print(f"  → {msg}")
        _update_job("running", msg)

    try:
        invoices, updated_cache = run_scraper(creds, progress,
                                              invoice_cache=existing_cache,
                                              on_partial=_on_partial)
        _save_results(invoices, updated_cache)
        print(f"\n✅  {len(invoices)} lignes sauvegardées ({len(updated_cache)} factures en cache).")
    except Exception as e:
        _update_job("error", error=str(e))
        print(f"\n❌  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
