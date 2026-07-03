-- Migration : rendre rsf_defaults spécifique à l'année (2025 vs 2026)
-- ------------------------------------------------------------------------------
-- Problème : rsf_defaults(lab, rsf_pct, remise2, remise3) est année-agnostique.
-- Une seule RDP par palier, partagée 2025/2026 → impossible d'avoir des valeurs
-- 2025 différentes de 2026 (le save fusionne, 2026 gagne).
--
-- Solution : ajouter une colonne `year`, contrainte d'unicité (lab, rsf_pct, year).
-- Les lignes existantes deviennent 2026 ; on les duplique en 2025 (mêmes valeurs)
-- pour que 2025 démarre identique à aujourd'hui (aucun changement de comportement
-- tant que l'admin ne modifie pas 2025).
--
-- À exécuter sur le Postgres self-hosted (Hetzner), rôle propriétaire de la table.
-- Idempotent : peut être relancé sans dommage.
-- ------------------------------------------------------------------------------

BEGIN;

-- 1) Colonne year (défaut 2026 pour la compat ascendante)
ALTER TABLE rsf_defaults ADD COLUMN IF NOT EXISTS year int;

-- 2) Renseigner year sur les lignes existantes
--    Sentinels année-taggués par leur clé : __*_2025 / __*25 → 2025, sinon 2026.
UPDATE rsf_defaults SET year = 2025
  WHERE year IS NULL AND (rsf_pct LIKE '\_\_%2025' OR rsf_pct LIKE '\_\_%25');
UPDATE rsf_defaults SET year = 2026 WHERE year IS NULL;

-- 3) Dupliquer les paliers réels (rsf_pct numérique) 2026 → 2025, valeurs identiques,
--    pour que 2025 parte peuplé (aucune régression). Ne touche pas aux sentinels __*.
INSERT INTO rsf_defaults (lab, rsf_pct, remise2, remise3, year)
  SELECT lab, rsf_pct, remise2, remise3, 2025
  FROM rsf_defaults d
  WHERE d.year = 2026
    AND d.rsf_pct NOT LIKE '\_\_%'
    AND NOT EXISTS (
      SELECT 1 FROM rsf_defaults e
      WHERE e.lab = d.lab AND e.rsf_pct = d.rsf_pct AND e.year = 2025
    );

-- 4) year obligatoire, défaut 2026
ALTER TABLE rsf_defaults ALTER COLUMN year SET DEFAULT 2026;
ALTER TABLE rsf_defaults ALTER COLUMN year SET NOT NULL;

-- 5) Remplacer la contrainte d'unicité (lab, rsf_pct) → (lab, rsf_pct, year).
--    Le nom de la contrainte existante est découvert dynamiquement (2 colonnes,
--    unique ou clé primaire).
DO $$
DECLARE cname text;
BEGIN
  SELECT conname INTO cname
  FROM pg_constraint
  WHERE conrelid = 'rsf_defaults'::regclass
    AND contype IN ('u', 'p')
    AND array_length(conkey, 1) = 2
  LIMIT 1;
  IF cname IS NOT NULL THEN
    EXECUTE format('ALTER TABLE rsf_defaults DROP CONSTRAINT %I', cname);
  END IF;
END $$;

ALTER TABLE rsf_defaults
  ADD CONSTRAINT rsf_defaults_lab_pct_year_key UNIQUE (lab, rsf_pct, year);

COMMIT;

-- 6) Recharger le cache de schéma de PostgREST (sinon la colonne year n'apparaît pas)
NOTIFY pgrst, 'reload schema';
