-- ============================================================================
-- Migration : historique des prix par date d'effet (multi-MAJ tarifaires/an)
-- Cas d'usage : Zydus (et autres) publient plusieurs tarifs par an (Mai, Juin…).
-- references_pharmacie.puht ne garde qu'UNE valeur (la plus récente) ; on ajoute
-- une table append-only pour retrouver le prix effectif à n'importe quelle date.
--
-- À appliquer sur le PostgreSQL self-hosted (Hetzner 178.104.40.21), via psql
-- ou l'éditeur SQL. PostgREST ne fait que du CRUD, d'où cette migration manuelle.
-- ============================================================================

-- 1. Table d'historique des prix --------------------------------------------
CREATE TABLE IF NOT EXISTS price_history (
    id             bigserial PRIMARY KEY,
    cip13          text  NOT NULL,
    labo           text  NOT NULL,
    effective_date date  NOT NULL,        -- date de prise d'effet du tarif
    puht           numeric,
    source         text,                  -- ex. 'Tarif Zydus France Juin 2026'
    created_at     timestamptz DEFAULT now(),
    UNIQUE (cip13, effective_date)
);

CREATE INDEX IF NOT EXISTS idx_price_history_cip_date
    ON price_history (cip13, effective_date DESC);

-- Pas de RLS (cohérent avec references_pharmacie/rsf_history, accès via clé anon)
ALTER TABLE price_history DISABLE ROW LEVEL SECURITY;

-- Accès PostgREST. L'app écrit avec la clé anon (comme references_pharmacie/rsf_history),
-- donc on accorde le CRUD à anon ET authenticated pour cohérence.
GRANT SELECT, INSERT, UPDATE, DELETE ON price_history TO anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON price_history TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE price_history_id_seq TO anon, authenticated;

-- 2. Prix effectif d'un CIP à une date donnée -------------------------------
CREATE OR REPLACE FUNCTION get_puht_at(p_cip text, p_date date)
RETURNS numeric LANGUAGE sql STABLE AS $$
    SELECT puht
    FROM   price_history
    WHERE  cip13 = p_cip
      AND  effective_date <= p_date
    ORDER  BY effective_date DESC
    LIMIT  1;
$$;

-- 3. Prix effectifs en lot pour une date (utilisé par la simulation) --------
--    Renvoie, pour chaque CIP, le dernier prix dont effective_date <= p_date.
CREATE OR REPLACE FUNCTION get_puht_batch(p_cips text[], p_date date)
RETURNS TABLE (cip13 text, puht numeric) LANGUAGE sql STABLE AS $$
    SELECT DISTINCT ON (ph.cip13) ph.cip13, ph.puht
    FROM   price_history ph
    WHERE  ph.cip13 = ANY(p_cips)
      AND  ph.effective_date <= p_date
    ORDER  BY ph.cip13, ph.effective_date DESC;
$$;

-- ============================================================================
-- Étapes post-migration (hors SQL) :
--   a) Lancer load_prices_history.py pour peupler price_history depuis les
--      "Tarif Zydus France Mai/Juin 2026.pdf" (effective_date = 1er du mois).
--   b) Frontend (index.html, launchSimulation) : si la simulation porte sur un
--      mois M passé, appeler rpc/get_puht_batch(cips, 'AAAA-MM-01') au lieu de
--      lire references_pharmacie.puht. Par défaut (mois courant) le comportement
--      actuel reste valable (references_pharmacie.puht = dernier tarif).
-- ============================================================================
