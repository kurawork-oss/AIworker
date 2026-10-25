-- =============================================================================
-- AIworker v2 core schema (Supabase / PostgreSQL)
--
-- The rules that make the system safe live HERE, in the database, not only in
-- the app. The phone UI, the GitHub Actions pipeline and a future Mac worker
-- all write to these tables; enforcing invariants in any one of those clients
-- would leave the other two free to break them.
--
-- Enforced here:
--   * RLS: every row belongs to its owner; strangers who sign up see nothing.
--   * `approved` can only be written by approve_item() -- a trigger rejects any
--     other path, including the service role.
--   * Status changes are checked against an explicit transition table.
--   * Changing an approved item's content invalidates the approval.
--   * Idempotency keys stop double execution of jobs.
--   * The kill switch is a row the pipeline must read before acting.
-- =============================================================================

create extension if not exists pgcrypto;

-- -----------------------------------------------------------------------------
-- helpers
-- -----------------------------------------------------------------------------
create or replace function public.request_role() returns text
language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'),
    ''
  )
$$;

-- The single person allowed to use this deployment. Seeded once at setup.
-- Signing up to the Supabase project does NOT grant access: every policy
-- checks this table, so even if public sign-up is left on by mistake, a
-- stranger's account can read and write nothing.
create table if not exists public.owners (
  user_id    uuid primary key,
  created_at timestamptz not null default now()
);
alter table public.owners enable row level security;

create or replace function public.is_owner() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.owners where user_id = auth.uid())
$$;

create or replace function public.touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

-- -----------------------------------------------------------------------------
-- businesses (事業) and accounts (媒体アカウント)
-- -----------------------------------------------------------------------------
create table if not exists public.businesses (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid not null,
  code        text not null,
  name        text not null,
  -- 設計中 / 接続準備 / 検証中 / 運用中 / 停止中
  stage       text not null default 'designing'
              check (stage in ('designing','preparing','verifying','operating','stopped')),
  settings    jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (owner_id, code)
);

create table if not exists public.accounts (
  id               uuid primary key default gen_random_uuid(),
  owner_id         uuid not null,
  business_id      uuid not null references public.businesses(id) on delete cascade,
  platform         text not null,
  handle           text not null default '',
  -- 原則1日1本 (要件 §1). Ceiling of 50 is a guard against typos, not a target.
  daily_limit      int  not null default 1 check (daily_limit between 0 and 50),
  approval_policy  text not null default 'required' check (approval_policy in ('required','auto')),
  publish_method   text not null default 'manual'
                   check (publish_method in ('manual','api','native_schedule')),
  -- 規約確認 (要件 §8): unverified operations are held, not attempted.
  terms_status     text not null default 'unverified'
                   check (terms_status in ('verified','conditional','unverified','forbidden')),
  terms_checked_on date,
  terms_source_url text not null default '',
  status           text not null default 'active' check (status in ('active','paused','stopped')),
  settings         jsonb not null default '{}'::jsonb,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index if not exists accounts_business_idx on public.accounts(business_id);

-- 設定の版管理 (F01): every change to a business or account is kept, so
-- before/after performance can be compared and a bad change undone.
create table if not exists public.settings_versions (
  id          bigint generated always as identity primary key,
  owner_id    uuid not null,
  entity      text not null check (entity in ('business','account')),
  entity_id   uuid not null,
  before      jsonb,
  after       jsonb not null,
  changed_by  text not null default '',
  created_at  timestamptz not null default now()
);
create index if not exists settings_versions_entity_idx
  on public.settings_versions(entity, entity_id, created_at desc);

create or replace function public.version_settings() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  kind text := case tg_table_name when 'businesses' then 'business' else 'account' end;
begin
  if tg_op = 'UPDATE' and to_jsonb(old) - 'updated_at' = to_jsonb(new) - 'updated_at' then
    return new;  -- nothing really changed
  end if;
  insert into public.settings_versions(owner_id, entity, entity_id, before, after, changed_by)
  values (new.owner_id, kind, new.id,
          case when tg_op = 'UPDATE' then to_jsonb(old) end,
          to_jsonb(new),
          coalesce(auth.uid()::text, public.request_role()));
  return new;
end $$;

drop trigger if exists businesses_touch on public.businesses;
create trigger businesses_touch before update on public.businesses
  for each row execute function public.touch_updated_at();
drop trigger if exists businesses_version on public.businesses;
create trigger businesses_version after insert or update on public.businesses
  for each row execute function public.version_settings();

drop trigger if exists accounts_touch on public.accounts;
create trigger accounts_touch before update on public.accounts
  for each row execute function public.touch_updated_at();
drop trigger if exists accounts_version on public.accounts;
create trigger accounts_version after insert or update on public.accounts
  for each row execute function public.version_settings();

-- -----------------------------------------------------------------------------
-- items (成果物)
-- -----------------------------------------------------------------------------
create table if not exists public.items (
  id                  uuid primary key default gen_random_uuid(),
  owner_id            uuid not null,
  business_id         uuid not null references public.businesses(id) on delete cascade,
  account_id          uuid references public.accounts(id) on delete set null,
  kind                text not null,
  title               text not null default '',
  body                text not null default '',
  meta                jsonb not null default '{}'::jsonb,
  -- Bumped on every content change. An approval is for one version only.
  version             int  not null default 1,
  status              text not null default 'draft' check (status in (
                        'draft','image_queued','checking','pending_review','blocked',
                        'needs_revision','on_hold','approved','rejected',
                        'waiting_pc','awaiting_manual','scheduled',
                        'published','failed','cancelled')),
  blocking            boolean not null default false,
  policy_report       jsonb not null default '{}'::jsonb,
  quality_report      jsonb not null default '{}'::jsonb,
  revision_note       text not null default '',
  -- Remote preview (low-res, private bucket, signed URLs) vs the final file.
  preview_path        text not null default '',
  final_path          text not null default '',
  -- 実績とサンプルを区別する (要件 §1).
  is_sample           boolean not null default false,
  approved_version    int,
  approved_at         timestamptz,
  approved_by         text,
  approval_expires_at timestamptz,
  -- 本人操作の完了は承認とは別 (F03, F04): evidence is required.
  manual_done_at      timestamptz,
  manual_evidence     text not null default '',
  external_id         text not null default '',
  external_url        text not null default '',
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
create index if not exists items_status_idx   on public.items(owner_id, status, created_at desc);
create index if not exists items_business_idx on public.items(business_id, created_at desc);

create table if not exists public.approvals (
  id            bigint generated always as identity primary key,
  owner_id      uuid not null,
  item_id       uuid not null references public.items(id) on delete cascade,
  item_version  int  not null,
  action        text not null check (action in (
                  'approve','approve_override','auto_approve','revise','reject',
                  'hold','resubmit','invalidate','manual_done')),
  actor         text not null,
  note          text not null default '',
  created_at    timestamptz not null default now()
);
create index if not exists approvals_item_idx on public.approvals(item_id, created_at);

-- Allowed status transitions (F04 「許可された遷移だけ実行する」).
create table if not exists public.item_transitions (
  from_status text not null,
  to_status   text not null,
  primary key (from_status, to_status)
);
insert into public.item_transitions(from_status, to_status) values
  ('draft','image_queued'), ('draft','checking'), ('draft','pending_review'),
  ('draft','blocked'), ('draft','failed'), ('draft','cancelled'),
  ('image_queued','checking'), ('image_queued','waiting_pc'),
  ('image_queued','failed'), ('image_queued','cancelled'),
  ('checking','pending_review'), ('checking','blocked'), ('checking','approved'),
  ('checking','failed'), ('checking','cancelled'),
  ('pending_review','approved'), ('pending_review','needs_revision'),
  ('pending_review','rejected'), ('pending_review','on_hold'),
  ('pending_review','blocked'), ('pending_review','cancelled'),
  ('blocked','approved'), ('blocked','needs_revision'), ('blocked','rejected'),
  ('blocked','on_hold'), ('blocked','pending_review'), ('blocked','cancelled'),
  ('needs_revision','draft'), ('needs_revision','checking'),
  ('needs_revision','pending_review'), ('needs_revision','rejected'),
  ('needs_revision','cancelled'),
  ('on_hold','pending_review'), ('on_hold','rejected'), ('on_hold','cancelled'),
  ('approved','waiting_pc'), ('approved','awaiting_manual'), ('approved','scheduled'),
  ('approved','pending_review'), ('approved','failed'), ('approved','cancelled'),
  ('waiting_pc','awaiting_manual'), ('waiting_pc','scheduled'),
  ('waiting_pc','pending_review'), ('waiting_pc','failed'), ('waiting_pc','cancelled'),
  ('scheduled','published'), ('scheduled','waiting_pc'), ('scheduled','pending_review'),
  ('scheduled','failed'), ('scheduled','cancelled'),
  ('awaiting_manual','published'), ('awaiting_manual','pending_review'),
  ('awaiting_manual','failed'), ('awaiting_manual','cancelled'),
  ('failed','draft'), ('failed','image_queued'), ('failed','cancelled')
on conflict do nothing;

create or replace function public.guard_item_update() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  content_changed boolean;
begin
  content_changed :=
       new.title        is distinct from old.title
    or new.body         is distinct from old.body
    or new.meta         is distinct from old.meta
    or new.preview_path is distinct from old.preview_path;

  if content_changed then
    new.version := old.version + 1;
    -- 承認後の重要な変更は承認を無効化 (F03). The approval was for the old
    -- version; what would now be published is something nobody approved.
    if old.approved_version is not null then
      new.approved_version := null;
      new.approved_at := null;
      new.approved_by := null;
      if new.status in ('approved','waiting_pc','awaiting_manual','scheduled') then
        new.status := 'pending_review';
      end if;
      insert into public.approvals(owner_id, item_id, item_version, action, actor, note)
      values (old.owner_id, old.id, old.version, 'invalidate', 'system',
              'content changed after approval');
    end if;
  end if;

  if new.status is distinct from old.status then
    if not exists (select 1 from public.item_transitions
                   where from_status = old.status and to_status = new.status) then
      raise exception 'illegal status transition % -> %', old.status, new.status
        using errcode = 'check_violation';
    end if;
    -- `approved` has exactly one writer. Not the UI, not the pipeline, not the
    -- service role: only approve_item(), which sets this flag for its own
    -- transaction.
    if new.status = 'approved'
       and coalesce(current_setting('aiworker.via_approval', true), '') <> 'on' then
      raise exception 'items may only become approved through approve_item()'
        using errcode = 'insufficient_privilege';
    end if;
  end if;

  if new.status = 'approved' and new.approved_version is distinct from new.version then
    raise exception 'approved_version must match the current version'
      using errcode = 'check_violation';
  end if;

  new.updated_at := now();
  return new;
end $$;

drop trigger if exists items_guard on public.items;
create trigger items_guard before update on public.items
  for each row execute function public.guard_item_update();

create or replace function public.guard_item_insert() returns trigger
language plpgsql as $$
begin
  if new.status not in ('draft','image_queued','checking','pending_review','blocked') then
    raise exception 'new items must start unapproved (got %)', new.status
      using errcode = 'check_violation';
  end if;
  new.approved_version := null;
  return new;
end $$;

drop trigger if exists items_insert_guard on public.items;
create trigger items_insert_guard before insert on public.items
  for each row execute function public.guard_item_insert();

-- -----------------------------------------------------------------------------
-- controls (緊急停止) and workers (接続状態)
-- -----------------------------------------------------------------------------
create table if not exists public.controls (
  owner_id    uuid primary key,
  global_stop boolean not null default false,
  stop_reason text not null default '',
  stopped_at  timestamptz,
  updated_at  timestamptz not null default now()
);

create table if not exists public.workers (
  id           text primary key,
  owner_id     uuid not null,
  kind         text not null check (kind in ('cloud','mac')),
  last_seen_at timestamptz not null default now(),
  info         jsonb not null default '{}'::jsonb
);

-- -----------------------------------------------------------------------------
-- jobs (永続ジョブ)
-- -----------------------------------------------------------------------------
create table if not exists public.jobs (
  id              uuid primary key default gen_random_uuid(),
  owner_id        uuid not null,
  kind            text not null check (kind in ('generate','image','upscale','publish','sync')),
  runner          text not null default 'cloud' check (runner in ('cloud','mac')),
  item_id         uuid references public.items(id) on delete cascade,
  -- 重複防止キー (N03): the same logical operation can be enqueued once.
  idempotency_key text not null,
  status          text not null default 'queued'
                  check (status in ('queued','running','done','failed','cancelled')),
  attempts        int  not null default 0,
  max_attempts    int  not null default 3 check (max_attempts between 1 and 10),
  locked_by       text,
  locked_until    timestamptz,
  not_before      timestamptz not null default now(),
  payload         jsonb not null default '{}'::jsonb,
  result          jsonb not null default '{}'::jsonb,
  last_error      text not null default '',
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (owner_id, idempotency_key)
);
create index if not exists jobs_due_idx on public.jobs(runner, status, not_before);

-- Claim one due job atomically. SKIP LOCKED means a double-started runner
-- cannot pick up the same job twice (N03 排他制御).
create or replace function public.claim_job(p_runner text, p_worker text, p_lease_seconds int default 600)
returns setof public.jobs
language plpgsql security definer set search_path = public as $$
begin
  return query
  update public.jobs j
     set status = 'running', locked_by = p_worker,
         locked_until = now() + make_interval(secs => p_lease_seconds),
         attempts = j.attempts + 1, updated_at = now()
   where j.id = (
     select id from public.jobs
      where runner = p_runner
        and not_before <= now()
        and (status = 'queued'
             or (status = 'running' and locked_until < now()))  -- abandoned lease
        and attempts < max_attempts
      order by not_before, created_at
      for update skip locked
      limit 1)
  returning j.*;
end $$;

-- -----------------------------------------------------------------------------
-- events (監査・運用ログ)
-- -----------------------------------------------------------------------------
create table if not exists public.events (
  id         bigint generated always as identity primary key,
  owner_id   uuid not null,
  level      text not null default 'info' check (level in ('debug','info','warning','error','critical')),
  category   text not null,
  message    text not null,
  payload    jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists events_recent_idx on public.events(owner_id, created_at desc);

-- -----------------------------------------------------------------------------
-- revenue (F06): actual vs forecast, gross vs fee, confirmed vs pending
-- -----------------------------------------------------------------------------
create table if not exists public.revenue_entries (
  id          bigint generated always as identity primary key,
  owner_id    uuid not null,
  business_id uuid references public.businesses(id) on delete set null,
  occurred_on date not null,
  kind        text not null check (kind in (
                'sale','reward_confirmed','reward_pending','payout','fee','refund','cost')),
  amount      numeric(12,2) not null,
  currency    text not null default 'JPY',
  -- 実績値と予測値を分ける. A forecast must never be summed into actuals.
  is_actual   boolean not null default true,
  source      text not null,          -- 'csv:a8', 'manual', 'api:youtube' ...
  note        text not null default '',
  created_at  timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- approval functions -- the only way to approve, revise, reject or hold
-- -----------------------------------------------------------------------------
create or replace function public._actor() returns text
language sql stable as $$
  select case
    when auth.uid() is not null then 'user:' || auth.uid()::text
    when public.request_role() = 'service_role' then 'service'
    else ''
  end
$$;

create or replace function public._load_item_for_decision(p_item uuid)
returns public.items
language plpgsql security definer set search_path = public as $$
declare
  it public.items;
begin
  select * into it from public.items where id = p_item for update;
  if not found then
    raise exception 'item % not found', p_item using errcode = 'no_data_found';
  end if;
  -- A signed-in caller may act only on their own items, and only if they are
  -- the registered owner. The service role is trusted but restricted below.
  if auth.uid() is not null then
    if not public.is_owner() or it.owner_id <> auth.uid() then
      raise exception 'not allowed' using errcode = 'insufficient_privilege';
    end if;
  elsif public.request_role() <> 'service_role' then
    raise exception 'not allowed' using errcode = 'insufficient_privilege';
  end if;
  return it;
end $$;

-- approve_item(item, expected_version, note, force)
--   expected_version: the version the person was looking at. If the content
--   changed while they were reading it, the approval is refused -- they
--   approved something else.
--   force: override a guardrail block. Requires a written note.
-- The service role may only auto-approve: policy must be `auto`, the item must
-- not be blocking, and force is never honoured.
create or replace function public.approve_item(
  p_item uuid, p_expected_version int, p_note text default '', p_force boolean default false)
returns public.items
language plpgsql security definer set search_path = public as $$
declare
  it      public.items;
  acc     public.accounts;
  stopped boolean;
  is_auto boolean := auth.uid() is null;
  act     text;
begin
  it := public._load_item_for_decision(p_item);

  -- Idempotent: a double tap on 承認 is not an error and does nothing new.
  if it.status = 'approved' and it.approved_version = it.version then
    return it;
  end if;

  if it.version <> p_expected_version then
    raise exception 'item changed since you viewed it (v% now, you saw v%)',
      it.version, p_expected_version using errcode = 'serialization_failure';
  end if;

  if it.status not in ('checking','pending_review','blocked','on_hold') then
    raise exception 'cannot approve from status %', it.status using errcode = 'check_violation';
  end if;

  select global_stop into stopped from public.controls where owner_id = it.owner_id;
  if coalesce(stopped, false) and is_auto then
    raise exception 'global stop is on' using errcode = 'check_violation';
  end if;

  if it.account_id is not null then
    select * into acc from public.accounts where id = it.account_id;
  end if;

  if is_auto then
    if acc.id is null or acc.approval_policy <> 'auto' then
      raise exception 'auto-approval is not enabled for this account' using errcode = 'insufficient_privilege';
    end if;
    if it.blocking then
      raise exception 'a blocked item cannot be auto-approved' using errcode = 'insufficient_privilege';
    end if;
    if p_force then
      raise exception 'the service role cannot force an approval' using errcode = 'insufficient_privilege';
    end if;
    -- Test output made with a stand-in generator is never real work (要件 §1
    -- 実績とサンプルを区別): only a person may approve it, to try the flow.
    if it.is_sample then
      raise exception 'a sample cannot be auto-approved' using errcode = 'insufficient_privilege';
    end if;
    act := 'auto_approve';
  elsif it.blocking or it.status = 'blocked' then
    if not p_force then
      raise exception 'guardrails block this item; pass force with a reason to override'
        using errcode = 'check_violation';
    end if;
    if coalesce(trim(p_note), '') = '' then
      raise exception 'an override needs a written reason' using errcode = 'check_violation';
    end if;
    act := 'approve_override';
  else
    act := 'approve';
  end if;

  perform set_config('aiworker.via_approval', 'on', true);
  update public.items
     set status = 'approved', approved_version = version,
         approved_at = now(), approved_by = public._actor()
   where id = p_item
  returning * into it;
  perform set_config('aiworker.via_approval', 'off', true);

  -- The finished file already exists and a person publishes it by hand: move
  -- it straight onto their to-do list, rather than leaving it "approved"
  -- until the next scheduled run notices.
  -- A sample stays merely "approved": it must never show up as an upload task.
  if it.final_path <> '' and not it.is_sample
     and coalesce(acc.publish_method, 'manual') = 'manual' then
    update public.items set status = 'awaiting_manual' where id = p_item
    returning * into it;
  end if;

  insert into public.approvals(owner_id, item_id, item_version, action, actor, note)
  values (it.owner_id, it.id, it.version, act,
          case when is_auto then 'auto' else public._actor() end, coalesce(p_note, ''));
  insert into public.events(owner_id, level, category, message, payload)
  values (it.owner_id, case when act = 'approve_override' then 'warning' else 'info' end,
          'approval', act || ' ' || it.id::text,
          jsonb_build_object('item_id', it.id, 'version', it.version, 'note', p_note));
  return it;
end $$;

create or replace function public.decide_item(
  p_item uuid, p_action text, p_expected_version int, p_note text default '')
returns public.items
language plpgsql security definer set search_path = public as $$
declare
  it     public.items;
  target text;
begin
  if p_action not in ('revise','reject','hold','resubmit') then
    raise exception 'unknown action %', p_action using errcode = 'invalid_parameter_value';
  end if;
  if auth.uid() is null then
    raise exception 'only a person can %', p_action using errcode = 'insufficient_privilege';
  end if;
  it := public._load_item_for_decision(p_item);
  if it.version <> p_expected_version then
    raise exception 'item changed since you viewed it' using errcode = 'serialization_failure';
  end if;
  if p_action = 'revise' and coalesce(trim(p_note), '') = '' then
    raise exception 'a revision request needs a note saying what to change'
      using errcode = 'check_violation';
  end if;

  target := case p_action
    when 'revise'   then 'needs_revision'
    when 'reject'   then 'rejected'
    when 'hold'     then 'on_hold'
    when 'resubmit' then 'pending_review'
  end;
  if it.status = target then
    return it;  -- idempotent
  end if;

  update public.items
     set status = target,
         revision_note = case when p_action = 'revise' then p_note else revision_note end
   where id = p_item
  returning * into it;

  insert into public.approvals(owner_id, item_id, item_version, action, actor, note)
  values (it.owner_id, it.id, it.version, p_action, public._actor(), coalesce(p_note, ''));
  return it;
end $$;

-- 本人操作の完了 (F03/F04): separate from approval, and needs evidence.
create or replace function public.mark_manual_done(p_item uuid, p_evidence text)
returns public.items
language plpgsql security definer set search_path = public as $$
declare
  it public.items;
begin
  if coalesce(trim(p_evidence), '') = '' then
    raise exception 'record a URL or other evidence that it was done'
      using errcode = 'check_violation';
  end if;
  it := public._load_item_for_decision(p_item);
  if it.status <> 'awaiting_manual' then
    raise exception 'item is not waiting for a manual step (%)', it.status
      using errcode = 'check_violation';
  end if;
  update public.items
     set status = 'published', manual_done_at = now(), manual_evidence = p_evidence,
         external_url = case when p_evidence ~* '^https?://' then p_evidence else external_url end
   where id = p_item
  returning * into it;
  insert into public.approvals(owner_id, item_id, item_version, action, actor, note)
  values (it.owner_id, it.id, it.version, 'manual_done', public._actor(), p_evidence);
  return it;
end $$;

create or replace function public.set_global_stop(p_stop boolean, p_reason text default '')
returns public.controls
language plpgsql security definer set search_path = public as $$
declare
  c public.controls;
begin
  if auth.uid() is null or not public.is_owner() then
    raise exception 'not allowed' using errcode = 'insufficient_privilege';
  end if;
  if p_stop and coalesce(trim(p_reason), '') = '' then
    raise exception 'say why you are stopping' using errcode = 'check_violation';
  end if;
  insert into public.controls(owner_id, global_stop, stop_reason, stopped_at, updated_at)
  values (auth.uid(), p_stop, coalesce(p_reason, ''), case when p_stop then now() end, now())
  on conflict (owner_id) do update
    set global_stop = excluded.global_stop, stop_reason = excluded.stop_reason,
        stopped_at = excluded.stopped_at, updated_at = now()
  returning * into c;
  insert into public.events(owner_id, level, category, message)
  values (auth.uid(), case when p_stop then 'critical' else 'warning' end, 'killswitch',
          case when p_stop then '全体停止: ' || p_reason else '全体停止を解除' end);
  return c;
end $$;

-- -----------------------------------------------------------------------------
-- Row level security
-- -----------------------------------------------------------------------------
alter table public.businesses        enable row level security;
alter table public.accounts          enable row level security;
alter table public.settings_versions enable row level security;
alter table public.items             enable row level security;
alter table public.approvals         enable row level security;
alter table public.item_transitions  enable row level security;
alter table public.controls          enable row level security;
alter table public.workers           enable row level security;
alter table public.jobs              enable row level security;
alter table public.events            enable row level security;
alter table public.revenue_entries   enable row level security;

-- Read: owner sees their own rows.
do $$
declare t text;
begin
  foreach t in array array['businesses','accounts','settings_versions','items','approvals',
                           'controls','workers','jobs','events','revenue_entries'] loop
    execute format('drop policy if exists %I on public.%I', t || '_owner_read', t);
    execute format(
      'create policy %I on public.%I for select to authenticated
         using (public.is_owner() and owner_id = auth.uid())', t || '_owner_read', t);
  end loop;
end $$;

drop policy if exists owners_self_read on public.owners;
create policy owners_self_read on public.owners for select to authenticated
  using (user_id = auth.uid());

drop policy if exists item_transitions_read on public.item_transitions;
create policy item_transitions_read on public.item_transitions for select to authenticated
  using (public.is_owner());

-- Write: settings are edited directly (and versioned by trigger). Items,
-- approvals, controls, jobs and events have NO write policy for the browser:
-- every change to them goes through a function above.
drop policy if exists businesses_owner_write on public.businesses;
create policy businesses_owner_write on public.businesses for all to authenticated
  using (public.is_owner() and owner_id = auth.uid())
  with check (public.is_owner() and owner_id = auth.uid());

drop policy if exists accounts_owner_write on public.accounts;
create policy accounts_owner_write on public.accounts for all to authenticated
  using (public.is_owner() and owner_id = auth.uid())
  with check (public.is_owner() and owner_id = auth.uid());

drop policy if exists revenue_owner_write on public.revenue_entries;
create policy revenue_owner_write on public.revenue_entries for insert to authenticated
  with check (public.is_owner() and owner_id = auth.uid());

-- Privileges. Supabase is moving new projects to grant nothing by default
-- (Data API exposure becomes opt-in), so every grant the app relies on is
-- stated here rather than inherited, and the schema behaves the same on an
-- old project and a new one. `anon` gets nothing: there is no signed-out data.
revoke all on all tables    in schema public from anon, authenticated, service_role;
revoke all on all functions in schema public from public, anon, authenticated, service_role;

-- The owner's browser reads everything of theirs (RLS narrows it to their
-- rows), edits settings, and records revenue. It cannot write items,
-- approvals, controls, jobs or events at all -- not even rows RLS would let
-- through -- because those change only through the functions below.
grant select on public.owners, public.item_transitions, public.settings_versions,
                public.items, public.approvals, public.controls, public.workers,
                public.jobs, public.events                         to authenticated;
grant select, insert, update, delete on public.businesses, public.accounts to authenticated;
grant select, insert on public.revenue_entries                     to authenticated;

-- The pipeline (secret key). Trusted, but still cannot make anything
-- `approved` except through approve_item(), which the item trigger enforces.
grant select, insert, update, delete on all tables in schema public to service_role;
grant usage, select on all sequences in schema public              to service_role;

grant execute on function public.is_owner() to authenticated, service_role;

-- Functions callable from the browser.
revoke all on function public.approve_item(uuid,int,text,boolean)  from public;
revoke all on function public.decide_item(uuid,text,int,text)      from public;
revoke all on function public.mark_manual_done(uuid,text)          from public;
revoke all on function public.set_global_stop(boolean,text)        from public;
revoke all on function public.claim_job(text,text,int)             from public;
grant execute on function public.approve_item(uuid,int,text,boolean) to authenticated, service_role;
grant execute on function public.decide_item(uuid,text,int,text)     to authenticated;
grant execute on function public.mark_manual_done(uuid,text)         to authenticated;
grant execute on function public.set_global_stop(boolean,text)       to authenticated;
grant execute on function public.claim_job(text,text,int)            to service_role;
