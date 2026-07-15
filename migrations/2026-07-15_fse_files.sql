-- Archivage des exports XLSX FSE Banque (relevés de virements) — même pattern que
-- digi_files : contenu en base64 dans Postgres, lecture via PostgREST + RLS.
-- Remplace l'upload MinIO du runner FSE qui échouait systématiquement (HTTP 400) :
-- le stockage self-hosted est du MinIO S3 brut, non accessible via l'auth JWT et
-- sans clés S3 côté GitHub Actions. Ces relevés sont des PIÈCES du litige labo
-- (preuve des virements) → archivés et visibles dans l'explorateur de fichiers.
-- Idempotent — relançable sans dommage.
-- À exécuter sur le Postgres self-hosted :
--   docker exec -i supa-db psql -U postgres -d postgres < ce_fichier.sql
-- ------------------------------------------------------------------------------

BEGIN;

CREATE TABLE IF NOT EXISTS public.fse_files (
  id          bigserial PRIMARY KEY,
  user_id     uuid NOT NULL DEFAULT auth.uid(),
  storage_key text NOT NULL,                    -- ex. 'fse_202510.xlsx'
  filename    text,
  yyyymm      text NOT NULL DEFAULT '',         -- mois couvert, ex. '202510'
  content_b64 text NOT NULL,                    -- le XLSX encodé base64
  uploaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS fse_files_user_key ON public.fse_files (user_id, storage_key);

ALTER TABLE public.fse_files ENABLE ROW LEVEL SECURITY;

-- Chaque utilisateur ne lit/écrit que ses propres fichiers (auth.uid() = user_id).
DROP POLICY IF EXISTS fse_files_own ON public.fse_files;
CREATE POLICY fse_files_own ON public.fse_files
  FOR ALL
  USING      (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

GRANT SELECT, INSERT, UPDATE, DELETE ON public.fse_files                 TO authenticated;
GRANT USAGE, SELECT                  ON SEQUENCE public.fse_files_id_seq TO authenticated;

COMMIT;

-- Recharger le cache de schéma PostgREST
NOTIFY pgrst, 'reload schema';
