"""
Mise à jour des connecteurs : OSPHARM DATASTAT (CSV ventes) + DIGIPHARMACIE (PDFs factures)
Usage : python maj_connecteurs.py
"""
import subprocess, sys, time
from pathlib import Path

PYTHON = str(Path(__file__).parent / "venv" / "Scripts" / "python.exe")
STEPS  = [
    ("OSPHARM DATASTAT — CSV ventes",      "scraper_ospharm.py"),
    ("DIGIPHARMACIE — PDFs factures",      "scraper_digipharmacie.py"),
]

for label, script in STEPS:
    print(f"\n{'='*55}")
    print(f"  {label}  ({script})")
    print(f"{'='*55}")
    t0  = time.time()
    ret = subprocess.run([PYTHON, "-X", "utf8", script])
    elapsed = time.time() - t0
    if ret.returncode != 0:
        print(f"\n❌  Échec de {script} (code {ret.returncode}) — arrêt.")
        sys.exit(ret.returncode)
    print(f"  ✓  {label} terminé en {elapsed:.1f}s")

print("\n✅  Mise à jour connecteurs complète.")
