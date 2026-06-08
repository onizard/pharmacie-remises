"""Test complet de run_ospharm() avec les credentials depuis Supabase."""
import json
import os
import sys
import io
import urllib.request
import asyncio

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
asyncio.set_event_loop(asyncio.new_event_loop())

SUPA_URL    = "https://api.break-pharma.fr"
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def get_first_ospharm_creds():
    url = f"{SUPA_URL}/rest/v1/user_state?select=user_id,state_json&limit=20"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    for row in rows:
        state = row.get("state_json", {})
        osp = state.get("connectors", {}).get("ospharm", {})
        if osp.get("user") and osp.get("pass"):
            return {"user": osp["user"], "pass": osp["pass"]}, row["user_id"]
    raise ValueError("No OSPHARM credentials in Supabase")


import os
os.environ["SUPABASE_SERVICE_KEY"] = SERVICE_KEY

creds, user_id = get_first_ospharm_creds()
print(f"user_id={user_id[:8]}... user={creds['user'][:4]}***")

from run_job_ospharm import run_ospharm

def progress(msg):
    print(f"  [progress] {msg}")

print("\nLancement run_ospharm()...")
try:
    rows, file_url, period_start, period_end = run_ospharm(creds, progress, user_id=user_id)
    print(f"\n✅ Succès!")
    print(f"   rows: {len(rows)}")
    print(f"   file_url: {file_url[:80] if file_url else '(vide)'}")
    print(f"   period: {period_start} → {period_end}")
    if rows:
        print(f"   colonnes: {list(rows[0].keys())}")
        print(f"   première ligne: {dict(list(rows[0].items())[:4])}")
except Exception as e:
    print(f"\n❌ Erreur: {e}")
    import traceback; traceback.print_exc()
