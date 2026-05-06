"""
Synchronise remises_partenariat.xlsx et libelle_synonyms.json vers Supabase.
À lancer après chaque mise à jour de l'Excel ou des synonymes.

Usage :
    python sync_supabase.py
"""

import os
import json
import urllib.request
import urllib.error
import openpyxl
from pathlib import Path


def load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env()

SUPA_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BATCH    = 500


def rest(method: str, path: str, body=None):
    url  = f"{SUPA_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(url, data=data, method=method)
    req.add_header("apikey",        SUPA_KEY)
    req.add_header("Authorization", f"Bearer {SUPA_KEY}")
    req.add_header("Content-Type",  "application/json")
    req.add_header("Prefer",        "resolution=merge-duplicates")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {e.read().decode()}") from e


def sync():
    if not SUPA_URL or not SUPA_KEY:
        raise SystemExit("❌  SUPABASE_URL ou SUPABASE_SERVICE_KEY manquant dans .env")

    # ── Références ────────────────────────────────────────────────────────────
    wb   = openpyxl.load_workbook("remises_partenariat.xlsx")
    rows = []
    for row in wb.active.iter_rows(min_row=2, values_only=True):
        labo, cip13, libelle, puht, rsf, rsf_first, punet = row
        rows.append({
            "cip13": str(cip13) if cip13 else None,
            "labo":  labo,
            "libelle": libelle,
            "puht":    float(puht)      if puht      is not None else None,
            "rsf_pct": float(rsf)       if rsf       is not None else None,
            "rsf_first_pct": float(rsf_first) if rsf_first is not None else None,
            "punet":   float(punet)     if punet     is not None else None,
        })

    # Vide la table puis réinsère par lots
    rest("DELETE", "references_pharmacie?cip13=neq.VIDE")
    for i in range(0, len(rows), BATCH):
        rest("POST", "references_pharmacie", rows[i:i + BATCH])
        print(f"  {min(i + BATCH, len(rows))}/{len(rows)} références…", end="\r")
    print(f"✅  {len(rows)} références synchronisées        ")

    # ── Synonymes ─────────────────────────────────────────────────────────────
    synonymes = json.loads(Path("libelle_synonyms.json").read_text(encoding="utf-8"))
    syno_rows = [{"libelle_source": k, "libelle_cible": v} for k, v in synonymes.items()]
    rest("DELETE", "synonymes_libelles?libelle_source=neq.VIDE")
    for i in range(0, len(syno_rows), BATCH):
        rest("POST", "synonymes_libelles", syno_rows[i:i + BATCH])
    print(f"✅  {len(synonymes)} synonymes synchronisés")


if __name__ == "__main__":
    sync()
