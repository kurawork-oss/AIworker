-- Minimal stand-in for the pieces of Supabase the core schema depends on, so
-- the migration can be tested against a plain PostgreSQL. NEVER apply this to
-- a real Supabase project: it would shadow the real auth schema.
create schema if not exists auth;
create table if not exists auth.users (id uuid primary key);

-- Same resolution order as Supabase's own auth.uid().
create or replace function auth.uid() returns uuid language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid
$$;

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
end $$;

-- Behave like a NEW Supabase project: the roles can reach the schema, but
-- nothing in it is granted automatically -- not even EXECUTE to PUBLIC. The
-- migration must grant every privilege the app uses, and a missing one fails
-- the tests here instead of the app in production.
grant usage on schema public, auth to anon, authenticated, service_role;
alter default privileges in schema public revoke execute on functions from public;
