-- « Activer l'assistance » : copie les données de l'admin vers le compte support
-- (claude@test.fr) pour permettre des tests en temps réel sur des données réelles.
-- Réservé à l'admin (contact@break-pharma.fr) — vérifié via le JWT.
-- Inclut aussi la colonne digi_files.kinds (au cas où la migration n'a pas été lancée).
-- Idempotent.  docker exec -i supa-db psql -U postgres -d postgres < ce_fichier.sql
-- ------------------------------------------------------------------------------
BEGIN;

-- Colonne kinds (pour l'aperçu des avoirs par catégorie), au cas où.
ALTER TABLE public.digi_files ADD COLUMN IF NOT EXISTS kinds text[] NOT NULL DEFAULT '{}';

CREATE OR REPLACE FUNCTION public.assist_sync(enable boolean)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  admin_id uuid;
  test_id  uuid := '90d23b50-b47d-4cdc-85ab-cc198d4bd0c0';  -- claude@test.fr
  n int := 0;
BEGIN
  -- Seul l'admin peut déclencher la synchro.
  IF (current_setting('request.jwt.claims', true)::json ->> 'email') IS DISTINCT FROM 'contact@break-pharma.fr' THEN
    RAISE EXCEPTION 'assist_sync : réservé à l''administrateur';
  END IF;
  admin_id := (current_setting('request.jwt.claims', true)::json ->> 'sub')::uuid;

  IF enable THEN
    -- Copier l'état COMPLET (state_json = conditions labo, données…) + les connecteurs
    -- (ospharm/digi), et poser le flag _assistAdmin → le compte support s'affiche
    -- exactement comme le compte admin.
    INSERT INTO user_state (user_id, state_json, connectors)
      SELECT test_id,
             jsonb_set(COALESCE(state_json, '{}'::jsonb), '{_assistAdmin}', 'true'::jsonb),
             COALESCE(connectors, '{}'::jsonb)
      FROM user_state WHERE user_id = admin_id
      ON CONFLICT (user_id) DO UPDATE
        SET state_json = EXCLUDED.state_json, connectors = EXCLUDED.connectors;

    -- Copier les avoirs (digi_files) : on repart propre côté support.
    DELETE FROM digi_files WHERE user_id = test_id;
    INSERT INTO digi_files (user_id, storage_key, filename, months, content_b64, status, kinds)
      SELECT test_id, storage_key, filename, months, content_b64,
             COALESCE(status, 'done'), COALESCE(kinds, '{}')
      FROM digi_files WHERE user_id = admin_id;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN 'assistance ON — état + connecteurs + ' || n || ' avoir(s) copiés';
  ELSE
    -- Purge du compte support (état, connecteurs, avoirs, flag admin).
    UPDATE user_state SET state_json = '{}'::jsonb, connectors = '{}'::jsonb WHERE user_id = test_id;
    DELETE FROM digi_files WHERE user_id = test_id;
    RETURN 'assistance OFF — compte support purgé';
  END IF;
END $$;

GRANT EXECUTE ON FUNCTION public.assist_sync(boolean) TO authenticated;

COMMIT;
NOTIFY pgrst, 'reload schema';
