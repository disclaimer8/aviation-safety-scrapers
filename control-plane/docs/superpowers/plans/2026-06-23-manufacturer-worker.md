# Manufacturer Discovery Worker (Worker 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover Airbus Safety First magazine issues and stage them into a new `staged_manufacturer_documents` table for later OCR/extract — a standalone (global, not per-country) discovery worker mirroring the regional worker.

**Architecture:** New `internal/worker/manufacturer/` package (a simplified clone of `internal/worker/regional/`): a `Client` fetches the Safety First listing (live or `--source-file`), `parse.go` turns it into records, `stage.go` idempotently upserts them, `runner.go` orchestrates, and a `process-manufacturer` command runs it. One global source — no `crawl_jobs`.

**Tech Stack:** Go 1.24+, SQLite, the same HTML parser the regional worker uses (`internal/worker/regional/parsehtml.go` — check whether it's `golang.org/x/net/html` or `goquery` and match it).

## Global Constraints

- Repo CI runs **CodeQL only — no `go test`**. Validate each task with `go build ./... && go vet ./... && go test ./...` locally (from `control-plane/`).
- Migration number: use **009**, but a concurrent cloud-agent (Worker 4) is also taking 009 — **renumber to the next free number at rebase** if Worker 4 lands first (like PR #9's 006→008). Verify `ls internal/migrations/sql/` before writing.
- `staged_manufacturer_documents` is a `STRICT` table — columns `TEXT`/`INTEGER` only.
- URL handling: allow `http`/`https` schemes only on resolved/probed URLs (CodeQL), mirroring `internal/worker/regional/parsehtml.go`.
- Global source: **no `country_id` / `crawl_job_id`** on the table; the worker is NOT driven by per-country `crawl_jobs`.
- MVP source = **Airbus Safety First only** (statistical summaries deferred).
- Migration entrypoint is `migrations.Apply(ctx, db)`; current migration count is **8**.
- Branch `feat/manufacturer-worker` off fresh `origin/main`. Commit after each task. A concurrent cloud-agent is active — rebase before merge; expect migration-number + `app.go`-command conflicts.

## File Structure

- `internal/migrations/sql/009_manufacturer.sql` — the staging table.
- `internal/worker/manufacturer/record.go` — `ManufacturerRecord` struct.
- `internal/worker/manufacturer/parse.go` — listing HTML → `[]ManufacturerRecord` (http(s)-only).
- `internal/worker/manufacturer/safetyfirst.go` — `Client` (live fetch + `--source-file`) + next-issue probe.
- `internal/worker/manufacturer/stage.go` — idempotent upsert.
- `internal/worker/manufacturer/runner.go` — `ProcessManufacturer`.
- `internal/worker/manufacturer/testdata/safetyfirst_listing.html` — saved real fixture.
- `internal/app/app.go` — `process-manufacturer` command.
- `internal/migrations/migrations_test.go` — count guard 8→9.

---

### Task 1: Migration 009 — `staged_manufacturer_documents`

**Files:**
- Create: `internal/migrations/sql/009_manufacturer.sql`
- Create: `internal/migrations/migrations_manufacturer_test.go`
- Modify: `internal/migrations/migrations_test.go` (count 8→9)

**Interfaces:**
- Produces: table `staged_manufacturer_documents` with the columns in the spec (manufacturer, publication, issue_ref, title, publication_date, original_url, report_url, mimetype, download/extraction state, event_id, created_at; `UNIQUE(publication, issue_ref)`; STRICT).

- [ ] **Step 1: Verify migration number free** — `ls internal/migrations/sql/ | tail -3` → highest `008_foreign`. Use `009` (renumber later if Worker 4's 009 merged first).

- [ ] **Step 2: Write the failing test**

```go
// internal/migrations/migrations_manufacturer_test.go
package migrations

import (
	"context"
	"database/sql"
	"testing"
	_ "modernc.org/sqlite"
)

func TestMigration009ManufacturerTable(t *testing.T) {
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil { t.Fatal(err) }
	defer db.Close()
	if err := Apply(context.Background(), db); err != nil { t.Fatal(err) }
	rows, err := db.Query("SELECT name FROM pragma_table_info('staged_manufacturer_documents')")
	if err != nil { t.Fatal(err) }
	defer rows.Close()
	got := map[string]bool{}
	for rows.Next() { var n string; if err := rows.Scan(&n); err != nil { t.Fatal(err) }; got[n] = true }
	for _, c := range []string{"manufacturer","publication","issue_ref","title","publication_date",
		"original_url","report_url","download_status","extraction_status","event_id"} {
		if !got[c] { t.Errorf("missing column %s", c) }
	}
}
```

> Confirm `Apply` signature is `Apply(ctx, db)` (it is, per migrations.go); match the call style in the existing `migrations_test.go`.

- [ ] **Step 3: Run → FAIL** `go test ./internal/migrations/ -run TestMigration009 -v`

- [ ] **Step 4: Write the migration** (copy the `CREATE TABLE staged_manufacturer_documents (...) STRICT;` from the spec verbatim into `009_manufacturer.sql`).

- [ ] **Step 5: Bump count guard** — in `migrations_test.go` change `!= 8` / `want 8` to `9`.

- [ ] **Step 6: Run → PASS + commit**

```bash
go test ./internal/migrations/...
git add internal/migrations/ && git commit -m "feat(manufacturer): migration 009 — staged_manufacturer_documents"
```

---

### Task 2: `record.go` + `parse.go` (fixture-driven)

**Files:**
- Create: `internal/worker/manufacturer/record.go`, `parse.go`
- Create: `internal/worker/manufacturer/testdata/safetyfirst_listing.html`
- Test: `internal/worker/manufacturer/parse_test.go`

**Interfaces:**
- Produces:
  ```go
  type ManufacturerRecord struct {
      IssueRef        string // e.g. "41"
      Title           string
      PublicationDate string // ISO yyyy-mm-dd or ""
      OriginalURL     string // issue/landing page (absolute)
      ReportURL       string // PDF URL (absolute) or ""
  }
  func ParseSafetyFirstListing(html []byte, baseURL string) ([]ManufacturerRecord, error)
  ```
  Absolute-URL rewrite against `baseURL`; **drop non-http(s)** resolved URLs; skip nav/external/empty-title anchors.

- [ ] **Step 1: Capture the real fixture** — fetch the live listing and save it:
  `curl -sL https://safetyfirst.airbus.com/magazine/ -o internal/worker/manufacturer/testdata/safetyfirst_listing.html`
  Inspect it to find the issue-link structure (issue number, title, PDF/landing href, date). (Per research the magazine is WordPress/SSR with a sitemap; the listing page lists issues. If the listing is JS-rendered and the saved HTML lacks issue links, use the **sitemap** `https://safetyfirst.airbus.com/sitemap.xml` (or the magazine sitemap) as the fixture+parse target instead — note which you used.)

- [ ] **Step 2: Write the failing test** asserting exact records from the fixture:

```go
// internal/worker/manufacturer/parse_test.go
package manufacturer

import ( "os"; "testing" )

func TestParseSafetyFirstListing(t *testing.T) {
	html, err := os.ReadFile("testdata/safetyfirst_listing.html")
	if err != nil { t.Fatal(err) }
	recs, err := ParseSafetyFirstListing(html, "https://safetyfirst.airbus.com/magazine/")
	if err != nil { t.Fatal(err) }
	if len(recs) == 0 { t.Fatal("no records parsed from fixture") }
	for _, r := range recs {
		if r.IssueRef == "" || r.Title == "" || r.OriginalURL == "" {
			t.Errorf("incomplete record: %+v", r)
		}
		if !(len(r.OriginalURL) >= 8 && (r.OriginalURL[:7] == "http://" || r.OriginalURL[:8] == "https://")) {
			t.Errorf("non-absolute/non-http url: %s", r.OriginalURL)
		}
	}
	// Pin the highest known issue from the fixture (fill in the real number after capture):
	// e.g. assert recs contains IssueRef "41".
}
```

> The exact assertions (issue count, a specific issue number/title) must be filled from the captured fixture — this is fixture-derived, not a placeholder. Pin at least one concrete issue (number + title substring) so the parser is genuinely verified.

- [ ] **Step 3: Run → FAIL** `go test ./internal/worker/manufacturer/ -run TestParse -v`

- [ ] **Step 4: Implement** `record.go` + `ParseSafetyFirstListing` using the SAME HTML parser as `internal/worker/regional/parsehtml.go` (read it; reuse its helpers/scheme-guard). Derive selectors from the fixture.

- [ ] **Step 5: Run → PASS + commit** `git commit -m "feat(manufacturer): Safety First listing parser + record"`

---

### Task 3: `safetyfirst.go` Client (fetch + source-file + next-issue probe)

**Files:**
- Create: `internal/worker/manufacturer/safetyfirst.go`
- Test: `internal/worker/manufacturer/safetyfirst_test.go`

**Interfaces:**
- Consumes: `ParseSafetyFirstListing` (Task 2).
- Produces:
  ```go
  type Client struct { HTTP *http.Client; SourceFile string; ListingURL string }
  func NewClient(timeout time.Duration, sourceFile string) *Client
  func (c *Client) Discover(ctx context.Context) ([]ManufacturerRecord, error)  // source-file if set, else GET ListingURL; parse
  func (c *Client) ProbeNextIssue(ctx context.Context, highestKnown int) (ManufacturerRecord, bool, error)  // HEAD/GET safety_first_<n+1>.pdf; returns the record if it exists (200, application/pdf)
  ```
  `ListingURL` default `https://safetyfirst.airbus.com/magazine/` (or the sitemap chosen in Task 2). The S3 PDF path pattern for `ProbeNextIssue` — **verify the exact current path during implementation** (research found `safety_first_<N>.pdf` on an S3 bucket); if unverifiable, ship `Discover` only and make `ProbeNextIssue` return `(_, false, nil)` with a `// TODO verify path` removed — i.e. omit the probe rather than hardcode a wrong path.

- [ ] **Step 1: Write failing test** — `Discover` with `SourceFile` set to the testdata fixture returns the parsed records (no network); `ProbeNextIssue` against an `httptest` server returning 200 application/pdf for `safety_first_42.pdf` returns the record, and 404 → `(_, false, nil)`.

- [ ] **Step 2–4: implement + run** `go test ./internal/worker/manufacturer/ -run "Discover|Probe"` → PASS. (http(s)-scheme guard on probe URL.)

- [ ] **Step 5: commit** `git commit -m "feat(manufacturer): Safety First client (discover + next-issue probe)"`

---

### Task 4: `stage.go` idempotent upsert

**Files:**
- Create: `internal/worker/manufacturer/stage.go`
- Test: `internal/worker/manufacturer/stage_test.go`

**Interfaces:**
- Produces: `func StageRecords(ctx, db *sql.DB, manufacturer, publication string, recs []ManufacturerRecord) (staged int, err error)` — one tx, prepared `INSERT INTO staged_manufacturer_documents (manufacturer, publication, issue_ref, title, publication_date, original_url, report_url) VALUES (?,?,?,?,?,?,?) ON CONFLICT(publication, issue_ref) DO NOTHING`; `nullIfEmpty` for date/report_url; returns RowsAffected sum. Mirror `internal/worker/regional/stage.go` (including its `nullIfEmpty` helper).

- [ ] **Step 1: Write failing test** (in-memory DB via `migrations.Apply`; stage 2 records → staged=2; stage same 2 again → staged=0; distinct issue_ref → distinct rows). 

- [ ] **Step 2–4: implement + run** `go test ./internal/worker/manufacturer/ -run Stage` → PASS.

- [ ] **Step 5: commit** `git commit -m "feat(manufacturer): idempotent staging into staged_manufacturer_documents"`

---

### Task 5: `runner.go` — `ProcessManufacturer`

**Files:**
- Create: `internal/worker/manufacturer/runner.go`
- Test: `internal/worker/manufacturer/runner_test.go`

**Interfaces:**
- Consumes: `Client.Discover`/`ProbeNextIssue` (Task 3), `StageRecords` (Task 4).
- Produces:
  ```go
  type Result struct { Found, Staged, Errors int }
  type Discoverer interface { Discover(ctx) ([]ManufacturerRecord, error); ProbeNextIssue(ctx, int) (ManufacturerRecord, bool, error) }
  func ProcessManufacturer(ctx, db *sql.DB, d Discoverer) (Result, error)
  ```
  Discover issues; merge in a probed next-issue if present; `StageRecords("airbus","safety_first", recs)`; return `{Found:len(recs), Staged:staged, Errors:errs}`. A discover error returns the error; per-issue oddities are counted in `Errors`, the batch continues.

- [ ] **Step 1: Write failing test** — fake `Discoverer` returning 3 records (+1 from probe) and an in-memory DB; assert `Found=4, Staged=4, Errors=0`; re-run → `Staged=0`.

- [ ] **Step 2–4: implement + run** `go test ./internal/worker/manufacturer/` → PASS (whole package).

- [ ] **Step 5: commit** `git commit -m "feat(manufacturer): ProcessManufacturer runner"`

---

### Task 6: `process-manufacturer` command + README

**Files:**
- Modify: `internal/app/app.go`
- Modify: `control-plane/README.md`
- Test: `internal/app/app_manufacturer_test.go`

**Interfaces:**
- Consumes: `manufacturer.NewClient`, `manufacturer.ProcessManufacturer`.
- Produces: `process-manufacturer` command (flags `--db` required, `--source-file`, `--timeout` default 30s). Opens DB, builds client, runs `ProcessManufacturer`, prints `found=… staged=… errors=…`. Add to usage strings + the command switch + README (a `### process-manufacturer` section: global Airbus Safety First discovery, `--source-file` for out-of-band listing, attribution/reprint note).

- [ ] **Step 1: Write failing test** — `app.Run(ctx, ["process-manufacturer","--db",tmp,"--source-file",fixturePath], …)` on a migrated DB returns exit 0 and prints `staged=`. Mirror `internal/app/app_regional_test.go` exactly (same migrate-then-run setup). (Point `--source-file` at a copy of the Task 2 fixture so no network.)

- [ ] **Step 2–4: implement + run** `go test ./internal/app/ -run Manufacturer` → PASS; `gofmt -l .` empty.

- [ ] **Step 5: Full check + commit**

```bash
go build ./... && go vet ./... && go test ./...
git add internal/app/ control-plane/README.md && git commit -m "feat(manufacturer): process-manufacturer command + README"
```

---

## Self-Review

**Spec coverage:** staging table (Task 1) ✅; Safety First parser (2) ✅; client discover + next-issue probe + source-file (3) ✅; idempotent staging (4) ✅; runner result {found,staged,errors} (5) ✅; `process-manufacturer` command + README (6) ✅; global/standalone (no crawl_jobs/country) — table has neither (1), runner takes no country (5) ✅; http(s)-only (2/3) ✅; deferred stats sources & extract adapter — out of scope, noted ✅; renumber-at-rebase + CodeQL-only-CI + concurrent-agent — Global Constraints ✅.

**Placeholder scan:** the only "fill-in" is the fixture-derived parser assertions (Task 2) and the S3 probe-path verification (Task 3) — both are genuine *capture-the-real-artifact* steps with explicit instructions and a fallback (sitemap; omit-probe-if-unverifiable), not deferred logic. Everything else has concrete code/SQL.

**Type consistency:** `ManufacturerRecord{IssueRef,Title,PublicationDate,OriginalURL,ReportURL}` consistent across Tasks 2/3/4/5. `StageRecords(ctx,db,manufacturer,publication,recs)` consistent (4/5). `ProcessManufacturer(ctx,db,Discoverer) (Result,error)` + `Discoverer` interface consistent (5/6). Migration entrypoint `Apply(ctx,db)` consistent (1, tests). `nullIfEmpty` reused from regional pattern (4).
