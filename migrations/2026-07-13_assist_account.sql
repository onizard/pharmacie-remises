-- Recrée le compte d'assistance : la cible de assist_sync passe du compte support
-- historique (claude@test.fr / 90d23b50…, dont le mot de passe était perdu) au
-- nouveau compte support assist@break-pharma.fr (uid abd09f20…, créé par signup,
-- mot de passe connu et conservé hors dépôt).
--
-- Seul l'admin (contact@break-pharma.fr) peut appeler assist_sync. Lorsqu'il
-- active l'assistance, ses données (state_json + connecteurs + avoirs digi_files)
-- sont copiées vers ce compte support, que l'assistance utilise pour déboguer sur
-- des données réelles. Le flag _assistAdmin fait passer le support en vue admin.
--
-- À exécuter sur le VPS :
--   docker exec -i supa-db psql -U postgres -d postgres < ce_fichier.sql
-- (ou via curl depuis le raw GitHub, comme les migrations précédentes)
-- Idempotent.
-- ------------------------------------------------------------------------------
BEGIN;

CREATE OR REPLACE FUNCTION public.assist_sync(enable boolean)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  admin_id uuid;
  test_id  uuid := 'abd09f20-af24-49c5-9ae0-22e0f251b94c';  -- assist@break-pharma.fr
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
