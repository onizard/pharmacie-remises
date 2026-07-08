-- Traitement des avoirs Digi EN FILE D'ATTENTE côté serveur (GitHub Actions).
-- On étend digi_files : les PDF déposés en lot sont insérés en 'pending' par le
-- frontend, puis un runner GitHub Actions les parse un par un et passe en 'done'
-- (ou 'error'). Le PDF étant déjà stocké, l'aperçu au clic marche sans étape en plus.
--
-- Le runner s'authentifie avec la clé service_role (bypass RLS) → il faut lui donner
-- les droits sur digi_files et user_state.
-- Idempotent. À exécuter :
--   docker exec -i supa-db psql -U postgres -d postgres < ce_fichier.sql
-- ------------------------------------------------------------------------------

BEGIN;

ALTER TABLE public.digi_files ADD COLUMN IF NOT EXISTS status   text NOT NULL DEFAULT 'done'; -- pending | done | error
ALTER TABLE public.digi_files ADD COLUMN IF NOT EXISTS batch_id text;
ALTER TABLE public.digi_files ADD COLUMN IF NOT EXISTS error    text;

-- Les lignes en attente n'ont pas encore de mois calculés.
ALTER TABLE public.digi_files ALTER COLUMN months DROP NOT NULL;

CREATE INDEX IF NOT EXISTS digi_files_user_status ON public.digi_files (user_id, status);

-- Droits pour le runner (service_role) : traite les fichiers de n'importe quel user.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON public.digi_files          TO service_role;
    GRANT USAGE, SELECT                  ON SEQUENCE public.digi_files_id_seq TO service_role;
  END IF;
END $$;

COMMIT;

NOTIFY pgrst, 'reload schema';
