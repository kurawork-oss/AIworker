-- =============================================================================
-- Storage buckets (Supabase only -- depends on the `storage` schema).
--
-- 遠隔プレビューを無条件の公開URLで配布しない (N02): both buckets are private.
-- The phone gets a short-lived signed URL after logging in; nothing is
-- reachable by guessing a path.
--
-- Layout: <owner uuid>/<item uuid>/<file>
--   previews -- ~1MP, what you look at on the phone before approving
--   finals   -- the file to publish: the >=4MP stock JPEG, or a Shorts MP4
--               (50 MB is the free plan's per-file ceiling)
-- Only the pipeline (service role) writes; the owner reads their own folder.
-- =============================================================================

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
  ('previews', 'previews', false, 5 * 1024 * 1024,  array['image/png','image/jpeg','image/webp']),
  ('finals',   'finals',   false, 50 * 1024 * 1024, array['image/jpeg','image/png','video/mp4'])
on conflict (id) do update
  set public = false,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "owner reads own previews" on storage.objects;
create policy "owner reads own previews" on storage.objects
  for select to authenticated
  using (
    bucket_id in ('previews', 'finals')
    and public.is_owner()
    and (storage.foldername(name))[1] = auth.uid()::text
  );
