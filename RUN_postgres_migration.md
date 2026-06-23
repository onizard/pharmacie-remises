# Tâche pour Claude VS Code — Migration `price_history` (PostgreSQL self-hosted)

## Contexte
La base est un **Supabase self-hosted sur le VPS Hetzner `178.104.40.21`** (PostgreSQL + PostgREST,
exposé via `https://api.break-pharma.fr`). PostgREST ne fait que du CRUD : la création de table et de
fonctions (DDL) doit se faire **directement en SQL** sur le Postgres.

Objectif : créer une table `price_history` (prix par date d'effet) pour gérer les labos qui publient
plusieurs tarifs par an (ex. Zydus Mai/Juin), puis la peupler depuis les PDF de tarifs.

## Étape 1 — Appliquer le DDL

Se connecter au Postgres du VPS. La stack tourne en Docker, le conteneur s'appelle en général
`supabase-db` :

```bash
ssh root@178.104.40.21 "docker exec -i \$(docker ps --format '{{.Names}}' | grep -i -E 'supabase-db|postgres' | head -1) psql -U postgres" <<'SQL'
CREATE TABLE IF NOT EXISTS price_history (
    id             bigserial PRIMARY KEY,
    cip13          text  NOT NULL,
    labo           text  NOT NULL,
    effective_date date  NOT NULL,
    puht           numeric,
    source         text,
    created_at     timestamptz DEFAULT now(),
    UNIQUE (cip13, effective_date)
);
CREATE INDEX IF NOT EXISTS idx_price_history_cip_date ON price_history (cip13, effective_date DESC);
ALTER TABLE price_history DISABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE, DELETE ON price_history TO anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON price_history TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE price_history_id_seq TO anon, authenticated;

CREATE OR REPLACE FUNCTION get_puht_at(p_cip text, p_date date)
RETURNS numeric LANGUAGE sql STABLE AS $$
  SELECT puht FROM price_history
  WHERE cip13 = p_cip AND effective_date <= p_date
  ORDER BY effective_date DESC LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION get_puht_batch(p_cips text[], p_date date)
RETURNS TABLE (cip13 text, puht numeric) LANGUAGE sql STABLE AS $$
  SELECT DISTINCT ON (ph.cip13) ph.cip13, ph.puht
  FROM price_history ph
  WHERE ph.cip13 = ANY(p_cips) AND ph.effective_date <= p_date
  ORDER BY ph.cip13, ph.effective_date DESC;
$$;

NOTIFY pgrst, 'reload schema';
SQL
```

> Si `docker exec` ne convient pas (Postgres natif, ou autre nom de conteneur), adapter en
> `psql "$DATABASE_URL" -f migrations/2026-06_price_history.sql` (le même SQL est versionné dans
> `migrations/2026-06_price_history.sql`).

Vérifier : `\dt price_history` doit lister la table, et `get_puht_batch` doit apparaître dans les
fonctions. Le `NOTIFY pgrst` recharge PostgREST pour qu'il expose tout de suite la table.

## Étape 2 — Peupler la table (tarifs Zydus Mai/Juin 2026)

Le script `load_prices_history.py` (déjà dans le repo) parse les PDF `Tarif Zydus France Mai/Juin
2026.pdf` et insère via PostgREST. Il a besoin de `SUPABASE_URL` et `SUPABASE_KEY` dans `.env`
(la clé service de préférence pour pouvoir écrire) :

```bash
pip install pdfplumber openpyxl   # si nécessaire
python3 load_prices_history.py
```

Sortie attendue : `~765 lignes insérées` (Mai 369 + Juin 396, effective_date = 1er du mois).

## Étape 3 — Vérification

```sql
SELECT labo, effective_date, count(*) FROM price_history GROUP BY 1,2 ORDER BY 1,2;
-- attendu : Zydus 2026-05-01 (~369), Zydus 2026-06-01 (~396)
```

```sql
-- prix d'un CIP à une date donnée
SELECT get_puht_at('3400930235966', '2026-06-15');   -- ~749.36
```

## Notes
- Le DDL n'a pas pu être fait depuis l'environnement Claude Code distant (réseau HTTPS-only,
  port 5432 injoignable) — d'où cette délégation à Claude VS Code (accès local au VPS).
- Une fois la table en place, l'environnement Claude Code peut, lui, la **peupler et la lire via
  PostgREST** (HTTPS) sans souci.
- Étape frontend séparée (hors Postgres) : brancher la simulation sur `get_puht_batch(cips, 'AAAA-MM-01')`
  pour les mois passés — à faire dans `index.html` (`launchSimulation`), pas bloquant.
