"""
run_job_digi_batch.py — Traitement EN FILE des avoirs Digi déposés par l'utilisateur.

Tourne sur GitHub Actions (runner toujours actif) → le traitement continue même si
l'utilisateur ferme son navigateur. Déclenché par le backend Render (dispatch), avec :

    USER_ID               (input du workflow) — l'utilisateur concerné
    SUPABASE_SERVICE_KEY  (GitHub Secret)     — clé service_role (bypass RLS)

Principe :
  1. lit les lignes digi_files en statut 'pending' pour cet utilisateur ;
  2. parse chaque PDF un par un (mêmes parsers que /parse/digi-pdf) ;
  3. fusionne digi_month_stats / escompte_stats / mdl_stats dans user_state ;
  4. passe chaque ligne en 'done' (avec les mois couverts) ou 'error' ;
  5. écrit la progression dans user_state.digi_batch_job à chaque fichier
     → la barre de progression du site la lit par polling.
"""
import os
import re
import sys
import json
import time
import base64
import tempfile
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from supabase_client import SUPA_URL, _supa_key, _get_state_sync, _patch_state_sync
from pdf_extractor import extract_invoice_lines
from run_job import (_compute_digi_month_stats, _merge_digi_stats,
                     _compute_escompte_stats, _merge_escompte_stats,
                     _compute_mdl_stats, _merge_mdl_stats)

USER_ID = os.environ.get("USER_ID", "").strip()


# ── Accès digi_files (clé service_role → bypass RLS) ──────────────────────────
def _digi_pending(user_id: str) -> list:
    key = _supa_key()
    url = (f"{SUPA_URL}/rest/v1/digi_files?user_id=eq.{user_id}&status=eq.pending"
           f"&select=id,filename,content_b64&order=id.asc")
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _digi_update(row_id, patch: dict):
    key = _supa_key()
    url = f"{SUPA_URL}/rest/v1/digi_files?id=eq.{row_id}"
    body = json.dumps(patch).encode()
    req = urllib.request.Request(url, data=body, method="PATCH", headers={
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    })
    with urllib.request.urlopen(req, timeout=30):
        pass


def _digi_done_no_kinds(user_id: str) -> list:
    """Avoirs déjà traités mais sans catégorie (kinds vide) → à ré-indexer
    (corriger months = période + kinds)."""
    key = _supa_key()
    url = (f"{SUPA_URL}/rest/v1/digi_files?user_id=eq.{user_id}&status=eq.done"
           f"&kinds=eq.%7B%7D&select=id,filename,content_b64&order=id.asc")
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _derive_months_kinds(lines: list):
    """Mois de rattachement (période pour rdp/presta) + catégories du PDF."""
    def _mois(l):
        if l.get("type") in ("rdp", "presta") and l.get("period_month"):
            return str(l["period_month"])[:7]
        return str(l.get("billing_date", ""))[:7]
    months = sorted({_mois(l) for l in lines if _mois(l)})
    kinds = []
    if any(l.get("type") == "rdp" for l in lines):    kinds.append("rdp")
    if any(l.get("type") == "presta" for l in lines): kinds.append("presta")
    if any(l.get("type") == "escompte" for l in lines): kinds.append("escompte")
    if any(l.get("type") == "mdl" for l in lines):      kinds.append("mdl")
    if any(l.get("type") not in ("rdp", "presta", "escompte", "mdl") for l in lines):
        kinds.append("product")
    return months, (kinds or ["product"])


def _persist(user_id: str, acc: dict, job: dict):
    """Relit l'état FRAIS (préserve ce que le frontend aurait écrit entre-temps),
    puis n'écrase que les clés du runner : stats Digi + statut du job."""
    fresh = _get_state_sync(user_id) or {}
    if acc.get("digi") is not None:     fresh["digi_month_stats"] = acc["digi"]
    if acc.get("esc")  is not None:     fresh["escompte_stats"]   = acc["esc"]
    if acc.get("mdl")  is not None:     fresh["mdl_stats"]        = acc["mdl"]
    job["updated_at"] = int(time.time() * 1000)
    fresh["digi_batch_job"] = job
    _patch_state_sync(user_id, fresh)


# ── Parse d'un PDF (même logique que /parse/digi-pdf) ─────────────────────────
def _parse_one(pdf_bytes: bytes, filename: str):
    provider = filename.rsplit(".", 1)[0][:80]
    billing_date = ""
    m = re.search(r'_(\d{2})(\d{2})(\d{4})(?:\.[Pp][Dd][Ff])?$', filename)
    if m:
        billing_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)
    try:
        lines = extract_invoice_lines(tmp_path, provider, billing_date)
    finally:
        tmp_path.unlink(missing_ok=True)
    return lines


def _reindex(user_id: str) -> int:
    """Ré-indexe les avoirs déjà stockés mais sans catégorie : re-parse chaque PDF
    et corrige months (= période de référence) + kinds. Ne touche PAS aux stats
    (déjà calculées au 1er dépôt). Passe unique : une fois kinds renseigné, exclu."""
    rows = _digi_done_no_kinds(user_id)
    if not rows:
        return 0
    print(f"→ ré-indexation de {len(rows)} avoir(s) existant(s)…", flush=True)
    n = 0
    for row in rows:
        try:
            pdf   = base64.b64decode(row.get("content_b64") or "")
            lines = _parse_one(pdf, row.get("filename") or "")
            if lines:
                months, kinds = _derive_months_kinds(lines)
                patch = {"kinds": kinds, "months": months}
            else:
                patch = {"kinds": ["product"]}   # illisible → marque pour ne pas reboucler
            _digi_update(row["id"], patch)
            n += 1
        except Exception as e:
            _digi_update(row["id"], {"kinds": ["product"]})
            print(f"  [warn] réindex {str(row.get('filename',''))[:40]} : {e}", flush=True)
    print(f"→ ré-indexation terminée : {n}", flush=True)
    return n


def main():
    if not USER_ID:
        print("!! USER_ID manquant — abandon", flush=True)
        sys.exit(1)

    state = _get_state_sync(USER_ID) or {}
    rows  = _digi_pending(USER_ID)
    total = len(rows)
    print(f"→ {total} avoir(s) en attente pour {USER_ID[:8]}", flush=True)

    # Accumulateurs des stats Digi (partent de l'existant, la base du runner).
    acc = {"digi": state.get("digi_month_stats") or {},
           "esc":  state.get("escompte_stats")   or {},
           "mdl":  state.get("mdl_stats")         or {}}

    ok = err = 0
    if total > 0:
      _persist(USER_ID, acc, {"status": "running", "done": 0, "total": total,
                              "message": f"Traitement de {total} fichier(s)…"})
      for i, row in enumerate(rows):
        fname = row.get("filename") or f"fichier {i+1}"
        try:
            pdf_bytes = base64.b64decode(row["content_b64"])
            lines = _parse_one(pdf_bytes, fname)
            if not lines:
                raise ValueError("aucune donnée extractible (format non reconnu)")

            esc  = [l for l in lines if l.get("type") == "escompte"]
            mdl  = [l for l in lines if l.get("type") == "mdl"]
            digi = [l for l in lines if l.get("type") not in ("escompte", "mdl")]

            if digi:
                acc["digi"] = _merge_digi_stats(acc["digi"], _compute_digi_month_stats(digi))
            if esc:
                acc["esc"]  = _merge_escompte_stats(acc["esc"], _compute_escompte_stats(esc))
            if mdl:
                acc["mdl"]  = _merge_mdl_stats(acc["mdl"], _compute_mdl_stats(mdl))

            months, kinds = _derive_months_kinds(lines)
            _digi_update(row["id"], {"status": "done", "months": months, "kinds": kinds})
            ok += 1
            msg = f"{i+1}/{total} · {fname} ✓"
        except Exception as e:
            _digi_update(row["id"], {"status": "error", "error": str(e)[:300]})
            err += 1
            msg = f"{i+1}/{total} · {fname} ✗ ({e})"
            print(f"  [warn] {msg}", flush=True)

        _persist(USER_ID, acc, {"status": "running", "done": i + 1, "total": total, "message": msg})
        print(f"  {msg}", flush=True)

    # Ré-indexation des avoirs existants mal datés / sans catégorie (une passe).
    reidx = _reindex(USER_ID)

    parts = []
    if total: parts.append(f"{ok} importé(s)")
    if err:   parts.append(f"{err} en erreur")
    if reidx: parts.append(f"{reidx} avoir(s) ré-indexé(s)")
    _persist(USER_ID, acc, {"status": "done", "done": total, "total": total,
                            "message": "Terminé : " + (", ".join(parts) if parts else "rien à faire")})
    print(f"✅ Terminé — {ok} ok, {err} err, {reidx} ré-indexé(s)", flush=True)


if __name__ == "__main__":
    main()
