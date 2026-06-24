# Unified PDF Extract Worker (Worker 4) — Design

**Date:** 2026-06-23
**Status:** Approved (brainstorm) — pending implementation plan
**Repo:** aviation-safety-scrapers / control-plane

## Summary

Generalize the OCR→LLM→promote extraction pipeline so it processes staged
documents from **all three discovery workers** — Wayback (Worker 1), regional
(Worker 3), and foreign-search (Worker 2) — not just Wayback. Today only
`staged_wayback_documents` has an extraction pipeline (`process-wayback-extract`,
PRs #7/#8). `staged_regional_documents` and `staged_foreign_documents` are
discovered + staged but never turned into `events`/`reports`.

Worker 4 introduces a **source-agnostic extract core** with one
`StagedDocSource` interface and three adapters (wayback, regional, foreign), and
a unified `process-extract` command that drains all three. The existing Wayback
extract behavior is preserved exactly (its tests are the regression net).

## Current state (verified)

- `internal/worker/wayback/`: `extract.go` (pure: `HasCriticalFields`,
  `ConfidenceScore`, `NormalizeEvent`, `normalizeEnum`), `extractrunner.go`
  (`ProcessExtractPending`, `ExtractOne`, `recordExtractFailure` — hardcoded to
  `staged_wayback_documents`), `ocr.go`/`llm.go` (injected `OCRClient`/`LLMClient`
  interfaces), `promote.go` (`PromoteDocument`, `ResolveSource` — wayback tier-2
  fallback), `download.go`/`cdx.go`/`fetcher.go` (CDX snapshot download),
  `prompts/extract.txt`.
- `staged_wayback_documents` (mig 005/006): has `download_status`,
  `local_file_path`, `digest`, `extraction_status`, `extraction_error`,
  `extraction_attempts`, `event_id`, `crawl_job_id`, `country_id`, `original_url`.
- `staged_regional_documents` (007) & `staged_foreign_documents` (008): have
  `crawl_job_id`, `country_id`, `original_url`, `report_url`, body/authority refs,
  but **no download/extraction columns**.
- `process-wayback-extract` command wired in `internal/app/app.go`.
- ⚠️ Repo CI runs CodeQL only — **no `go test`**; run `go test ./...` locally.
- Migrations top out at `008_foreign` → this worker uses **009**.

## Key decisions (from brainstorming)

1. **Full unification** of all three sources under one source-agnostic extract
   worker (wayback refactored to be one adapter, not left separate).
2. **Command:** new `process-extract` drains all three in country-priority order;
   keep `process-wayback-extract` as a thin **deprecated alias** → no ops/cron
   breakage.
3. **Download for regional/foreign:** direct HTTP GET of `report_url` → local PDF
   + digest. CF/blocked → `download_status='failed'` (retryable / out-of-band
   later); **no render-service in MVP**.
4. Fold in the known follow-up **I1**: `recordExtractFailure` records a real
   `error_type` (transport / parse / ocr / llm), not always `'unknown'`.

## Architecture

New package `internal/worker/extract/` holding the source-agnostic core, plus
per-source adapters. The pure `extract.go` logic and the `OCRClient`/`LLMClient`
interfaces are reused (moved or imported).

```
StagedDocSource interface {
    Name() string                                  // "wayback" | "regional" | "foreign"
    PendingDocs(ctx, db, limit) ([]ExtractDoc, error)  // source-specific SELECT (its table + country/iso2 join), priority-ordered, status IN ('pending','ocr_done','failed')
    DownloadURL(doc) string                        // wayback: archived/CDX URL; regional/foreign: report_url (fallback original_url)
    ResolveSource(ctx, q, doc) (sourceID int64, tier int, copyright string, err error)  // wayback: tier-2 from waybackTarget; regional: regional body source; foreign: foreign authority source
    MarkStatus(ctx, db, docID, status string, extras...) error  // UPDATE its own table
}
```

Shared core (`extract.go` orchestration, parameterized by a `StagedDocSource`):
1. `download` the `DownloadURL(doc)` to `storeDir` → local file + `digest`
   (wayback uses its CDX path; regional/foreign use a direct HTTP fetch — the
   download step is part of each adapter or a shared fetch keyed off a flag).
2. OCR (`OCRClient`) → text.
3. LLM (`LLMClient`) with `prompts/extract.txt` → `ExtractedEvent`.
4. `NormalizeEvent` + `ConfidenceScore`; `HasCriticalFields` gate.
5. `PromoteDocument` → `events`/`reports`, crediting `ResolveSource(doc)`.
6. `MarkStatus` per outcome (`extracted`/`skipped`/`failed` with `error_type`).

`ProcessExtractPending(ctx, db, ocr, llm, storeDir, limit, sources...)` iterates
the sources, interleaving by country priority, up to `limit` total.

`promote.go`: keep the generic `events`/`reports` insert + dedup; make
`ResolveSource` an adapter responsibility (wayback keeps its tier-2 fallback;
regional/foreign credit their body/authority source row, look-up-or-create on
`UNIQUE(canonical_url, source_type)` like the existing `upsertSource`).

## Schema — migration 009

`ALTER TABLE staged_regional_documents` and `staged_foreign_documents` to add the
same download/extraction columns `staged_wayback_documents` has:
`download_status TEXT NOT NULL DEFAULT 'pending' CHECK(...)`, `local_file_path TEXT`,
`digest TEXT`, `extraction_status TEXT NOT NULL DEFAULT 'pending' CHECK(...)`,
`extraction_error TEXT`, `extraction_attempts INTEGER NOT NULL DEFAULT 0`,
`event_id INTEGER REFERENCES events(id)`. Idempotent (guard each ADD COLUMN by
`PRAGMA table_info` or rely on the runner's per-version application). Bump the
migration count guard in `migrations_test.go` (8→9).

## Command

`internal/app/app.go`: add `process-extract` (unified) constructing all three
adapters + the OCR/LLM clients (same flags as `process-wayback-extract`:
`--db --limit --store-dir --ocr-endpoint --llm-endpoint --llm-model
--max-input-chars`). Make `process-wayback-extract` a deprecated alias that runs
`process-extract` restricted to the wayback source (or prints a deprecation note
and delegates). Update usage strings + README.

## Download (regional/foreign)

A direct HTTP fetch of `report_url` (fallback `original_url`) → `storeDir` file +
sha256 `digest`, setting `download_status`. Reuse the wayback `fetcher`/`download`
helpers where generic; a non-200 / CF-challenge / non-PDF body → `failed`
(retryable). PDFs are usually direct; CF-blocked report_urls are out-of-band
(future), consistent with the discovery side.

## Error handling / robustness

- Per-doc failures are isolated (one bad doc doesn't abort the batch),
  `extraction_attempts` increments, `error_type` recorded (transport/ocr/llm/
  parse/unknown).
- Stale-resume: a doc left mid-flight is re-selected (status IN
  pending/ocr_done/failed), idempotent like the wayback worker.
- Promotion dedup unchanged (existing `events` dedup_status logic).

## Testing

- Per-adapter unit tests: `PendingDocs` SELECT (its table, priority order,
  status filter) on seeded fixtures; `DownloadURL`; `ResolveSource` (correct
  source-credit + look-up-or-create) for regional + foreign; wayback adapter
  preserves tier-2.
- Unified `ProcessExtractPending` across multiple sources (interleave + limit)
  with injected fake OCR/LLM.
- Migration 009: columns added, idempotent, count guard 8→9.
- `recordExtractFailure` records the right `error_type` (I1).
- **Regression:** ALL existing wayback extract tests stay green after the
  refactor — this is the safety net for touching the live path.
- `go build ./...`, `go vet ./...`, `go test ./...` all green locally.

## Known traps

- Migration numbering: **009** (008 just merged; verify on fresh main).
- The refactor touches the LIVE wayback extract path — keep its tests green.
- This repo CI is CodeQL-only — validate with `go test ./...` locally.
- A concurrent cloud-agent is active in this repo — rebase before merge, expect
  migration-number / app.go-command conflicts (resolve like #9: renumber + keep
  both commands).
- URL scheme allow-listing (http/https only) for `report_url` downloads
  (CodeQL — mirror the regional parser's fix).

## Out of scope

- Render-service / CF-bypass download for blocked report_urls (out-of-band,
  future).
- New discovery (workers 5/6 mfr/MSN) — separate specs.
- Changing the OCR/LLM endpoints or the extraction prompt.
