"""
Mise à jour complète des RSF : scraping → extraction → prix → sync Supabase.
Usage : python maj_rsf.py
"""
import subprocess, sys, time
from pathlib import Path

PYTHON = str(Path(__file__).parent / "venv" / "Scripts" / "python.exe")
STEPS  = [
    ("Téléchargement PDFs",  "scraper_astera.py"),
    ("Extraction Excel",     "extraire_excel.py"),
    ("Scraping PU HT",       "scraper_puht.py"),
    ("Sync Supabase",        "sync_supabase.py"),
]

for label, script in STEPS:
    print(f"\n{'='*50}")
    print(f"  {label}  ({script})")
    print(f"{'='*50}")
    t0  = time.time()
    ret = subprocess.run([PYTHON, "-X", "utf8", script])
    elapsed = time.time() - t0
    if ret.returncode != 0:
        print(f"\n❌  Échec de {script} (code {ret.returncode}) — arrêt.")
        sys.exit(ret.returncode)
    print(f"  ✓  {label} terminé en {elapsed:.1f}s")

print("\n✅  Mise à jour RSF complète.")
