"""
Crée la table rsf_history dans Supabase et insère les taux RSF 2025
extraits du BDC Biogaran 20250520.
"""

import re
import pdfplumber
import psycopg2
from psycopg2.extras import execute_values

PDF_PATH = "cgv labos/BDC Biogaran - 20250520-122845.pdf"
LABO = "Biogaran"
YEAR = 2025

DB_HOST = "aws-0-eu-west-1.pooler.supabase.com"
DB_PORT = 5432
DB_USER = "postgres.fmterazwesiwpwjpkyqi"
DB_PASS = "lDXWqP1SsuchEIRH"
DB_NAME = "postgres"

# CIP13  ...texte...  PFHT €  REMISE%  RDP%  COLISAGE  (QUANTITE optionnel)
LINE_RE = re.compile(
    r"(\d{13})\s+.+?"           # CIP13 + libellé
    r"[\d\s]+[,.][\d]+\s*€\s+"  # PFHT (prix)
    r"([\d]+[,.]\d+)\s*%\s+"    # REMISE (1) → groupe 2
    r"[\d]+\s*%"                 # RDP (2)
)

def extract_rows(pdf_path):
    rows = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            for line in text.splitlines():
                m = LINE_RE.search(line)
                if m:
                    cip13 = m.group(1)
                    rsf_pct_str = m.group(2).replace(",", ".")
                    rsf_pct = float(rsf_pct_str)
                    rows[cip13] = rsf_pct   # dernier vu gagne (pages dupliquées)
    return rows

def main():
    print("Extraction du PDF…")
    rows = extract_rows(PDF_PATH)
    print(f"  → {len(rows)} CIP13 extraits")

    if not rows:
        print("Aucune donnée trouvée — vérifier le regex.")
        return

    # Afficher quelques exemples
    sample = list(rows.items())[:5]
    for cip, rsf in sample:
        print(f"  {cip}  RSF={rsf}%")

    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS, dbname=DB_NAME
    )
    cur = conn.cursor()

    # Créer la table si elle n'existe pas
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.rsf_history (
            id         bigserial PRIMARY KEY,
            cip13      text      NOT NULL,
            labo       text      NOT NULL,
            year       integer   NOT NULL,
            rsf_pct    float     NOT NULL,
            rsf_first_pct float  DEFAULT NULL,
            UNIQUE (cip13, labo, year)
        );
    """)

    # RLS : read pour tous, write pour authenticated/service_role
    cur.execute("""
        ALTER TABLE public.rsf_history ENABLE ROW LEVEL SECURITY;
    """)
    cur.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE tablename='rsf_history' AND policyname='rh_read_all'
            ) THEN
                CREATE POLICY rh_read_all ON public.rsf_history
                    FOR SELECT USING (true);
            END IF;
        END $$;
    """)
    cur.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE tablename='rsf_history' AND policyname='rh_write_auth'
            ) THEN
                CREATE POLICY rh_write_auth ON public.rsf_history
                    FOR ALL
                    USING      (auth.role() IN ('authenticated','service_role'))
                    WITH CHECK (auth.role() IN ('authenticated','service_role'));
            END IF;
        END $$;
    """)

    # Insérer / mettre à jour
    data = [(cip, LABO, YEAR, rsf) for cip, rsf in rows.items()]
    execute_values(cur, """
        INSERT INTO public.rsf_history (cip13, labo, year, rsf_pct)
        VALUES %s
        ON CONFLICT (cip13, labo, year)
        DO UPDATE SET rsf_pct = EXCLUDED.rsf_pct
    """, data)

    conn.commit()
    print(f"  → {len(data)} lignes insérées/mises à jour dans rsf_history (labo={LABO}, year={YEAR})")

    # Vérification rapide
    cur.execute("SELECT rsf_pct, COUNT(*) FROM rsf_history WHERE labo=%s AND year=%s GROUP BY rsf_pct ORDER BY rsf_pct", (LABO, YEAR))
    print("\nDistribution RSF% :")
    for rsf, cnt in cur.fetchall():
        print(f"  {rsf:5.2f}% → {cnt} refs")

    cur.close()
    conn.close()
    print("\nTerminé.")

if __name__ == "__main__":
    main()
