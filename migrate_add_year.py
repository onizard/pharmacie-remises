"""
Ajoute la colonne `year` (integer, nullable) à references_pharmacie.
ON CONFLICT reste sur cip13 — year est juste une métadonnée indiquant
depuis quelle CGV la ref a été importée.

Usage :
    python3 migrate_add_year.py
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

        # Ajoute la colonne si elle n'existe pas déjà
        cur.execute("""
            ALTER TABLE public.references_pharmacie
            ADD COLUMN IF NOT EXISTS year integer;
        """)
        conn.commit()
        print("✅  Colonne `year` ajoutée (ou déjà présente)")

        # Vérification
        cur.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'references_pharmacie'
            ORDER BY ordinal_position;
        """)
        print("\nSchéma actuel :")
        for row in cur.fetchall():
            print(f"  {row[0]:<20} {row[1]:<15} nullable={row[2]}")

        cur.close()
        conn.close()
    finally:
        tunnel.terminate()
        tunnel.wait()

    print("\nTerminé.")


if __name__ == "__main__":
    main()
