-- assist_access — accès « assistance » au compte admin par un 2e mot de passe.
--
-- Modèle : l'admin active l'assistance (enabled) et définit un mot de passe
-- d'assistance (stocké UNIQUEMENT haché, PBKDF2, jamais en clair). Tant que
-- enabled = true, l'API Render /assist/login ouvre une vraie session GoTrue sur
-- SON compte via ce mot de passe (magiclink admin → échange de jeton).
--
-- Sécurité : RLS activé SANS aucune policy → la table est inaccessible aux rôles
-- anon/authenticated. Seul le service_role (API Render, clé de service) y accède,
-- via les endpoints /assist/config (écriture, admin authentifié) et /assist/login
-- (lecture, contrôle du toggle + du hash côté serveur). Le mot de passe en clair
-- ne transite jamais par le dépôt.
--
-- À exécuter une fois dans la base (SQL editor / psql), comme assist_sync.

create table if not exists public.assist_access (
  user_id    uuid primary key references auth.users(id) on delete cascade,
  email      text,
  enabled    boolean not null default false,
  pw_hash    text,
  updated_at timestamptz not null default now()
);

create index if not exists assist_access_email_idx on public.assist_access (lower(email));

alter table public.assist_access enable row level security;
-- Volontairement AUCUNE policy : seul le service_role (qui bypass RLS) accède.

comment on table public.assist_access is
  'Accès assistance (2e mot de passe) au compte admin — géré exclusivement par l''API Render via la clé de service. pw_hash = PBKDF2, jamais de plaintext.';
