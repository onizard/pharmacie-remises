-- Sécurité : verrouiller l'ÉCRITURE de rsf_defaults au seul compte admin
-- ------------------------------------------------------------------------------
-- Problème : rsf_defaults a GRANT INSERT/UPDATE/DELETE à anon + authenticated
-- → n'importe quel compte connecté (voire anon) peut écraser les CGV de TOUS les
--   pharmaciens en tapant directement l'API PostgREST (la restriction actuelle
--   n'existe QUE côté navigateur — contournable).
--
-- Correctif :
--   • Lecture (SELECT) : conservée pour anon + authenticated (rendu des conditions).
--   • Écriture (INSERT/UPDATE/DELETE) : réservée à l'admin, identifié par l'email
--     du JWT GoTrue = 'contact@break-pharma.fr'. Mis en oeuvre via RLS.
--   • service_role (clé de service backend) bypass RLS → écritures serveur intactes.
--
-- Idempotent : relançable sans dommage.
-- À exécuter sur le Postgres self-hosted (Hetzner), rôle propriétaire de la table.
-- ------------------------------------------------------------------------------

BEGIN;

-- 1) Retirer l'écriture directe à anon (garde la lecture). authenticated garde les
--    droits d'écriture MAIS la RLS ci-dessous les restreint à l'admin.
REVOKE INSERT, UPDATE, DELETE ON rsf_defaults FROM anon;
GRANT  SELECT                 ON rsf_defaults TO anon, authenticated;
GRANT  INSERT, UPDATE, DELETE ON rsf_defaults TO authenticated;

-- 2) Activer la Row Level Security
ALTER TABLE rsf_defaults ENABLE ROW LEVEL SECURITY;
-- (FORCE pour que même le propriétaire de la table soit soumis aux policies —
--  optionnel ; on le laisse commenté pour ne pas se verrouiller en psql direct)
-- ALTER TABLE rsf_defaults FORCE ROW LEVEL SECURITY;

-- 3) Lecture ouverte à tous
DROP POLICY IF EXISTS rsf_defaults_select_all ON rsf_defaults;
CREATE POLICY rsf_defaults_select_all ON rsf_defaults
  FOR SELECT
  USING (true);

-- 4) Écriture réservée à l'admin (email du JWT GoTrue).
--    current_setting('request.jwt.claims', true) = payload JSON du JWT exposé par
--    PostgREST ; ->> 'email' = l'email de l'utilisateur connecté.
DROP POLICY IF EXISTS rsf_defaults_admin_write ON rsf_defaults;
CREATE POLICY rsf_defaults_admin_write ON rsf_defaults
  FOR ALL
  USING      ( (current_setting('request.jwt.claims', true)::json ->> 'email') = 'contact@break-pharma.fr' )
  WITH CHECK ( (current_setting('request.jwt.claims', true)::json ->> 'email') = 'contact@break-pharma.fr' );

COMMIT;

-- 5) Recharger le cache de schéma PostgREST
NOTIFY pgrst, 'reload schema';

-- ------------------------------------------------------------------------------
-- VÉRIFICATIONS (à lancer séparément après coup, facultatif)
-- ------------------------------------------------------------------------------
-- a) Les policies sont bien en place :
--    SELECT polname, cmd FROM pg_policies WHERE tablename = 'rsf_defaults';
--
-- b) Test côté API (depuis un shell) — un compte NON-admin doit être REFUSÉ en écriture
--    et AUTORISÉ en lecture. Un write anon/non-admin doit renvoyer 401/403 (42501 ou RLS),
--    plus le 200/201 d'avant.
