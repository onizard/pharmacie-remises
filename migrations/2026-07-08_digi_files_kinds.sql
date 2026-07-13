-- Catégorie du contenu de chaque avoir Digi, pour lier la remise reçue au BON PDF.
-- kinds : sous-ensemble de {'rdp','presta','escompte','mdl','product'}.
-- Un clic sur RDP reçu ouvre un PDF 'rdp' ; sur coop reçu, un PDF 'presta' —
-- plus jamais une facture produit à la place de l'avoir.
-- Idempotent.  docker exec -i supa-db psql -U postgres -d postgres < ce_fichier.sql
-- ------------------------------------------------------------------------------
BEGIN;
ALTER TABLE public.digi_files ADD COLUMN IF NOT EXISTS kinds text[] NOT NULL DEFAULT '{}';
COMMIT;
NOTIFY pgrst, 'reload schema';
