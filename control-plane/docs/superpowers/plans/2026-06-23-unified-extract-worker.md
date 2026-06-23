# Unified PDF Extract Worker (Worker 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the OCR→LLM→promote pipeline source-agnostic so it turns staged Wayback, regional, AND foreign-search documents into `events`/`reports` — via one `StagedDocSource` interface, three adapters, and a unified `process-extract` command.

**Architecture:** Extract a generic core (download→OCR→LLM→normalize→promote) parameterized by a `StagedDocSource`. Wayback's existing extract becomes one adapter (behavior + tests preserved); regional and foreign get adapters that download their `report_url`, then run the same core. A migration adds the download/extraction columns regional/foreign currently lack.

**Tech Stack:** Go 1.24+, SQLite (modernc/mattn), the existing `OCRClient`/`LLMClient` HTTP clients + `prompts/extract.txt`.

## Global Constraints

- Repo CI runs **CodeQL only — no `go test`**. Validate every task with `go build ./... && go vet ./... && go test ./...` locally (run from `control-plane/`).
- Migration number is **009** (008_foreign is latest; verify `ls internal/migrations/sql/` on fresh main before writing).
- `staged_regional_documents` / `staged_foreign_documents` are `STRICT` tables — added columns must be `TEXT`/`INTEGER` only (they are).
- The refactor touches the LIVE Wayback extract path — **all existing `internal/worker/wayback` tests must stay green** (regression net). Do not change Wayback's observable behavior.
- URL downloads: allow `http`/`https` schemes only (CodeQL), mirroring the regional parser's scheme guard.
- A concurrent cloud-agent is active in this repo — rebase before merge; resolve migration-number / app.go-command conflicts like PR #9 (renumber + keep all commands).
- Branch: `feat/unified-extract` off fresh `origin/main`. Commit after every task.

## File Structure

- New package `internal/worker/extract/`:
  - `source.go` — `StagedDocSource` interface, `ExtractDoc`, `ExtractStats`, `ExtractedEvent` (moved from wayback), shared `OCRClient`/`LLMClient` interfaces.
  - `core.go` — generic `ProcessExtractPending(ctx, db, ocr, llm, storeDir, limit, sources...)`, `extractOne`, `recordFailure` (with real `error_type`), `persistOCRText`.
  - `extractlogic.go` — pure `NormalizeEvent`/`HasCriticalFields`/`ConfidenceScore`/`normalizeEnum` (moved from wayback `extract.go`).
  - `promote.go` — generic `PromoteDocument` (events/reports insert + dedup) + `upsertSource` helper; per-source credit comes from the adapter's `ResolveSource`.
  - `download.go` — shared `DownloadReportURL(ctx, url, storeDir, iso2, digestSeed)` for regional/foreign (http(s) only).
  - `wayback_source.go`, `regional_source.go`, `foreign_source.go` — the three adapters.
- `internal/worker/wayback/`: keep CDX discovery/download (`cdx.go`, `download.go`, `fetcher.go`, `stage.go`, `runner.go`, `target.go`); its OCR/LLM HTTP client constructors (`NewHTTPOCRClient`, `NewHTTPLLMClient`) stay (re-exported or imported by `extract`). The wayback **extract** files (`extract.go`, `extractrunner.go`, `promote.go`, `llm.go` types) move into `extract` as the core + wayback adapter.
- `internal/migrations/sql/009_extract_columns.sql` (+ `migrations_test.go` count 8→9, + a focused migration test).
- `internal/app/app.go` — `process-extract` command + `process-wayback-extract` deprecated alias.

---

### Task 1: Migration 009 — download/extraction columns on regional + foreign

**Files:**
- Create: `internal/migrations/sql/009_extract_columns.sql`
- Create: `internal/migrations/migrations_extractcols_test.go`
- Modify: `internal/migrations/migrations_test.go` (count 8→9)

**Interfaces:**
- Produces: `staged_regional_documents` and `staged_foreign_documents` each gain `download_status`, `local_file_path`, `digest`, `ocr_text_path`, `extraction_status`, `extraction_error`, `extraction_attempts`, `event_id` — matching `staged_wayback_documents`.

- [ ] **Step 1: Verify migration number is free**

Run: `ls internal/migrations/sql/ | tail -3`
Expected: highest is `008_foreign.sql`. Use `009`.

- [ ] **Step 2: Write the failing test**

```go
// internal/migrations/migrations_extractcols_test.go
package migrations

import (
	"database/sql"
	"testing"
	_ "modernc.org/sqlite"
)

func cols(t *testing.T, db *sql.DB, table string) map[string]bool {
	t.Helper()
	rows, err := db.Query("SELECT name FROM pragma_table_info(?)", table)
	if err != nil { t.Fatal(err) }
	defer rows.Close()
	got := map[string]bool{}
	for rows.Next() { var n string; if err := rows.Scan(&n); err != nil { t.Fatal(err) }; got[n] = true }
	return got
}

func TestMigration009AddsExtractColumns(t *testing.T) {
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil { t.Fatal(err) }
	defer db.Close()
	if err := Apply(db); err != nil { t.Fatal(err) }
	want := []string{"download_status", "local_file_path", "digest", "ocr_text_path",
		"extraction_status", "extraction_error", "extraction_attempts", "event_id"}
	for _, table := range []string{"staged_regional_documents", "staged_foreign_documents"} {
		got := cols(t, db, table)
		for _, c := range want {
			if !got[c] { t.Errorf("%s missing column %s", table, c) }
		}
	}
}
```

> Confirm the migration entrypoint is `Apply(db)` (read `migrations.go`); if it differs (e.g. `apply` / takes a dir), match the existing call used in `migrations_test.go`.

- [ ] **Step 3: Run test to verify it fails**

Run: `go test ./internal/migrations/ -run TestMigration009 -v`
Expected: FAIL (columns missing).

- [ ] **Step 4: Write the migration**

```sql
-- 009_extract_columns.sql
-- Give staged_regional_documents and staged_foreign_documents the same
-- download + extraction state machine columns staged_wayback_documents has
-- (005/006), so the unified extract worker (Worker 4) can process them.
ALTER TABLE staged_regional_documents ADD COLUMN download_status TEXT NOT NULL DEFAULT 'pending'
  CHECK(download_status IN ('pending','downloaded','failed','skipped'));
ALTER TABLE staged_regional_documents ADD COLUMN local_file_path TEXT;
ALTER TABLE staged_regional_documents ADD COLUMN digest TEXT;
ALTER TABLE staged_regional_documents ADD COLUMN ocr_text_path TEXT;
ALTER TABLE staged_regional_documents ADD COLUMN extraction_status TEXT NOT NULL DEFAULT 'pending'
  CHECK(extraction_status IN ('pending','ocr_done','extracted','failed','skipped'));
ALTER TABLE staged_regional_documents ADD COLUMN extraction_error TEXT;
ALTER TABLE staged_regional_documents ADD COLUMN extraction_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE staged_regional_documents ADD COLUMN event_id INTEGER REFERENCES events(id);

ALTER TABLE staged_foreign_documents ADD COLUMN download_status TEXT NOT NULL DEFAULT 'pending'
  CHECK(download_status IN ('pending','downloaded','failed','skipped'));
ALTER TABLE staged_foreign_documents ADD COLUMN local_file_path TEXT;
ALTER TABLE staged_foreign_documents ADD COLUMN digest TEXT;
ALTER TABLE staged_foreign_documents ADD COLUMN ocr_text_path TEXT;
ALTER TABLE staged_foreign_documents ADD COLUMN extraction_status TEXT NOT NULL DEFAULT 'pending'
  CHECK(extraction_status IN ('pending','ocr_done','extracted','failed','skipped'));
ALTER TABLE staged_foreign_documents ADD COLUMN extraction_error TEXT;
ALTER TABLE staged_foreign_documents ADD COLUMN extraction_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE staged_foreign_documents ADD COLUMN event_id INTEGER REFERENCES events(id);
```

- [ ] **Step 5: Bump the count guard**

In `internal/migrations/migrations_test.go`, change the `len(migrations) != 8` / `want 8` assertion to `9`.

- [ ] **Step 6: Run tests + commit**

Run: `go test ./internal/migrations/...`  → PASS.
```bash
git add internal/migrations/
git commit -m "feat(extract): migration 009 — download+extraction columns on regional/foreign staging"
```

---

### Task 2: Move the source-agnostic core into `internal/worker/extract`

**Files:**
- Create: `internal/worker/extract/source.go`, `extractlogic.go`, `promote.go`, `core.go`
- Modify: `internal/worker/wayback/` (remove the moved files: `extract.go` pure logic, `extractrunner.go`, `promote.go`; relocate `ExtractDoc`/`ExtractedEvent` types)
- Test: existing wayback extract tests move to `internal/worker/extract/` and stay green

**Interfaces:**
- Produces:
  ```go
  package extract
  type ExtractDoc struct { ID, CountryID int64; ISO2, Digest, LocalFilePath, OriginalURL, ArchivedURL string
      OCRTextPath, Checksum sql.NullString; WaybackTarget string; Attempts int; CrawlJobID int64 }
  type ExtractStats struct { Extracted, Skipped, Failed int }
  type OCRClient interface { OCR(ctx, []byte) (string, error) }
  type LLMClient interface { Extract(ctx, string) (ExtractedEvent, error) }
  type StagedDocSource interface {
      Name() string
      PendingDocs(ctx, db *sql.DB, limit int) ([]ExtractDoc, error)
      EnsureDownloaded(ctx, db *sql.DB, storeDir string, doc *ExtractDoc) error  // wayback: no-op (pre-downloaded); regional/foreign: fetch report_url
      ResolveSource(ctx, q execQuerier, doc ExtractDoc) (sourceID int64, tier int, copyright string, err error)
      MarkSkipped(ctx, db *sql.DB, id int64) error
      MarkExtracted(ctx, db *sql.DB, id, eventID int64) error
      RecordFailure(ctx, db *sql.DB, doc ExtractDoc, url, errType string, cause error) error
      PersistOCRPath(ctx, db *sql.DB, id int64, path string) error
  }
  func ProcessExtractPending(ctx, db, ocr OCRClient, llm LLMClient, storeDir string, limit int, sources ...StagedDocSource) (ExtractStats, error)
  func PromoteDocument(ctx, db *sql.DB, src StagedDocSource, doc ExtractDoc, e ExtractedEvent) (int64, bool, error)
  func NormalizeEvent(ExtractedEvent) ExtractedEvent; func HasCriticalFields(ExtractedEvent) bool; func ConfidenceScore(ExtractedEvent, bool) int
  ```

- [ ] **Step 1: Move the pure logic + types (mechanical)**

`git mv` then re-`package` to `extract`:
- `internal/worker/wayback/extract.go` → `internal/worker/extract/extractlogic.go` (`package extract`).
- The `ExtractedEvent` struct + `OCRClient`/`LLMClient` interfaces from `wayback/llm.go`/`ocr.go`: move the **type definitions** to `extract/source.go`; leave the HTTP client *constructors* (`NewHTTPOCRClient`, `NewHTTPLLMClient`) in `wayback` but have them return types that satisfy `extract.OCRClient`/`extract.LLMClient` (structural — Go interfaces are implicit, so no import cycle if the constructors' return types implement the methods).
- `ExtractDoc` (from `wayback/promote.go`) → `extract/source.go` (add `CrawlJobID int64`).

- [ ] **Step 2: Write `core.go` — generic orchestration**

Port `ExtractOne`/`ProcessExtractPending`/`recordExtractFailure`/`PersistOCRText` from `wayback/extractrunner.go` into `extract/core.go`, replacing every hardcoded `staged_wayback_documents` reference and status UPDATE with the `StagedDocSource` methods (`MarkSkipped`, `MarkExtracted`, `RecordFailure`, `PersistOCRPath`), and call `src.EnsureDownloaded(ctx, db, storeDir, &doc)` at the top of `extractOne` before the OCR read. `ProcessExtractPending` collects `PendingDocs` from every source, merges, sorts by country priority (carry a `Priority` field on `ExtractDoc` or sort within each source and round-robin), applies `limit`, and runs each through `extractOne`. Record the real `error_type` in `RecordFailure` (transport/ocr/llm/parse) — the adapter writes the `crawl_errors` row.

- [ ] **Step 3: Write `promote.go` — source-parameterized**

Port `PromoteDocument`/`upsertSource` from `wayback/promote.go`; change `ResolveSource` from a wayback-specific function to a call into `src.ResolveSource(ctx, tx, doc)`. The events/reports INSERT + dedup logic is unchanged.

- [ ] **Step 4: Move the wayback extract tests**

`git mv` the existing wayback extract test files into `internal/worker/extract/` (package `extract`), updating references to the new symbols. These are the regression net.

- [ ] **Step 5: Build + full test**

Run: `go build ./... && go vet ./... && go test ./...`
Expected: compiles; the moved wayback extract tests still pass against the generic core (driven by the wayback adapter from Task 3 — so Task 3 may need to land together; if so, combine Tasks 2+3 into one commit). If the wayback adapter isn't ready, temporarily keep a thin wayback shim so the suite builds, removed in Task 3.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "refactor(extract): move OCR→LLM→promote core into source-agnostic extract package"
```

> NOTE: Tasks 2 and 3 are tightly coupled (the core can't be tested without at least the wayback adapter). The implementer may merge them into one commit; keep the reviewer gate at the end of Task 3.

---

### Task 3: Wayback adapter (preserve existing behavior)

**Files:**
- Create: `internal/worker/extract/wayback_source.go`
- Test: the moved wayback extract tests now exercise this adapter

**Interfaces:**
- Consumes: `StagedDocSource` (Task 2).
- Produces: `type WaybackSource struct{}` implementing `StagedDocSource`: `PendingDocs` runs the exact existing SELECT on `staged_wayback_documents` (download_status='downloaded', extraction_status IN pending/ocr_done/failed, attempts<3, ORDER BY priority); `EnsureDownloaded` = no-op (already downloaded); `ResolveSource` = the existing tier-2-from-`wayback_target` logic; `MarkSkipped`/`MarkExtracted`/`RecordFailure`/`PersistOCRPath` UPDATE `staged_wayback_documents` + write `crawl_errors`.

- [ ] **Step 1–4: TDD** — the moved wayback extract tests are the spec; make `WaybackSource` satisfy them. Run `go test ./internal/worker/extract/ -run Wayback` (or the moved test names). Expected: all previously-green wayback extract tests pass via the adapter.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor(extract): wayback as a StagedDocSource adapter (behavior + tests preserved)"
```

---

### Task 4: Shared report-URL download (regional/foreign)

**Files:**
- Create: `internal/worker/extract/download.go`
- Test: `internal/worker/extract/download_test.go`

**Interfaces:**
- Produces: `DownloadReportURL(ctx, client *http.Client, rawURL, storeDir, iso2 string) (localPath, digest string, err error)` — rejects non-http(s) schemes; GETs the URL; writes bytes to `storeDir/<iso2>/<sha256>.pdf`; returns the path + sha256 hex. Errors are typed (callers map to `download_status='failed'`).

- [ ] **Step 1: Write the failing test** (httptest server serving PDF bytes; assert file written + digest = sha256; assert `javascript:`/`ftp:` rejected).

```go
// internal/worker/extract/download_test.go — table test: 200 PDF → file+digest;
// non-http scheme → error; 404 → error. Use httptest.NewServer + t.TempDir().
```

- [ ] **Step 2–4:** implement `DownloadReportURL` (scheme allow-list `http`/`https`; `sha256.Sum256`; `os.MkdirAll`; atomic temp+rename via the existing `internal/atomicfile` package if present). Run the test → PASS.

- [ ] **Step 5: Commit** `git commit -m "feat(extract): http(s) report-URL downloader with sha256 digest"`

---

### Task 5: Regional adapter

**Files:**
- Create: `internal/worker/extract/regional_source.go`
- Test: `internal/worker/extract/regional_source_test.go`

**Interfaces:**
- Produces: `type RegionalSource struct{ HTTP *http.Client }` implementing `StagedDocSource`: `PendingDocs` SELECT on `staged_regional_documents` JOIN countries (report_url present, download/extraction status needs-work, attempts<3, ORDER BY priority; map `ArchivedURL=report_url`, `OriginalURL`, `CrawlJobID`, `WaybackTarget=""`); `EnsureDownloaded` calls `DownloadReportURL` then UPDATEs `download_status`/`local_file_path`/`digest`; `ResolveSource` credits the regional body as a source (look-up-or-create on `UNIQUE(canonical_url, source_type)` keyed off the body's known domain + `body_code`); `MarkSkipped/Extracted/RecordFailure/PersistOCRPath` UPDATE `staged_regional_documents`.

- [ ] **Step 1: Write failing tests** (seed a `staged_regional_documents` row + country; assert `PendingDocs` returns it with report_url→ArchivedURL; assert `ResolveSource` creates/returns a regional-body source row; assert `MarkExtracted` sets status+event_id). Use an in-memory DB with `migrations.Apply`.

- [ ] **Step 2–4:** implement; run `go test ./internal/worker/extract/ -run Regional` → PASS.

- [ ] **Step 5: Commit** `git commit -m "feat(extract): regional StagedDocSource adapter"`

---

### Task 6: Foreign adapter

**Files:**
- Create: `internal/worker/extract/foreign_source.go`
- Test: `internal/worker/extract/foreign_source_test.go`

**Interfaces:**
- Produces: `type ForeignSource struct{ HTTP *http.Client }` implementing `StagedDocSource` over `staged_foreign_documents` (authority ntsb/bea/atsb), mirroring Task 5 but: `ResolveSource` credits the foreign authority (NTSB/BEA/ATSB) as the source; `PendingDocs` JOIN countries; `EnsureDownloaded` via `DownloadReportURL`.

- [ ] **Step 1–4: TDD** as Task 5 (seed `staged_foreign_documents`; assert PendingDocs/ResolveSource/Mark*). Run `go test ./internal/worker/extract/ -run Foreign` → PASS.

- [ ] **Step 5: Commit** `git commit -m "feat(extract): foreign StagedDocSource adapter"`

---

### Task 7: Unified `process-extract` command + deprecated alias

**Files:**
- Modify: `internal/app/app.go`
- Modify: `control-plane/README.md`
- Test: `internal/app/app_extract_test.go`

**Interfaces:**
- Consumes: `extract.ProcessExtractPending` + the three adapters + `wayback.NewHTTPOCRClient`/`NewHTTPLLMClient`.
- Produces: `process-extract` command (flags `--db --limit --store-dir --ocr-endpoint --llm-endpoint --llm-model --max-input-chars`) building all three adapters and calling `extract.ProcessExtractPending(..., extract.WaybackSource{}, extract.RegionalSource{HTTP:hc}, extract.ForeignSource{HTTP:hc})`. `process-wayback-extract` stays as a deprecated alias that prints a one-line deprecation to stderr and runs `process-extract` with only `WaybackSource{}`.

- [ ] **Step 1: Write failing test** — `app.Run` with `["process-extract","--db",tmp]` on a migrated+seeded DB returns exit 0 and prints `extracted=… skipped=… failed=…`; `["process-wayback-extract", ...]` still works (alias). Mirror the existing app test pattern (`internal/app/app_regional_test.go`).

- [ ] **Step 2–4:** wire the command + alias; update usage strings (add `process-extract`) and README (replace the `process-wayback-extract` section with a `process-extract` section covering all three sources, note the alias). Run `go test ./internal/app/...` → PASS.

- [ ] **Step 5: Commit** `git commit -m "feat(extract): process-extract command (all sources) + process-wayback-extract alias"`

---

### Task 8: Final integration check

- [ ] **Step 1:** `cd control-plane && go build ./... && go vet ./... && go test ./...` — ALL green (esp. the moved wayback extract tests + new adapter/migration/download/app tests).
- [ ] **Step 2:** `gofmt -l .` — empty.
- [ ] **Step 3:** Manual smoke (optional, documented): `go run ./cmd/aviation-coverage migrate --db /tmp/x.db && ... seed ... && ... process-extract --db /tmp/x.db --limit 1` against a row with a real report_url (or skip if no OCR endpoint locally — note it).
- [ ] **Step 4: Commit** any fmt/cleanup.

---

## Self-Review

**Spec coverage:** full unification (Tasks 2/3 core+wayback adapter) ✅; regional adapter (5) ✅; foreign adapter (6) ✅; migration 009 download/extraction columns (1) ✅; download report_url http(s)-only (4) ✅; unified `process-extract` + deprecated alias (7) ✅; per-source `ResolveSource` credit (3/5/6) ✅; error_type fix I1 (2, in `RecordFailure`) ✅; regression net = moved wayback tests stay green (2/3) ✅; CI-is-CodeQL-only → local `go test` every task ✅.

**Placeholder scan:** no TBD/TODO; code given for the discrete new pieces (migration, interface, download, adapters' SELECT/ResolveSource shapes, command). The core-move tasks (2/3) give precise port-instructions + the target interface rather than reproducing the existing ~120-line functions verbatim — appropriate for a refactor where the implementer reads the source; flagged that 2+3 may land as one commit.

**Type consistency:** `ExtractDoc`/`ExtractStats`/`ExtractedEvent`/`OCRClient`/`LLMClient`/`StagedDocSource` defined once in `extract` (source.go), consumed by core (2), adapters (3/5/6), command (7). `ProcessExtractPending(..., sources ...StagedDocSource)` signature consistent across core + command. `DownloadReportURL(...) (localPath, digest string, err error)` consistent across download (4) + adapters (5/6).

**Risks:** Task 2/3 (core move + wayback adapter) is the highest-risk — the moved wayback tests are the safety net and MUST stay green; if package-cycle issues arise between `wayback` (client constructors) and `extract`, keep the HTTP client constructors in `wayback` returning structurally-compatible types (Go implicit interfaces avoid the cycle), or move the constructors too. Migration-number/app.go conflicts with the active cloud-agent — rebase before merge.
