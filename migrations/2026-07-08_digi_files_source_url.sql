-- Extension navigateur « break-pharma connect » : import des avoirs/factures Digi
-- depuis la SESSION de l'utilisateur (contourne l'anti-bot Cloudflare de Digipharmacie).
--
-- L'extension récupère la liste des factures via l'API Digi (/api/v1/invoices/) et
-- l'envoie au backend, qui insère une ligne digi_files 'pending' par facture, en
-- portant l'URL du PDF (source_url). Le runner GitHub Actions télécharge ensuite
-- chaque PDF depuis cette URL (mêmes URLs publiques/S3 que le scraper historique),
-- remplit content_b64, puis parse — l'aperçu au clic marche donc comme pour un dépôt.
--
-- content_b64 devient nullable : au moment de l'insertion 'pending' par l'extension,
-- le PDF n'est pas encore téléchargé (le runner le fera).
-- Idempotent. À exécuter :
--   docker exec -i supa-db psql -U postgres -d postgres < ce_fichier.sql
-- ------------------------------------------------------------------------------

BEGIN;

ALTER TABLE public.digi_files ADD COLUMN IF NOT EXISTS source_url text;

-- Le PDF n'est pas encore là quand l'extension insère la ligne 'pending'.
ALTER TABLE public.digi_files ALTER COLUMN content_b64 DROP NOT NULL;

COMMIT;

NOTIFY pgrst, 'reload schema';
