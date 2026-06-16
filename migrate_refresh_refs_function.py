"""
Crée la fonction PostgreSQL refresh_references_from_rsf_history(p_labo text).

Cette fonction reconstruit references_pharmacie depuis rsf_history en prenant
le millésime le plus récent par CIP13. Elle remplace les upserts directs dans
main.py, ce qui garantit que rsf_history reste la source de vérité :
- import 2025 après 2026 → pas de dégradation (WHERE year >= existing)
- puht du scraper préservé (COALESCE)
- rsf_first_pct inclus depuis les PDFs

Usage :
    python3 migrate_refresh_refs_function.py
"""

import subprocess, time, sys
import psycopg2

TUNNEL_LOCAL_PORT = 15432
HETZNER_HOST      = "178.104.40.21"
CONTAINER_IP      = "172.18.0.2"
CONTAINER_PORT    = 5432
PG_USER  = "postgres"
PG_PASS  = "GB5Ie5yh5tsaIGHGWF10poOMM3GkRKy_"
PG_DB    = "postgres"

SQL = """
CREATE OR REPLACE FUNCTION public.refresh_references_from_rsf_history(p_labo text)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  n integer;
BEGIN
  INSERT INTO public.references_pharmacie (cip13, labo, libelle, puht, rsf_pct, rsf_first_pct, year)
  SELECT DISTINCT ON (cip13)
    cip13,
    labo,
    libelle,
    pfht    AS puht,
    rsf_pct,
    rsf_first_pct,
    year
  FROM public.rsf_history
  WHERE labo = p_labo
    AND rsf_pct IS NOT NULL
  ORDER BY cip13, year DESC
  ON CONFLICT (cip13) DO UPDATE SET
    labo          = EXCLUDED.labo,
    libelle       = EXCLUDED.libelle,
    rsf_pct       = EXCLUDED.rsf_pct,
    rsf_first_pct = EXCLUDED.rsf_first_pct,
    year          = EXCLUDED.year,
    -- Preserves puht mis à jour par scraper_puht.py ; pfht sert d'initialisation seulement
    puht          = COALESCE(references_pharmacie.puht, EXCLUDED.puht)
  WHERE COALESCE(references_pharmacie.year, 0) <= EXCLUDED.year;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

-- PostgREST doit pouvoir appeler la fonction avec le rôle anon/authenticated
GRANT EXECUTE ON FUNCTION public.refresh_references_from_rsf_history(text) TO anon;
GRANT EXECUTE ON FUNCTION public.refresh_references_from_rsf_history(text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.refresh_references_from_rsf_history(text) TO service_role;
"""


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
    print("Ouverture tunnel SSH …")
    tunnel = start_tunnel()
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=TUNNEL_LOCAL_PORT,
            user=PG_USER, password=PG_PASS, dbname=PG_DB,
            connect_timeout=10,
        )
        cur = conn.cursor()
        cur.execute(SQL)
        conn.commit()
        print("✅  Fonction refresh_references_from_rsf_history créée")

        # Vérification rapide
        cur.execute("SELECT proname, prosecdef FROM pg_proc WHERE proname = 'refresh_references_from_rsf_history'")
        row = cur.fetchone()
        if row:
            print(f"   proname={row[0]}  security_definer={row[1]}")

        cur.close()
        conn.close()
    finally:
        tunnel.terminate()
        tunnel.wait()

    print("\nTerminé.")


if __name__ == "__main__":
    main()
