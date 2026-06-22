"""
Crée la table rsf_history (self-hosted PostgreSQL via tunnel SSH) et insère les taux RSF/RDP
extraits du BDC PDF pour un labo donné sur une année donnée.

Usage:
    python3 build_rsf_history.py
    python3 build_rsf_history.py --pdf "cgv labos/2026/cgv biogaran 2026.pdf" --labo Biogaran --year 2026

Colonnes insérées: cip13, labo, year, rsf_pct, rdp_pct, pfht
"""

import re
import subprocess
import sys
import time
import argparse
import pdfplumber
import psycopg2
from psycopg2.extras import execute_values

# Valeurs par défaut
DEFAULT_PDF  = "cgv labos/2025/BDC Biogaran - 20250520-122845.pdf"
DEFAULT_LABO = "Biogaran"
DEFAULT_YEAR = 2025

# Tunnel SSH vers le container PostgreSQL sur Hetzner
TUNNEL_LOCAL_PORT = 15432
HETZNER_HOST  = "178.104.40.21"
CONTAINER_IP  = "172.18.0.2"
CONTAINER_PORT = 5432
PG_USER = "postgres"
PG_PASS = "GB5Ie5yh5tsaIGHGWF10poOMM3GkRKy_"
PG_DB   = "postgres"

# CIP13  libellé…  PFHT€  REMISE%  RDP%  COLISAGE  [QUANTITE]
LINE_RE = re.compile(
    r"(\d{13})\s+"                        # CIP13
    r".+?"                                # libellé
    r"([\d]+[,.][\d]+)\s*€\s+"           # PFHT (toujours décimal)
    r"([\d]+(?:[,.][\d]+)?)\s*%\s+"      # REMISE (1)
    r"([\d]+(?:[,.][\d]+)?)\s*%"         # RDP (2)
)


def extract_rows(pdf_path):
    rows = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''
            for line in text.splitlines():
                m = LINE_RE.search(line)
                if m:
                    cip13 = m.group(1)
                    pfht  = float(m.group(2).replace(',', '.'))
                    rsf   = -abs(float(m.group(3).replace(',', '.')))
                    rdp   = -abs(float(m.group(4).replace(',', '.')))
                    rows[cip13] = (pfht, rsf, rdp)
    return rows


def start_tunnel():
    proc = subprocess.Popen([
        "ssh", "-N", "-L",
        f"{TUNNEL_LOCAL_PORT}:{CONTAINER_IP}:{CONTAINER_PORT}",
        f"root@{HETZNER_HOST}",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ExitOnForwardFailure=yes",
    ])
    time.sleep(2)
    return proc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf",  default=DEFAULT_PDF)
    parser.add_argument("--labo", default=DEFAULT_LABO)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    args = parser.parse_args()

    print(f"Extraction {args.pdf} (labo={args.labo}, year={args.year}) …")
    rows = extract_rows(args.pdf)
    print(f"  → {len(rows)} CIP13 extraits")

    if not rows:
        print("Aucune donnée trouvée — vérifier le PDF.")
        sys.exit(1)

    for cip, (pfht, rsf, rdp) in list(rows.items())[:5]:
        print(f"  {cip}  PFHT={pfht:.2f}€  RSF={rsf:.2f}%  RDP={rdp:.2f}%")

    print("\nOuverture tunnel SSH …")
    tunnel = start_tunnel()

    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=TUNNEL_LOCAL_PORT,
            user=PG_USER, password=PG_PASS, dbname=PG_DB,
            connect_timeout=10
        )
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.rsf_history (
                id            bigserial PRIMARY KEY,
                cip13         text    NOT NULL,
                labo          text    NOT NULL,
                year          integer NOT NULL,
                rsf_pct       float   NOT NULL,
                rsf_first_pct float   DEFAULT NULL,
                rdp_pct       float   DEFAULT NULL,
                pfht          float   DEFAULT NULL,
                UNIQUE (cip13, labo, year)
            );
        """)
        cur.execute("ALTER TABLE public.rsf_history ENABLE ROW LEVEL SECURITY;")
        for policy, stmt in [
            ("rh_read_all",
             "CREATE POLICY rh_read_all ON public.rsf_history FOR SELECT USING (true);"),
            ("rh_write_auth",
             """CREATE POLICY rh_write_auth ON public.rsf_history
                FOR ALL USING (true) WITH CHECK (true);"""),
        ]:
            cur.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_policies
                        WHERE tablename='rsf_history' AND policyname='{policy}')
                    THEN {stmt} END IF;
                END $$;
            """)
        conn.commit()

        # Exclure les non-génériques (pansements / LPP) : ils ne doivent pas faire partie
        # des groupes RSF ni de la liste des références normalisées. On s'appuie sur
        # references_pharmacie.is_generic (les pansements y sont déjà marqués False).
        cur.execute(
            "SELECT cip13 FROM public.references_pharmacie "
            "WHERE labo = %s AND is_generic IS FALSE", (args.labo,))
        non_generic = {r[0] for r in cur.fetchall()}
        if non_generic:
            before = len(rows)
            rows = {c: v for c, v in rows.items() if c not in non_generic}
            print(f"  → {before - len(rows)} non-génériques (pansements/LPP) exclus du RSF")

        # Purge des lignes non-génériques déjà présentes dans rsf_history (nettoyage rétroactif)
        if non_generic:
            cur.execute(
                "DELETE FROM public.rsf_history WHERE labo = %s AND year = %s "
                "AND cip13 = ANY(%s)", (args.labo, args.year, list(non_generic)))
            print(f"  → {cur.rowcount} lignes pansements purgées de rsf_history")
            conn.commit()

        data = [(cip, args.labo, args.year, rsf, rdp, pfht)
                for cip, (pfht, rsf, rdp) in rows.items()]
        execute_values(cur, """
            INSERT INTO public.rsf_history (cip13, labo, year, rsf_pct, rdp_pct, pfht)
            VALUES %s
            ON CONFLICT (cip13, labo, year)
            DO UPDATE SET
                rsf_pct = EXCLUDED.rsf_pct,
                rdp_pct = EXCLUDED.rdp_pct,
                pfht    = EXCLUDED.pfht
        """, data)
        conn.commit()
        print(f"\n  → {len(data)} lignes insérées/mises à jour (labo={args.labo}, year={args.year})")

        cur.execute("""
            SELECT rsf_pct, COUNT(*) FROM rsf_history
            WHERE labo=%s AND year=%s GROUP BY rsf_pct ORDER BY rsf_pct
        """, (args.labo, args.year))
        print("Distribution RSF% :")
        for rsf, cnt in cur.fetchall():
            print(f"  {rsf:6.2f}% → {cnt} refs")

        cur.close()
        conn.close()
    finally:
        tunnel.terminate()
        tunnel.wait()

    print("\nTerminé.")


if __name__ == "__main__":
    main()
