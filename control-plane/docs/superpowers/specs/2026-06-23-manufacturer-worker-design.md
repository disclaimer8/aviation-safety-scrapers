# Manufacturer Discovery Worker (Worker 5) — Design

**Date:** 2026-06-23
**Status:** Approved (brainstorm) — pending implementation plan
**Repo:** aviation-safety-scrapers / control-plane

## Summary

A discovery+staging worker for **Airbus Safety First** — the manufacturer safety
magazine whose issues are per-incident safety analyses. The worker discovers
published issues (PDFs), and stages them into a new `staged_manufacturer_documents`
table for later OCR/extract (via the unified extract worker). It mirrors the
regional/foreign discovery+staging pattern, but is a **single global source**
(not per-country) so it runs standalone, not off the per-country `crawl_jobs`
queue.

Worker 5 of roadmap sub-projects 1–6. MVP is **Airbus Safety First only**.

## Why Safety First (and why only it for MVP)

Research (`/tmp/mfr-safety-sources.md`, verified live) compared manufacturer
safety publications:

- **Airbus Safety First** (`safetyfirst.airbus.com`) — per-incident analyses,
  ~41 issues, WordPress + SSR + sitemap, no JS/Cloudflare/login. **Copyright:
  reprint permitted with attribution** (printed notice) — the only manufacturer
  source that sanctions excerpting, not just facts. → best fit for the
  document→event model AND best legality.
- **Airbus Statistical Analysis** / **Boeing Statistical Summary** — *aggregate*
  statistics (counts/rates by year/phase), all-rights-reserved → facts-only.
  These are **not per-incident documents**, so they do not fit the
  staging→extract→`events` model. **Deferred** to a separate "manufacturer-stats"
  ingestion (different shape, facts-only, explainer-first like ASN/IATA).
- Boeing AERO (discontinued), Embraer (member-gated), Safran (no fleet data) —
  dropped.

## Key decisions (from brainstorming)

1. MVP source = **Airbus Safety First only**; statistical summaries deferred.
2. **Global / standalone** — not per-country; runs as a one-shot
   `process-manufacturer` command, NOT driven by per-country `crawl_jobs`.
3. **Discovery + staging only** this worker; OCR/extract→`events` is handled by
   the unified extract worker (Worker 4) via a later manufacturer adapter.
4. Legality: Safety First's reprint-with-attribution notice permits excerpting;
   still attribute + link. (The deferred stats sources are facts-only.)

## Architecture

New package `internal/worker/manufacturer/` (mirrors `internal/worker/regional/`,
simplified to one source):

- `safetyfirst.go` — `Client` that fetches the Safety First issue index (sitemap
  / magazine listing) and resolves issue PDF URLs; supports `--source-file`
  (out-of-band HTML) like the regional worker, for resilience.
- `parse.go` — parse the listing HTML into issue records (issue number, title,
  PDF URL, publication date); http(s)-scheme allow-list on resolved URLs (CodeQL).
- `record.go` — the staged record shape + validation.
- `stage.go` — idempotent upsert into `staged_manufacturer_documents`
  (`ON CONFLICT(publication, issue_ref) DO NOTHING`).
- `runner.go` — `ProcessManufacturer(ctx, db, client, opts)`: discover → stage →
  return a result (`{found, staged, errors}`); record a provenance run + a
  `crawl_errors`-style row per failure if the provenance mechanism is cheap to
  reuse (else log). No crawl_jobs to finalize (global source).

Discovery strategy (from research): read the magazine sitemap/listing for known
issues, AND probe `safety_first_<N+1>.pdf` (S3 path) to detect a new issue beyond
the highest known number. Stage each issue once.

## Schema — migration 009 (renumber at rebase if Worker 4's 009 lands first)

```sql
-- NNN_manufacturer.sql
CREATE TABLE staged_manufacturer_documents (
  id INTEGER PRIMARY KEY,
  manufacturer TEXT NOT NULL,                 -- 'airbus'
  publication  TEXT NOT NULL,                 -- 'safety_first'
  issue_ref    TEXT NOT NULL,                 -- e.g. '41'
  title        TEXT NOT NULL,
  publication_date TEXT,                      -- ISO 'YYYY-MM-DD' when known
  original_url TEXT NOT NULL,                  -- issue/landing page
  report_url   TEXT,                           -- the PDF
  mimetype     TEXT,
  -- download + extraction state (same shape as other staging tables, so the
  -- unified extract worker's adapter can drain it):
  download_status TEXT NOT NULL DEFAULT 'pending'
    CHECK(download_status IN ('pending','downloaded','failed','skipped')),
  local_file_path TEXT,
  digest TEXT,
  ocr_text_path TEXT,
  extraction_status TEXT NOT NULL DEFAULT 'pending'
    CHECK(extraction_status IN ('pending','ocr_done','extracted','failed','skipped')),
  extraction_error TEXT,
  extraction_attempts INTEGER NOT NULL DEFAULT 0,
  event_id INTEGER REFERENCES events(id),
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  UNIQUE(publication, issue_ref)
) STRICT;
```

No `country_id`/`crawl_job_id` (global, not per-country). Bump the migration
count guard in `migrations_test.go`.

## Command

`internal/app/app.go`: add `process-manufacturer` (flags `--db`, `--limit`,
`--source-file` for out-of-band listing HTML, `--timeout`). Builds the Safety
First client, runs `ProcessManufacturer`, prints `{found, staged, errors}`.
Update usage strings + README.

## Error handling / robustness

- Idempotent staging (re-run stages 0 new).
- Per-issue failures isolated; a non-200 / non-PDF issue is recorded/logged, the
  rest proceed.
- http(s)-only on resolved URLs (CodeQL), as in the regional parser.
- `--source-file` fallback so a transient site/network problem doesn't block.

## Testing

- `parse.go` on a testdata fixture (saved Safety First listing HTML) → exact
  issue records (number, title, PDF URL, date); rejects non-http(s) URLs;
  ignores nav/external anchors.
- `stage.go` idempotency: stage twice → second stages 0 (UNIQUE(publication,
  issue_ref)); distinct issues distinct rows.
- `runner.go` happy path with a fake client + in-memory DB (`migrations.Apply`):
  discovers N, stages N, result `{found:N, staged:N, errors:0}`.
- Migration: table created with expected columns, idempotent, count guard +1.
- `process-manufacturer` command via `app.Run` (exit 0, prints counts) — mirror
  the existing `app_regional_test.go` pattern.
- `go build ./... && go vet ./... && go test ./...` green locally
  (⚠️ repo CI runs CodeQL only — no `go test`).

## Known traps

- Migration numbering: Worker 4 (concurrent cloud-agent) is also taking **009** —
  branch off fresh main and **renumber at rebase** (like PR #9: 006→008).
- A concurrent cloud-agent is active in this repo — rebase before merge; expect
  migration-number + `app.go` command conflicts (resolve: renumber + keep both
  commands).
- This repo CI is CodeQL-only — validate with `go test ./...` locally.
- Safety First S3 PDF path pattern (`safety_first_<N>.pdf`) is the research's
  finding — verify the exact current path during implementation before hardcoding.

## Out of scope

- OCR/extract → `events` (Worker 4 unified pipeline; add a manufacturer
  StagedDocSource adapter as a follow-up once both land).
- Airbus Statistical Analysis + Boeing Statistical Summary (aggregate stats,
  facts-only — separate manufacturer-stats ingestion).
- Other manufacturers (Boeing CASO, Bombardier, ATR) — Safety First only for MVP.
- Per-country `crawl_jobs` integration / a `manufacturer_discovery` job type.
