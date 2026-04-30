"""
Synchronise remises_partenariat.xlsx et libelle_synonyms.json vers Supabase.
À lancer après chaque mise à jour de l'Excel ou des synonymes.

Usage :
    python sync_supabase.py
"""

import os
import json
import psycopg2
import psycopg2.extras
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

DB = dict(
    host="db.fmterazwesiwpwjpkyqi.supabase.co",
    port=5432,
    dbname="postgres",
    user="postgres",
    password=os.environ.get("SUPABASE_DB_PASSWORD", ""),
    sslmode="require",
)


def sync():
    conn = psycopg2.connect(**DB)
    conn.autocommit = True
    cur = conn.cursor()

    # Références
    wb = openpyxl.load_workbook("remises_partenariat.xlsx")
    rows = []
    for row in wb.active.iter_rows(min_row=2, values_only=True):
        labo, cip13, libelle, puht, rsf, punet = row
        rows.append((cip13, labo, libelle, puht, rsf, punet))

    cur.execute("TRUNCATE references_pharmacie;")
    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO references_pharmacie (cip13, labo, libelle, puht, rsf_pct, punet)
           VALUES %s""",
        rows, page_size=500
    )
    print(f"✅  {len(rows)} références synchronisées")

    # Synonymes
    synonymes = json.loads(Path("libelle_synonyms.json").read_text(encoding="utf-8"))
    cur.execute("TRUNCATE synonymes_libelles;")
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO synonymes_libelles (libelle_source, libelle_cible) VALUES %s",
        list(synonymes.items())
    )
    print(f"✅  {len(synonymes)} synonymes synchronisés")

    conn.close()


if __name__ == "__main__":
    sync()
