#!/usr/bin/env python3
"""
fix_digi_double.py — Correctif ponctuel : divise par 2 les champs d'AVOIRS Digi
(rdp_total, rdp_by_taux, presta_total[_ttc]) doublés par la fusion additive
(_merge_digi_stats). Ne touche PAS aux champs produits (qty, total_ht).

Diagnostic à l'origine : sur un compte, tous les mois Biogaran affichaient un
rdp_total exactement ×2 (ex. juillet 5492,46 au lieu de 2746,23). Cause : le même
avoir compté deux fois. Ce script restaure la valeur ×1.

⚠️  À LANCER SUR LE VPS (là où la vraie clé de service self-hosted est dispo).
    La clé cloud « sb_… » NE marche PAS (PostgREST self-hosted veut un JWT « eyJ… »).

Usage :
    # récupérer l'uid admin :
    #   docker exec supa-db psql -U postgres -d postgres -tc \\
    #     "SELECT id FROM auth.users WHERE email='contact@break-pharma.fr';"
    SUPABASE_SERVICE_KEY=eyJ...  USER_ID=<uuid>  python3 migrations/fix_digi_double.py          # DRY-RUN (n'écrit rien)
    SUPABASE_SERVICE_KEY=eyJ...  USER_ID=<uuid>  python3 migrations/fix_digi_double.py --apply  # écrit

Idempotent-ish : à ne lancer qu'UNE fois avec --apply (re-lancer re-diviserait).
Le dry-run est sûr et rejouable autant qu'on veut.
"""
import json
import os
import sys
import urllib.request

SUPA_URL    = os.environ.get("SUPA_URL", "https://api.break-pharma.fr").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
USER_ID     = os.environ.get("USER_ID", "") or (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "")
APPLY       = "--apply" in sys.argv

AVOIR_SCALAR = ("rdp_total", "presta_total", "presta_total_ttc")
TAUX_FIELDS  = ("grossiste", "direct", "ca_grossiste", "ca_direct")


def _fail(msg):
    print("✗ " + msg)
    sys.exit(1)


def _req(method, path, body=None):
    url = f"{SUPA_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def _half(v):
    return round((v or 0) / 2.0, 2)


def main():
    if not SERVICE_KEY.startswith("eyJ"):
        _fail("SUPABASE_SERVICE_KEY manquante ou au mauvais format (attendu un JWT self-hosted « eyJ… », pas « sb_… »).")
    if not USER_ID:
        _fail("USER_ID manquant (env USER_ID=<uuid> ou 1er argument).")

    rows = _req("GET", f"user_state?user_id=eq.{USER_ID}&select=state_json")
    if not rows:
        _fail(f"Aucun user_state pour {USER_ID}.")
    state = rows[0]["state_json"]
    dms = state.get("_digiMonthStats") or state.get("digi_month_stats")
    key = "_digiMonthStats" if "_digiMonthStats" in state else "digi_month_stats"
    if not dms:
        _fail("Pas de digi_month_stats dans l'état.")

    changed = 0
    print(f"{'MOIS':9} {'LABO':16} {'rdp_total avant':>16} {'→ après':>12}")
    print("-" * 58)
    for mk in sorted(dms):
        for r in dms[mk]:
            before = r.get("rdp_total", 0) or 0
            presta = r.get("presta_total", 0) or 0
            if not before and not presta and not r.get("rdp_by_taux"):
                continue
            for f in AVOIR_SCALAR:
                if r.get(f):
                    r[f] = _half(r[f])
            for e in (r.get("rdp_by_taux") or []):
                for f in TAUX_FIELDS:
                    if e.get(f):
                        e[f] = _half(e[f])
            changed += 1
            print(f"{mk:9} {str(r.get('labo',''))[:16]:16} {before:>16.2f} {r.get('rdp_total',0):>12.2f}")

    print("-" * 58)
    print(f"{changed} ligne(s) d'avoir concernée(s).")
    if not APPLY:
        print("\nDRY-RUN : rien n'a été écrit. Relance avec --apply pour appliquer.")
        return
    state[key] = dms
    _req("PATCH", f"user_state?user_id=eq.{USER_ID}", {"state_json": state})
    print("\n✓ APPLIQUÉ : état corrigé écrit en base.")


if __name__ == "__main__":
    main()
