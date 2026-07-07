-- Table de stockage des avoirs Digi (PDF) pour prévisualisation depuis le vérificateur.
-- Le PDF est stocké en base64 (colonne text) ; lecture/écriture via PostgREST avec le
-- JWT de l'utilisateur (RLS : chacun ne voit que ses propres fichiers). Pas de MinIO :
-- le stockage self-hosted est du MinIO S3 brut, non accessible via l'auth JWT.
-- Idempotent — relançable sans dommage.
-- À exécuter sur le Postgres self-hosted :
--   docker exec -i supa-db psql -U postgres -d postgres < ce_fichier.sql
-- ------------------------------------------------------------------------------

BEGIN;

CREATE TABLE IF NOT EXISTS public.digi_files (
  id          bigserial PRIMARY KEY,
  user_id     uuid NOT NULL DEFAULT auth.uid(),
  storage_key text NOT NULL,
  filename    text,
  months      text[] NOT NULL DEFAULT '{}',   -- mois couverts, ex. {'2026-03','2026-04'}
  content_b64 text NOT NULL,                    -- le PDF encodé base64
  uploaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS digi_files_user_key   ON public.digi_files (user_id, storage_key);
CREATE INDEX        IF NOT EXISTS digi_files_user_months ON public.digi_files USING gin (months);

ALTER TABLE public.digi_files ENABLE ROW LEVEL SECURITY;

-- Chaque utilisateur ne lit/écrit que ses propres fichiers (auth.uid() = user_id).
DROP POLICY IF EXISTS digi_files_own ON public.digi_files;
CREATE POLICY digi_files_own ON public.digi_files
  FOR ALL
  USING      (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

GRANT SELECT, INSERT, UPDATE, DELETE ON public.digi_files          TO authenticated;
GRANT USAGE, SELECT                  ON SEQUENCE public.digi_files_id_seq TO authenticated;

COMMIT;

-- Recharger le cache de schéma PostgREST
NOTIFY pgrst, 'reload schema';
