# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A content operations pipeline that generates social/article/script/stock-asset
drafts with an LLM, screens them, and publishes approved ones on randomised
schedules. It exists to make platform bans *unlikely*, so most of the design
decisions here trade throughput for safety. Read `docs/01-architecture.md`
before changing anything in `guard/`, `approval/` or `scheduler/`.

## Commands

```bash
make test                       # pytest
make check                      # test + hygiene + ui  (what CI runs, minus smoke)
make smoke                      # E2E against an INSTALLED package (pip install . first)
make sync-templates             # after editing any repo-root template (see below)

python -m pytest tests/test_pipeline.py::test_unapproved_content_is_never_published
python -m pytest -k quota       # one area
python3 scripts/check_repo_hygiene.py    # secrets / state files / unsafe shipped defaults
python3 scripts/check_web_ui.py          # every UI screen still renders
```

Run the CLI without installing: `./scripts/aiworker <command>` (sets `PYTHONPATH=src`).

CI (`.github/workflows/ci.yml`) runs tests on Python 3.11/3.12/3.13, plus the
three checks above. `smoke` is separate on purpose: unit tests import from
`src/`, so they cannot see a packaging mistake — and two have already shipped
that way (`init` and `checklist` both failed on `pip install`).

## The invariant everything else serves

**Content that has not been approved never leaves the machine by any path.**

- Generation cannot write `APPROVED`. The best outcome of `generators/service.py`
  is `PENDING_REVIEW`; the worst is `BLOCKED`.
- `approval/service.py` is the *only* module that can move an item to
  `APPROVED`, and every transition lands in the `approvals` table with an actor.
- Guardrails re-run at approval time, over the current text — an edit between
  generation and approval cannot slip past checks that ran before the edit.
- Overriding a block needs `force=True` **and** a written reason, recorded as
  `approve_override` at WARNING.
- A platform may set `approval: auto`, which approves items that pass **every**
  guardrail without waiting for a person — routed through `approve()` with
  actor `auto`, never `force`, so the audit trail and the block behaviour are
  unchanged. Review is tiered by what a mistake costs, not applied uniformly:
  a rejected stock asset costs a re-upload, a bad post under the operator's own
  name costs the account.
- `scheduler/planner.py:_preflight` re-verifies everything immediately before
  handing an item to a publisher. Planning-time decisions are hints, never
  permission.

If a change would let content reach a publisher without passing all of the
above, it is wrong regardless of how convenient it is.

## Architecture notes that span files

**Dependency direction.** `core` depends on nothing; `guard` depends only on
`core` and `notify`; everything else sits above. Publishers are never called
directly — only `scheduler/planner.py` invokes them, so an adapter cannot
bypass a check by forgetting to call it.

**`Refusal(reason, kind)` in the scheduler** separates two cases that look
alike in a log and are opposite operationally. `block` means a human must look
(not approved, approval withdrawn, a guardrail verdict changed). `defer` means
the cadence rules say "not this minute" — the job moves to its next legal slot
and stays queued. Deferrals must never count towards the consecutive-failure
detector, or rate limiting working correctly would trip the kill switch.

**`STAGED` is not `PUBLISHED`.** A publisher declaring `stages_for_human`
(i.e. `manual`, the shipped default) only writes a file to `var/outbox/`. The
item waits in `STAGED` until a person confirms. But the posting slot is spent
at staging time — `db.published_timestamps` counts `STAGED` alongside `DONE`.
Over-counting a slot costs one post; under-counting costs an account.

**Quota counts reserved future slots**, not just past posts, so running `plan`
twice cannot double-book a day. At execution time a job must exclude *itself*
(`exclude_job_id`), or every scheduled job blocks on its own reservation.

**`db.daily_reach` falls back to impressions** where `reach` is absent, because
X exports impressions and YouTube exports views. Deliberately not
`MAX(reach, impressions)`: reach is always the smaller, so MAX would silently
always pick impressions and mix measures across days, making the median
comparison in the reach-drop detector meaningless.

**Guards do not scan `negative_prompt`.** It lists what an image must *not*
contain ("no logos, no real person"), so scanning it flags the safest prompts.
`core/models.NON_CONTENT_META` holds the exclusions; `guard/quality.py` checks
instead that those exclusions are present.

**`dry_run` is applied in one place**, `publishers/registry.py`. Anything
declaring `performs_network_io` is swapped for the no-op publisher there, so an
adapter that forgets to check the flag cannot defeat it. Adapters are reachable
only when explicitly listed in `ADAPTERS` — never by existing on disk.

**The database is migrated, never recreated.** It holds the approval history.
Schema changes go in `db.MIGRATIONS` keyed by version; see `_migrate_2_unique_metrics`
for the shape (dedupe existing rows *before* adding a constraint, or it fails on
a live database).

## Conventions

- **Operator-facing output is Japanese** (CLI, web UI, docs, alerts). Code,
  comments, docstrings, commit messages and test names are English. Status enum
  values stay English in the database and get Japanese labels at display time.
- **Secrets never appear in YAML.** `config/config.yaml` holds behaviour;
  `${ENV:VAR}` is the only way to pull a credential in, resolved at load.
  `require_human_approval: false` is a config *error* — unattended publishing is
  out of scope by design, not by default.
- **Templates exist twice.** `config/config.example.yaml`, `.env.example`,
  `config/policy/banned_terms.example.yaml` and `docs/05-risk-checklist.md` are
  canonical at the repository root (that is where people browse) and are copied
  into `src/aiworker/data/` (that is where the CLI reads them, so they work from
  a `pip install`). `tests/test_resources.py` fails when they drift — run
  `make sync-templates`.
- **The default LLM provider is `mock`**: offline, seeded, no API key, obviously
  placeholder text. It exists so the whole pipeline is testable without spend.
  Tests rely on it; don't replace it with a network call.
- The web UI is server-rendered with no framework and no build step. The one
  script in it is the outbox copy button, because that screen's entire job is
  "copy this, paste it there".

## What is deliberately not implemented

Documented in `docs/06-roadmap.md`; the short version, because these look like
gaps and are decisions:

- **Automated posting is not the default.** note has no public posting API, and
  automating it through an unofficial path is precisely the risk this system
  exists to avoid. Generation, screening, scheduling and record-keeping are
  automated; a person presses post.
- **Revenue and analytics come from CSV, not scraping.** Logging into a console
  to scrape numbers would risk the account to populate a dashboard.
- Detection evasion (proxy rotation and similar) is out of scope.
