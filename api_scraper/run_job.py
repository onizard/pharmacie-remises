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

SUPA_URL    = "https://api.break-pharma.fr"
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
    """Agrège les lignes digi → {year-MM: [{labo, qty, total_ht, rdp_total, presta_total}]}.

    - Lignes produits  : clé = billing_date[:7], cumule qty + total_ht
    - Lignes rdp       : clé = period_month,     cumule rdp_total (valeur absolue)
    - Lignes presta    : clé = period_month,      cumule presta_total
    """
    def _zero():
        return {"qty": 0, "total_ht": 0.0, "rdp_total": 0.0, "presta_total": 0.0, "presta_total_ttc": 0.0}

    acc: dict[str, dict] = {}
    for line in lines:
        line_type = line.get("type")

        if line_type == "rdp":
            mk   = str(line.get("period_month") or line.get("billing_date", ""))[:7]
            labo = _norm_labo(line.get("labo") or "")
            if len(mk) < 7 or not labo: continue
            amt  = abs(float(line.get("montant") or 0))   # montant négatif → valeur absolue
            acc.setdefault(mk, {}).setdefault(labo, _zero())
            acc[mk][labo]["rdp_total"] = round(acc[mk][labo]["rdp_total"] + amt, 2)

        elif line_type == "presta":
            mk   = str(line.get("period_month") or line.get("billing_date", ""))[:7]
            labo = _norm_labo(line.get("labo") or "")
            if len(mk) < 7 or not labo: continue
            amt_ht  = float(line.get("total_ht")  or line.get("montant") or 0)
            amt_ttc = float(line.get("total_ttc") or amt_ht * (1 + float(line.get("tva_pct", 20)) / 100))
            acc.setdefault(mk, {}).setdefault(labo, _zero())
            acc[mk][labo]["presta_total"]     = round(acc[mk][labo]["presta_total"]     + amt_ht,  2)
            acc[mk][labo]["presta_total_ttc"] = round(acc[mk][labo]["presta_total_ttc"] + amt_ttc, 2)

        else:
            date = str(line.get("billing_date", ""))
            if len(date) < 7: continue
            mk   = date[:7]
            labo = _norm_labo(line.get("labo") or line.get("fournisseur") or "")
            qty  = int(line.get("quantite") or 0)
            tot  = float(line.get("total_ht") or 0)
            acc.setdefault(mk, {}).setdefault(labo, _zero())
            acc[mk][labo]["qty"]      += qty
            acc[mk][labo]["total_ht"]  = round(acc[mk][labo]["total_ht"] + tot, 2)

    return {
        mk: sorted(
            [{"labo":         labo,
              "qty":              d["qty"],
              "total_ht":         round(d["total_ht"], 2),
              "rdp_total":        round(d["rdp_total"], 2),
              "presta_total":     round(d["presta_total"], 2),
              "presta_total_ttc": round(d.get("presta_total_ttc", d["presta_total"] * 1.20), 2)}
             for labo, d in labos.items()],
            key=lambda r: r["labo"],
        )
        for mk, labos in acc.items()
    }


def _merge_digi_stats(existing: dict, new_partial: dict) -> dict:
    """Fusionne les nouvelles stats partielles avec les stats existantes."""
    result = {mk: [dict(r) for r in rows] for mk, rows in existing.items()}
    for mk, new_rows in new_partial.items():
        if mk not in result:
            result[mk] = new_rows
        else:
            labo_map = {r["labo"]: r for r in result[mk]}
            for nr in new_rows:
                labo = nr["labo"]
                if labo in labo_map:
                    labo_map[labo]["qty"]          += nr["qty"]
                    labo_map[labo]["total_ht"]       = round(labo_map[labo]["total_ht"]       + nr["total_ht"],     2)
                    labo_map[labo]["rdp_total"]          = round(labo_map[labo].get("rdp_total",0)          + nr.get("rdp_total",0),          2)
                    labo_map[labo]["presta_total"]       = round(labo_map[labo].get("presta_total",0)       + nr.get("presta_total",0),       2)
                    labo_map[labo]["presta_total_ttc"]   = round(labo_map[labo].get("presta_total_ttc",0)   + nr.get("presta_total_ttc",0),   2)
                else:
                    labo_map[labo] = dict(nr)
            result[mk] = sorted(labo_map.values(), key=lambda r: r["labo"])
    return result


def _compute_escompte_stats(lines: list[dict]) -> dict:
    """Agrège les lignes escompte → {year-MM: {fournisseur: {...}}}."""
    result: dict = {}
    for line in lines:
        if line.get("type") != "escompte":
            continue
        mk   = str(line.get("period_month") or line.get("billing_date", ""))[:7]
        four = line.get("fournisseur") or "CERP"
        if len(mk) < 7:
            continue
        result.setdefault(mk, {})
        existing = result[mk].get(four, {})
        result[mk][four] = {
            "ca_spec_gen_ht":     round((existing.get("ca_spec_gen_ht", 0)     + line.get("ca_spec_gen_ht", 0)),     2),
            "remise_spec_gen_ht": round((existing.get("remise_spec_gen_ht", 0) + line.get("remise_spec_gen_ht", 0)), 2),
            "remise_total_ht":    round((existing.get("remise_total_ht", 0)    + line.get("remise_total_ht", 0)),    2),
            "remise_total_ttc":   round((existing.get("remise_total_ttc", 0)   + line.get("remise_total_ttc", 0)),   2),
            "escompte_ttc":       round((existing.get("escompte_ttc", 0)       + line.get("escompte_ttc", 0)),       2),
            "total_ttc":          round((existing.get("total_ttc", 0)          + line.get("total_ttc", 0)),          2),
        }
    return result


def _merge_escompte_stats(existing: dict, new_partial: dict) -> dict:
    """Fusionne (last-write-wins par mois+fournisseur — un seul relevé par mois)."""
    result = {mk: dict(v) for mk, v in existing.items()}
    for mk, four_map in new_partial.items():
        result.setdefault(mk, {}).update(four_map)
    return result


def _compute_mdl_stats(lines: list[dict]) -> dict:
    """Agrège les lignes MDL → {year-MM: {labo: {ca_fab_mois, ca_fab_cumul}}}."""
    result: dict = {}
    for line in lines:
        if line.get("type") != "mdl":
            continue
        mk   = str(line.get("period_month") or line.get("billing_date", ""))[:7]
        labo = line.get("labo") or ""
        if len(mk) < 7 or not labo:
            continue
        result.setdefault(mk, {})[labo] = {
            "ca_fab_mois":   line.get("ca_fab_mois", 0.0),
            "ca_fab_cumul":  line.get("ca_fab_cumul", 0.0),
            "smr_gen_mois":  line.get("smr_gen_mois", 0.0),
            "smr_gen_cumul": line.get("smr_gen_cumul", 0.0),
            "smr_total_mois":line.get("smr_total_mois", 0.0),
        }
    return result


def _merge_mdl_stats(existing: dict, new_partial: dict) -> dict:
    """Last-write-wins par mois — un seul MDL par mois."""
    result = {mk: dict(v) for mk, v in existing.items()}
    for mk, labo_map in new_partial.items():
        result.setdefault(mk, {}).update(labo_map)
    return result


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

def _save_results(lines: list, cache: dict, existing_stats: dict,
                  partial: bool = False, existing_escompte: dict | None = None,
                  existing_mdl: dict | None = None):
    """Fusionne nouvelles lignes avec stats existantes, stocke le cache compact {key: True}."""
    n_cache = sum(1 for v in cache.values() if v is True)
    n_new   = len(lines)
    label   = (f"Partiel : {n_new} nouvelles lignes ({n_cache} en cache)"
               if partial else
               f"{n_new} nouvelles lignes ({n_cache} en cache)")
    state = _supa_get_state()
    compact_cache = {k: True for k in cache}
    state["verif_job"] = {
        "status":        "done",
        "message":       label,
        "invoices":      [],
        "invoice_cache": compact_cache,
        "error":         "",
    }
    # Stats factures produits/RDP/presta
    digi_lines    = [l for l in lines if l.get("type") not in ("escompte", "mdl")]
    new_partial   = _compute_digi_month_stats(digi_lines)
    merged        = _merge_digi_stats(existing_stats, new_partial)
    if merged:
        state["digi_month_stats"] = merged
        months = sorted(merged)
        print(f"  📊  digi_month_stats : {len(merged)} mois ({months[0]} → {months[-1]})")
    # Stats escomptes CERP
    escompte_lines = [l for l in lines if l.get("type") == "escompte"]
    if escompte_lines:
        new_esc    = _compute_escompte_stats(escompte_lines)
        merged_esc = _merge_escompte_stats(existing_escompte or {}, new_esc)
        state["escompte_stats"] = merged_esc
        print(f"  📊  escompte_stats : {len(merged_esc)} mois")
    # Stats MDL CERP
    mdl_lines = [l for l in lines if l.get("type") == "mdl"]
    if mdl_lines:
        new_mdl    = _compute_mdl_stats(mdl_lines)
        merged_mdl = _merge_mdl_stats(existing_mdl or {}, new_mdl)
        state["mdl_stats"] = merged_mdl
        print(f"  📊  mdl_stats : {len(merged_mdl)} mois")
    _supa_patch_state(state)


def main():
    import signal as _sig

    # État partagé — mis à jour après chaque PDF pour que SIGTERM puisse sauver
    _partial: dict = {"lines": [], "cache": {}, "existing_stats": {}, "existing_escompte": {}, "existing_mdl": {}}

    # Handler SIGTERM : GitHub Actions tue le job à timeout-minutes
    # Sauve les résultats partiels pour que la prochaine run reprenne en cache
    def _on_sigterm(sig, frame):
        print("\n⚠️  SIGTERM — sauvegarde des résultats partiels…", flush=True)
        try:
            _save_results(_partial["lines"], _partial["cache"],
                          _partial["existing_stats"], partial=True,
                          existing_escompte=_partial["existing_escompte"],
                          existing_mdl=_partial["existing_mdl"])
            print(f"  ✓ {len(_partial['lines'])} nouvelles lignes / {len(_partial['cache'])} en cache", flush=True)
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

    # Charger le cache compact et les stats existantes
    try:
        _ex_state      = _supa_get_state()
        existing_cache   = (_ex_state.get("verif_job") or {}).get("invoice_cache") or {}
        existing_stats   = _ex_state.get("digi_month_stats") or {}
        existing_escompte = _ex_state.get("escompte_stats") or {}
        existing_mdl      = _ex_state.get("mdl_stats")      or {}
        print(f"  → Cache : {len(existing_cache)} factures déjà traitées")
        print(f"  → Stats existantes : {len(existing_stats)} mois")
    except Exception:
        existing_cache, existing_stats, existing_escompte, existing_mdl = {}, {}, {}, {}

    _partial["existing_stats"]    = existing_stats
    _partial["existing_escompte"] = existing_escompte
    _partial["existing_mdl"]      = existing_mdl

    def _on_partial(lines: list, cache: dict):
        _partial["lines"] = lines
        _partial["cache"] = cache

    def progress(msg: str):
        print(f"  → {msg}")
        _update_job("running", msg)

    try:
        invoices, updated_cache = run_scraper(creds, progress,
                                              invoice_cache=existing_cache,
                                              on_partial=_on_partial)
        _save_results(invoices, updated_cache, existing_stats,
                      existing_escompte=existing_escompte, existing_mdl=existing_mdl)
        n_cache = sum(1 for v in updated_cache.values() if v is True)
        print(f"\n✅  {len(invoices)} nouvelles lignes ({n_cache} en cache).")
    except Exception as e:
        _update_job("error", error=str(e))
        print(f"\n❌  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
