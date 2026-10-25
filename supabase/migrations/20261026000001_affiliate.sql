-- =============================================================================
-- A8 (and other ASP) programs the owner has been approved for.
--
-- The owner pastes what the ASP gives them; the app keeps only the tracking
-- link and the impression pixel, never the raw HTML. `facts` is the ONLY
-- source of product claims an article may make (景品表示法): the model is told
-- to say nothing about price, features or results that is not written here.
-- =============================================================================

create table if not exists public.affiliate_programs (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid not null,
  asp         text not null default 'a8' check (asp in ('a8', 'other')),
  name        text not null check (length(name) between 1 and 120),
  link_url    text not null check (link_url ~ '^https://[^\s"<>]+$'),
  pixel_url   text not null default '' check (pixel_url = '' or pixel_url ~ '^https://[^\s"<>]+$'),
  facts       text not null default '' check (length(facts) <= 4000),
  -- Where the program's terms allow the link: blog / note / sns / youtube.
  media       text[] not null default array['blog'],
  status      text not null default 'active' check (status in ('active', 'paused')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists affiliate_programs_owner_idx on public.affiliate_programs(owner_id, status);

drop trigger if exists affiliate_programs_touch on public.affiliate_programs;
create trigger affiliate_programs_touch before update on public.affiliate_programs
  for each row execute function public.touch_updated_at();

alter table public.affiliate_programs enable row level security;

drop policy if exists affiliate_programs_owner on public.affiliate_programs;
create policy affiliate_programs_owner on public.affiliate_programs for all to authenticated
  using (public.is_owner() and owner_id = auth.uid())
  with check (public.is_owner() and owner_id = auth.uid());

revoke all on public.affiliate_programs from anon, authenticated, service_role;
grant select, insert, update, delete on public.affiliate_programs to authenticated;
grant select on public.affiliate_programs to service_role;
