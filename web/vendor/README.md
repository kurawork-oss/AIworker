# Vendored libraries

| File | Package | Version | Licence | SHA-256 |
|---|---|---|---|---|
| `supabase-2.117.3.js` | `@supabase/supabase-js` (`dist/umd/supabase.js`) | 2.117.3 (2026-10-07) | MIT (`supabase-LICENSE`) | `d6a5c4414a5d4ce646d9c1de223aa7067d3ff664c15394ffeb7fcffc763354a3` |

Served from this site rather than a CDN: the page's Content-Security-Policy
then allows scripts from its own origin only, and the app keeps working if a
CDN is down or blocked. To update: `npm pack @supabase/supabase-js@<version>`,
copy `package/dist/umd/supabase.js` here under the new version's name, and
update `index.html`, `sw.js` and this table.
