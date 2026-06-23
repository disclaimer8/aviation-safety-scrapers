# Foreign-Search Worker (Discovery + Staging) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `process-foreign-search` command that drains `ntsb_foreign_search` / `bea_foreign_search` / `atsb_search` crawl jobs: route each by delegate authority, query that authority's accident records for the occurrence country, and stage the discovered records idempotently.

**Architecture:** A new `internal/worker/foreignsearch` package with a per-authority `AuthorityClient` (the only network/file seam) and pure offline-tested parsers (`parseNTSB`/`parseBEA`/`parseATSB`). NTSB CAROL + BEA fetch live; ATSB is out-of-band via `--source-file` (Akamai). A migration adds `staged_foreign_documents`. The CLI wiring, routing, staging, and runner mirror the merged Wayback worker (`internal/worker/wayback`).

**Tech Stack:** Go 1.24+, `database/sql` over SQLite, embedded SQL migrations, `net/http`, `encoding/json`, `golang.org/x/net/html` IF already vendored else stdlib string parsing, standard `testing`.

## Global Constraints

- **Work in the worktree:** `/Users/denyskolomiiets/ass-worktrees/foreign-search`. All paths below are relative to `control-plane/` inside it. Run all `git`/`go` from there. Do NOT touch the main checkout at `~/aviation-safety-scrapers`.
- Module path: `github.com/denyskolomiiets/aviation-safety-scrapers/control-plane`. Go 1.24+. **No new third-party dependencies** (stdlib only — for HTML, use stdlib string/regex scanning, not a new parser dep).
- Migrations immutable: `internal/migrations/migrations.go` enforces filename + SHA-256 checksum drift detection. **Never edit `001`–`005`.** New schema only in `internal/migrations/sql/006_foreign.sql`. The migration-count guard in `migrations_test.go` currently asserts `5`; bump it to `6`.
- SQLite tables use `STRICT`.
- The only network/file access is behind `AuthorityClient`; all package logic is unit-tested with a `fixtureClient` (no real network in tests).
- Determinism: job selection orders `priority_score DESC, iso2 ASC`.
- Idempotency: `staged_foreign_documents` has `UNIQUE(authority, foreign_ref)`; staging uses `ON CONFLICT … DO NOTHING`.
- Exit codes (from `internal/app/app.go`): `exitOK=0`, `exitFailure=1`, `exitUsage=2`.
- Test DB helper: `database.Open(t.TempDir()+"/coverage.db")`, then `migrations.Apply(ctx, db)` and (where countries are needed) `seed.Apply(ctx, db)`.
- **Reference implementation:** `internal/worker/wayback/` (merged). Mirror its `runner.go` (RunJob/ProcessPending/finalize/recordError, stale-running re-selection, finalize-failed on DB error) and `internal/app/app.go`'s `runProcessWayback` exactly, adapting names.

---

## File Structure

- `internal/migrations/sql/006_foreign.sql` — **new.** `staged_foreign_documents`.
- `internal/worker/foreignsearch/record.go` — **new.** `ForeignRecord`, `AuthorityClient`.
- `internal/worker/foreignsearch/stage.go` — **new.** `StageRecords`.
- `internal/worker/foreignsearch/ntsb.go` / `bea.go` / `atsb.go` — **new.** client + pure parser per authority.
- `internal/worker/foreignsearch/runner.go` — **new.** `Job`, `RunJob`, `ProcessPending`, routing.
- `internal/worker/foreignsearch/*_test.go` + `testdata/` — **new.** tests, `fixtureClient`, captured fixtures.
- `internal/app/app.go` — **modify.** Wire `process-foreign-search`.
- `README.md` — **modify.** Document the command + ATSB `--source-file`.

---

## Task 1: Migration `006_foreign.sql`

**Files:**
- Create: `internal/migrations/sql/006_foreign.sql`
- Modify: `internal/migrations/migrations_test.go` (count guard 5→6)
- Test: `internal/migrations/migrations_foreign_test.go`

**Interfaces:**
- Produces: table `staged_foreign_documents` with `authority` CHECK in {ntsb,bea,atsb} and `UNIQUE(authority, foreign_ref)`.

- [ ] **Step 1: Write the failing test**

Create `internal/migrations/migrations_foreign_test.go`:

```go
package migrations

import (
	"context"
	"testing"
)

func TestMigration006ForeignSchema(t *testing.T) {
	db := applyTestSchema(t)
	ctx := context.Background()

	// Need a country + source + crawl_job to satisfy FKs.
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score)
		VALUES ('XF','XFF','Test F','Test','allowed','delegated_to_foreign_authority',3,2)`); err != nil {
		t.Fatal(err)
	}
	var cid int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XF'`).Scan(&cid)
	res, err := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES ('t','https://t/','https://t/','official_foreign_accredited_rep',2)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status)
		VALUES (?,?, 'ntsb_foreign_search','running')`, srcID, cid)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()

	ins := `INSERT INTO staged_foreign_documents
		(crawl_job_id, country_id, authority, foreign_ref, title, original_url)
		VALUES (?,?,?,?,?,?)`
	if _, err := db.ExecContext(ctx, ins, jid, cid, "ntsb", "CEN20LA001", "Accident A", "https://ntsb/CEN20LA001"); err != nil {
		t.Fatalf("insert staged foreign doc: %v", err)
	}
	// Same (authority, foreign_ref) must conflict.
	if _, err := db.ExecContext(ctx, ins, jid, cid, "ntsb", "CEN20LA001", "dup", "https://ntsb/x"); err == nil {
		t.Fatal("expected UNIQUE(authority, foreign_ref) violation")
	}
	// A bad authority value must violate the CHECK.
	if _, err := db.ExecContext(ctx, ins, jid, cid, "faa", "X1", "t", "https://x"); err == nil {
		t.Fatal("expected authority CHECK violation for 'faa'")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/migrations/ -run TestMigration006Foreign -v`
Expected: FAIL — no such table `staged_foreign_documents`.

- [ ] **Step 3: Create the migration**

Create `internal/migrations/sql/006_foreign.sql`:

```sql
-- 006_foreign.sql
-- Staging table for the foreign-search worker: accident records discovered at a
-- foreign accredited-representative authority (NTSB/BEA/ATSB) for a delegated
-- country.
CREATE TABLE staged_foreign_documents (
  id INTEGER PRIMARY KEY,
  crawl_job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  authority TEXT NOT NULL CHECK(authority IN ('ntsb', 'bea', 'atsb')),
  foreign_ref TEXT NOT NULL,
  title TEXT NOT NULL,
  occurrence_date TEXT,
  original_url TEXT NOT NULL,
  report_url TEXT,
  mimetype TEXT,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  UNIQUE(authority, foreign_ref)
) STRICT;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/migrations/ -run TestMigration006Foreign -v`
Expected: PASS.

- [ ] **Step 5: Bump the migration-count guard**

In `internal/migrations/migrations_test.go` find the assertion that the embedded migration count equals `5` and change it to `6`.

- [ ] **Step 6: Run the full migrations suite**

Run: `cd control-plane && go test ./internal/migrations/...`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add internal/migrations/sql/006_foreign.sql internal/migrations/migrations_foreign_test.go internal/migrations/migrations_test.go
git commit -m "feat(control-plane): migration 006 staged_foreign_documents"
```

---

## Task 2: `ForeignRecord` + `AuthorityClient` + `fixtureClient`

**Files:**
- Create: `internal/worker/foreignsearch/record.go`
- Test: `internal/worker/foreignsearch/record_test.go`

**Interfaces:**
- Produces:
  - `type ForeignRecord struct { ForeignRef, Title, OccurrenceDate, OriginalURL, ReportURL, Mimetype string }`
  - `type AuthorityClient interface { Search(ctx context.Context, countryISO2 string) ([]ForeignRecord, error) }`
  - A `fixtureClient` test helper (in `record_test.go`) implementing `AuthorityClient`: `type fixtureClient struct { Records []ForeignRecord; Err error }`.

- [ ] **Step 1: Write the failing test**

Create `internal/worker/foreignsearch/record_test.go`:

```go
package foreignsearch

import (
	"context"
	"testing"
)

// fixtureClient is the offline AuthorityClient used across the package's tests.
type fixtureClient struct {
	Records []ForeignRecord
	Err     error
}

func (f *fixtureClient) Search(ctx context.Context, countryISO2 string) ([]ForeignRecord, error) {
	if f.Err != nil {
		return nil, f.Err
	}
	return f.Records, nil
}

var _ AuthorityClient = (*fixtureClient)(nil)

func TestForeignRecordFields(t *testing.T) {
	r := ForeignRecord{ForeignRef: "CEN20LA001", Title: "A", OccurrenceDate: "2020-01-02",
		OriginalURL: "https://ntsb/x", ReportURL: "https://ntsb/x.pdf", Mimetype: "application/pdf"}
	if r.ForeignRef == "" || r.Title == "" || r.OriginalURL == "" {
		t.Fatal("ForeignRecord required fields must be settable")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run TestForeignRecordFields -v`
Expected: FAIL — package/types undefined.

- [ ] **Step 3: Implement `record.go`**

Create `internal/worker/foreignsearch/record.go`:

```go
// Package foreignsearch is the foreign-investigation acquisition worker: it
// drains ntsb/bea/atsb_foreign_search crawl jobs by querying the delegated
// authority's accident records for the occurrence country and staging them.
package foreignsearch

import "context"

// ForeignRecord is one accident record discovered at a foreign authority.
type ForeignRecord struct {
	ForeignRef     string // the authority's stable case/record id
	Title          string
	OccurrenceDate string // ISO yyyy-mm-dd when known, else ""
	OriginalURL    string // the human page for the record
	ReportURL      string // direct report/PDF URL when present, else ""
	Mimetype       string // of ReportURL when known, else ""
}

// AuthorityClient queries one foreign authority for accidents in a country.
type AuthorityClient interface {
	Search(ctx context.Context, countryISO2 string) ([]ForeignRecord, error)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run TestForeignRecordFields -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/worker/foreignsearch/record.go internal/worker/foreignsearch/record_test.go
git commit -m "feat(control-plane): foreignsearch ForeignRecord + AuthorityClient"
```

---

## Task 3: `StageRecords`

**Files:**
- Create: `internal/worker/foreignsearch/stage.go`
- Test: `internal/worker/foreignsearch/stage_test.go`

**Interfaces:**
- Consumes: `*sql.DB`, `ForeignRecord`, `staged_foreign_documents`.
- Produces: `func StageRecords(ctx context.Context, db *sql.DB, jobID, countryID int64, authority string, recs []ForeignRecord) (staged int, err error)` — inserts each record with `ON CONFLICT(authority, foreign_ref) DO NOTHING`; returns count newly inserted.
- Also defines a shared test helper `foreignTestDB(t)` (migrate-only DB) and `stageFixtureJob(t, ctx, db)` (country XF + source + running ntsb job → countryID, jobID) in `stage_test.go`, reused by later tasks.

- [ ] **Step 1: Write the failing test**

Create `internal/worker/foreignsearch/stage_test.go`:

```go
package foreignsearch

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func foreignTestDB(t *testing.T) (context.Context, *sql.DB) {
	t.Helper()
	ctx := context.Background()
	db, err := database.Open(t.TempDir() + "/coverage.db")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := migrations.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	return ctx, db
}

func stageFixtureJob(t *testing.T, ctx context.Context, db *sql.DB) (int64, int64) {
	t.Helper()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score, delegate_iso2)
		VALUES ('XF','XFF','Test F','Test','allowed','delegated_to_foreign_authority',3,2,'US')`); err != nil {
		t.Fatal(err)
	}
	var cid int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XF'`).Scan(&cid)
	res, err := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES ('fs','https://fs/','https://fs/','official_foreign_accredited_rep',2)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status)
		VALUES (?,?, 'ntsb_foreign_search','running')`, srcID, cid)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()
	return cid, jid
}

func TestStageRecordsDedups(t *testing.T) {
	ctx, db := foreignTestDB(t)
	cid, jid := stageFixtureJob(t, ctx, db)

	recs := []ForeignRecord{
		{ForeignRef: "CEN20LA001", Title: "A", OriginalURL: "https://ntsb/1", ReportURL: "https://ntsb/1.pdf", Mimetype: "application/pdf", OccurrenceDate: "2020-01-02"},
		{ForeignRef: "CEN20LA002", Title: "B", OriginalURL: "https://ntsb/2"},
	}
	n, err := StageRecords(ctx, db, jid, cid, "ntsb", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("staged = %d, want 2", n)
	}
	n2, err := StageRecords(ctx, db, jid, cid, "ntsb", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n2 != 0 {
		t.Fatalf("re-stage = %d, want 0 (dedup)", n2)
	}
	var total int
	db.QueryRowContext(ctx, `SELECT COUNT(*) FROM staged_foreign_documents`).Scan(&total)
	if total != 2 {
		t.Fatalf("total = %d, want 2", total)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run TestStageRecords -v`
Expected: FAIL — `StageRecords` undefined.

- [ ] **Step 3: Implement `stage.go`**

Create `internal/worker/foreignsearch/stage.go`:

```go
package foreignsearch

import (
	"context"
	"database/sql"
	"fmt"
)

// StageRecords inserts each record into staged_foreign_documents, skipping any
// (authority, foreign_ref) already present. Returns the count newly inserted.
func StageRecords(ctx context.Context, db *sql.DB, jobID, countryID int64, authority string, recs []ForeignRecord) (int, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("foreignsearch: stage begin tx: %w", err)
	}
	defer tx.Rollback()

	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO staged_foreign_documents
			(crawl_job_id, country_id, authority, foreign_ref, title, occurrence_date, original_url, report_url, mimetype)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(authority, foreign_ref) DO NOTHING`)
	if err != nil {
		return 0, fmt.Errorf("foreignsearch: stage prepare: %w", err)
	}
	defer stmt.Close()

	staged := 0
	for _, r := range recs {
		occ := nullIfEmpty(r.OccurrenceDate)
		rep := nullIfEmpty(r.ReportURL)
		mime := nullIfEmpty(r.Mimetype)
		res, err := stmt.ExecContext(ctx, jobID, countryID, authority, r.ForeignRef, r.Title, occ, r.OriginalURL, rep, mime)
		if err != nil {
			return 0, fmt.Errorf("foreignsearch: stage insert %s/%s: %w", authority, r.ForeignRef, err)
		}
		n, _ := res.RowsAffected()
		staged += int(n)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("foreignsearch: stage commit: %w", err)
	}
	return staged, nil
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run TestStageRecords -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/worker/foreignsearch/stage.go internal/worker/foreignsearch/stage_test.go
git commit -m "feat(control-plane): foreignsearch record staging with dedup"
```

---

## Task 4: NTSB client + parser (probe-driven)

**Files:**
- Create: `internal/worker/foreignsearch/ntsb.go`, `internal/worker/foreignsearch/testdata/ntsb_carol.json`
- Test: `internal/worker/foreignsearch/ntsb_test.go`

**Interfaces:**
- Produces:
  - `func parseNTSB(raw []byte) (recs []ForeignRecord, warnings int, err error)` — parses a NTSB CAROL accident-query JSON response into records.
  - `type ntsbClient struct { http *http.Client }` implementing `AuthorityClient`; `func NewNTSBClient(timeout time.Duration) AuthorityClient`.

**This is a probe-and-parse task — the CAROL accident-query format must be captured from the live service, not guessed.**

- [ ] **Step 1: Probe the live CAROL API and capture a real fixture**

The NTSB CAROL public query API is at `https://data.ntsb.gov/carol-main-public/api/Query/Main`. Capture a real accident-query response for a delegated country (e.g. occurrences in the Marshall Islands or the Bahamas). Try a query like:

```bash
curl -s 'https://data.ntsb.gov/carol-main-public/api/Query/Main' \
  -H 'Content-Type: application/json' \
  --data '{"ResultSetSize":10,"ResultSetOffset":0,"QueryGroups":[{"QueryRules":[{"RuleType":"Simple","Values":["Bahamas"],"Columns":["Country"],"Operator":"is","selectedOption":{"FieldName":"Country"}}],"AndOr":"and"}],"AndOr":"and","TargetCollection":"cases","SortColumn":null,"SortDescending":true}' \
  | tee internal/worker/foreignsearch/testdata/ntsb_carol.json | head -c 400
```

Inspect the JSON. If the exact payload/collection differs, adjust until you get a non-empty result set of accident *cases* with, per row: a stable case id (e.g. `NtsbNo`/`Mkey`), a title/synopsis, an event date, and a URL. Save the real response to `testdata/ntsb_carol.json`. **If the live API cannot be reached or refuses every query shape after reasonable attempts, STOP and report BLOCKED with the responses you saw** — do not fabricate a fixture.

- [ ] **Step 2: Write the failing test (against the captured fixture's real fields)**

Create `internal/worker/foreignsearch/ntsb_test.go`. Use the ACTUAL field values present in the fixture you captured (read it first):

```go
package foreignsearch

import (
	"os"
	"testing"
)

func TestParseNTSB(t *testing.T) {
	raw, err := os.ReadFile("testdata/ntsb_carol.json")
	if err != nil {
		t.Fatal(err)
	}
	recs, warnings, err := parseNTSB(raw)
	if err != nil {
		t.Fatalf("parseNTSB: %v", err)
	}
	if len(recs) == 0 {
		t.Fatalf("expected records from the captured CAROL fixture, got 0 (warnings=%d)", warnings)
	}
	for _, r := range recs {
		if r.ForeignRef == "" {
			t.Errorf("record missing ForeignRef: %+v", r)
		}
		if r.OriginalURL == "" {
			t.Errorf("record missing OriginalURL: %+v", r)
		}
		if r.Title == "" {
			t.Errorf("record missing Title: %+v", r)
		}
	}
}

func TestParseNTSBErrorsOnGarbage(t *testing.T) {
	if _, _, err := parseNTSB([]byte("not json")); err == nil {
		t.Fatal("expected error on unparseable body")
	}
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run TestParseNTSB -v`
Expected: FAIL — `parseNTSB` undefined.

- [ ] **Step 4: Implement `ntsb.go` — parser mapped to the captured fixture's real structure**

Create `internal/worker/foreignsearch/ntsb.go`. Unmarshal the CAROL JSON into a struct matching the REAL shape you captured (field names from the fixture), then map each result row to a `ForeignRecord`:
- `ForeignRef` = the row's stable id (e.g. `NtsbNo` or `Mkey` — whichever the fixture provides as the case identifier).
- `Title` = the synopsis/title field; if absent, build from city/make/model.
- `OccurrenceDate` = the event date, normalized to `yyyy-mm-dd` (the fixture's date may be `2020-01-02T00:00:00` — take the date prefix).
- `OriginalURL` = the public case URL, e.g. `https://data.ntsb.gov/carol-main-public/basic-search` deep link or `https://www.ntsb.gov/.../` + id; use the URL pattern the fixture/site actually uses for a case.
- `ReportURL` = a report/PDF URL if the row carries one, else "".
A row missing the stable id is skipped and counted in `warnings`. A non-JSON body returns an error. Keep the function pure (input bytes → records); the network call lives in `ntsbClient.Search`.

Add the live client (its body is exercised only in the live smoke, not unit tests):

```go
type ntsbClient struct{ http *http.Client }

// NewNTSBClient returns an AuthorityClient backed by the live CAROL API.
func NewNTSBClient(timeout time.Duration) AuthorityClient {
	return &ntsbClient{http: &http.Client{Timeout: timeout}}
}

func (c *ntsbClient) Search(ctx context.Context, countryISO2 string) ([]ForeignRecord, error) {
	body, err := c.fetch(ctx, countryISO2) // builds the CAROL query payload by country name and POSTs it
	if err != nil {
		return nil, err
	}
	recs, _, err := parseNTSB(body)
	return recs, err
}
```

Implement `fetch` to map `countryISO2` → country name (a small map for the delegated ISO2s in scope is acceptable; document it) and POST the CAROL query shape you verified in Step 1. Wrap errors with `foreignsearch:`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run TestParseNTSB -v && go vet ./internal/worker/foreignsearch/`
Expected: PASS; vet clean.

- [ ] **Step 6: Commit**

```bash
git add internal/worker/foreignsearch/ntsb.go internal/worker/foreignsearch/ntsb_test.go internal/worker/foreignsearch/testdata/ntsb_carol.json
git commit -m "feat(control-plane): foreignsearch NTSB CAROL client + parser"
```

---

## Task 5: BEA client + parser (probe-driven)

**Files:**
- Create: `internal/worker/foreignsearch/bea.go`, `internal/worker/foreignsearch/testdata/bea_listing.html`
- Test: `internal/worker/foreignsearch/bea_test.go`

**Interfaces:**
- Produces: `func parseBEA(raw []byte) (recs []ForeignRecord, warnings int, err error)`; `type beaClient struct{ http *http.Client }` implementing `AuthorityClient`; `func NewBEAClient(timeout time.Duration) AuthorityClient`.

**Probe-and-parse task — capture a real BEA listing page.**

- [ ] **Step 1: Probe BEA and capture a real fixture**

BEA publishes investigation reports at `https://www.bea.aero/en/investigation-reports/notified-events/` (and a searchable list). Capture a real listing HTML that includes several investigation entries (each with a title, a date, and a link to the report page). For a country-filtered list, find BEA's search/filter URL (inspect the site); if a clean country filter URL isn't available, capture the general notified-events listing and have the parser extract all entries (country filtering can be refined later). Save to `testdata/bea_listing.html`:

```bash
curl -s 'https://www.bea.aero/en/investigation-reports/notified-events/' \
  | tee internal/worker/foreignsearch/testdata/bea_listing.html | head -c 400
```

**If BEA is unreachable after reasonable attempts, STOP and report BLOCKED** with what you saw — do not fabricate.

- [ ] **Step 2: Write the failing test (against the captured fixture)**

Create `internal/worker/foreignsearch/bea_test.go`:

```go
package foreignsearch

import (
	"os"
	"testing"
)

func TestParseBEA(t *testing.T) {
	raw, err := os.ReadFile("testdata/bea_listing.html")
	if err != nil {
		t.Fatal(err)
	}
	recs, _, err := parseBEA(raw)
	if err != nil {
		t.Fatalf("parseBEA: %v", err)
	}
	if len(recs) == 0 {
		t.Fatal("expected records from the captured BEA listing, got 0")
	}
	for _, r := range recs {
		if r.ForeignRef == "" || r.OriginalURL == "" || r.Title == "" {
			t.Errorf("record missing required field: %+v", r)
		}
	}
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run TestParseBEA -v`
Expected: FAIL — `parseBEA` undefined.

- [ ] **Step 4: Implement `bea.go` — HTML extraction with stdlib (no new deps)**

Create `internal/worker/foreignsearch/bea.go`. Parse the listing with stdlib scanning (`strings`/`regexp`) — extract each report entry's link `href` (→ `OriginalURL`), its visible title (→ `Title`), the date if present (→ `OccurrenceDate`, normalized `yyyy-mm-dd`), and derive `ForeignRef` from the report URL's stable slug/path segment. An entry missing a link/title is skipped and counted in `warnings`. An empty/garbage body that yields no entries returns `(nil, n, nil)` with warnings (BEA HTML is never "unparseable" the way JSON is — absence of entries is just zero records). Map relative hrefs to absolute (`https://www.bea.aero` + path). Keep the function pure. Add the live client mirroring `ntsbClient` (fetch the country-or-general listing URL, then `parseBEA`).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run TestParseBEA -v && go vet ./internal/worker/foreignsearch/`
Expected: PASS; vet clean.

- [ ] **Step 6: Commit**

```bash
git add internal/worker/foreignsearch/bea.go internal/worker/foreignsearch/bea_test.go internal/worker/foreignsearch/testdata/bea_listing.html
git commit -m "feat(control-plane): foreignsearch BEA listing client + parser"
```

---

## Task 6: ATSB client + parser (out-of-band `--source-file`)

**Files:**
- Create: `internal/worker/foreignsearch/atsb.go`, `internal/worker/foreignsearch/testdata/atsb_export.json`
- Test: `internal/worker/foreignsearch/atsb_test.go`

**Interfaces:**
- Produces: `func parseATSB(raw []byte) (recs []ForeignRecord, warnings int, err error)`; `type atsbClient struct{ sourceFile string }` implementing `AuthorityClient` by reading `sourceFile` (NOT the network); `func NewATSBClient(sourceFile string) AuthorityClient`.

ATSB sits behind Akamai; the worker never fetches it live. An operator exports a country's investigation list from the mini-PC browser (the ATSB investigations API returns JSON; e.g. the `atsb.gov.au` investigations search backs an XHR JSON endpoint). The worker reads that saved file.

- [ ] **Step 1: Capture / construct the ATSB fixture**

If you can reach the ATSB investigations JSON XHR endpoint from this environment, capture a real country export to `testdata/atsb_export.json`. If Akamai blocks it (expected), construct a SMALL representative fixture from the ATSB investigations JSON schema (an array of investigations, each with an `investigation number`/id, a `title`, an `occurrence date`, and a report URL) — and note in your report that this fixture is schema-representative pending a real operator export. (This is the one place a constructed fixture is acceptable, because the real export is an operator step; keep it minimal and shaped like ATSB's real JSON.)

- [ ] **Step 2: Write the failing test**

Create `internal/worker/foreignsearch/atsb_test.go`:

```go
package foreignsearch

import (
	"context"
	"os"
	"testing"
)

func TestParseATSB(t *testing.T) {
	raw, err := os.ReadFile("testdata/atsb_export.json")
	if err != nil {
		t.Fatal(err)
	}
	recs, _, err := parseATSB(raw)
	if err != nil {
		t.Fatalf("parseATSB: %v", err)
	}
	if len(recs) == 0 {
		t.Fatal("expected records from the ATSB export fixture, got 0")
	}
	for _, r := range recs {
		if r.ForeignRef == "" || r.OriginalURL == "" || r.Title == "" {
			t.Errorf("record missing required field: %+v", r)
		}
	}
}

func TestATSBClientReadsSourceFile(t *testing.T) {
	c := NewATSBClient("testdata/atsb_export.json")
	recs, err := c.Search(context.Background(), "WS")
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) == 0 {
		t.Fatal("ATSB client should parse the source file into records")
	}
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run 'TestParseATSB|TestATSBClient' -v`
Expected: FAIL — `parseATSB`/`NewATSBClient` undefined.

- [ ] **Step 4: Implement `atsb.go`**

Create `internal/worker/foreignsearch/atsb.go`. `parseATSB` unmarshals the ATSB investigations JSON (shape matching `testdata/atsb_export.json`) and maps each to a `ForeignRecord` (id → `ForeignRef`, title → `Title`, occurrence date → `OccurrenceDate` normalized, the investigation page URL → `OriginalURL`, the report PDF URL if present → `ReportURL`). A non-JSON body returns an error; a row missing the id is a counted warning. `atsbClient.Search` reads `sourceFile` (via `os.ReadFile`) and calls `parseATSB`; if `sourceFile == ""` it returns an error `"foreignsearch: atsb requires --source-file"`. Keep `parseATSB` pure.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run 'TestParseATSB|TestATSBClient' -v && go vet ./internal/worker/foreignsearch/`
Expected: PASS; vet clean.

- [ ] **Step 6: Commit**

```bash
git add internal/worker/foreignsearch/atsb.go internal/worker/foreignsearch/atsb_test.go internal/worker/foreignsearch/testdata/atsb_export.json
git commit -m "feat(control-plane): foreignsearch ATSB out-of-band client + parser"
```

---

## Task 7: Routing + `RunJob` + `ProcessPending`

**Files:**
- Create: `internal/worker/foreignsearch/runner.go`
- Test: `internal/worker/foreignsearch/runner_test.go`

**Interfaces:**
- Consumes: `AuthorityClient`, `StageRecords`, `crawl_jobs`, `crawl_errors`, `countries`.
- Produces:
  - `type Job struct { ID, CountryID int64; ISO2, JobType string }`
  - `type Clients struct { NTSB, BEA, ATSB AuthorityClient }` — the per-authority clients (tests pass `fixtureClient`s; the CLI passes the real ones).
  - `func clientFor(c Clients, jobType string) (AuthorityClient, string, bool)` — returns the client + authority code (`ntsb`/`bea`/`atsb`) for a job type, false if unknown.
  - `func RunJob(ctx context.Context, db *sql.DB, c Clients, job Job) error` — resolve ISO2; pick client; `Search`; stage; finalize (`success`/`partial`/`failed` + `stats_json{found,staged,errors}` + `finished_at`); `crawl_errors` on Search error / unknown job type. Returns error only on unexpected DB failure.
  - `func ProcessPending(ctx context.Context, db *sql.DB, c Clients, limit int) (processed int, err error)` — selects pending OR stale-running (`started_at < now-3600000ms`) jobs whose `job_type IN ('ntsb_foreign_search','bea_foreign_search','atsb_search')`, ordered `priority_score DESC, iso2 ASC`, capped by `limit`; marks each running; calls `RunJob`.

Mirror `internal/worker/wayback/runner.go` exactly (finalize/recordError/jobStats helpers, stale-running re-selection, finalize-failed on DB-error early returns).

- [ ] **Step 1: Write the failing test**

Create `internal/worker/foreignsearch/runner_test.go`:

```go
package foreignsearch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"testing"
)

func insertForeignJob(t *testing.T, ctx context.Context, db *sql.DB, iso2, jobType string, priority float64, status string, startedAgoMs int64) (int64, int64) {
	t.Helper()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score, priority_score, delegate_iso2)
		VALUES (?, ?, 'N','R','allowed','delegated_to_foreign_authority',3,2, ?, 'US')`, iso2, iso2+"X", priority); err != nil {
		t.Fatal(err)
	}
	var cid int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2=?`, iso2).Scan(&cid)
	res, _ := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES (?, ?, ?, 'official_foreign_accredited_rep',2)`, "s"+iso2, "https://s/"+iso2, "https://s/"+iso2)
	srcID, _ := res.LastInsertId()
	res, err := db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status) VALUES (?,?,?,?)`, srcID, cid, jobType, status)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()
	if startedAgoMs > 0 {
		db.ExecContext(ctx, `UPDATE crawl_jobs SET started_at = (CAST(unixepoch('subsec')*1000 AS INTEGER) - ?) WHERE id=?`, startedAgoMs, jid)
	}
	return cid, jid
}

func TestRunJobSuccessStages(t *testing.T) {
	ctx, db := foreignTestDB(t)
	_, jid := insertForeignJob(t, ctx, db, "BS", "ntsb_foreign_search", 50, "running", 0)
	var cid int64
	db.QueryRowContext(ctx, `SELECT country_id FROM crawl_jobs WHERE id=?`, jid).Scan(&cid)
	clients := Clients{NTSB: &fixtureClient{Records: []ForeignRecord{
		{ForeignRef: "CEN20LA001", Title: "A", OriginalURL: "https://ntsb/1"},
		{ForeignRef: "CEN20LA002", Title: "B", OriginalURL: "https://ntsb/2"},
	}}}
	if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "BS", JobType: "ntsb_foreign_search"}); err != nil {
		t.Fatal(err)
	}
	var status, stats string
	db.QueryRowContext(ctx, `SELECT status, stats_json FROM crawl_jobs WHERE id=?`, jid).Scan(&status, &stats)
	if status != "success" {
		t.Fatalf("status = %q, want success", status)
	}
	var s struct{ Found, Staged, Errors int }
	json.Unmarshal([]byte(stats), &s)
	if s.Found != 2 || s.Staged != 2 || s.Errors != 0 {
		t.Fatalf("stats = %+v", s)
	}
}

func TestRunJobSearchErrorFails(t *testing.T) {
	ctx, db := foreignTestDB(t)
	_, jid := insertForeignJob(t, ctx, db, "BS", "ntsb_foreign_search", 50, "running", 0)
	var cid int64
	db.QueryRowContext(ctx, `SELECT country_id FROM crawl_jobs WHERE id=?`, jid).Scan(&cid)
	clients := Clients{NTSB: &fixtureClient{Err: errors.New("boom")}}
	if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "BS", JobType: "ntsb_foreign_search"}); err != nil {
		t.Fatal(err)
	}
	var status string
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, jid).Scan(&status)
	if status != "failed" {
		t.Fatalf("status = %q, want failed", status)
	}
	var n int
	db.QueryRowContext(ctx, `SELECT COUNT(*) FROM crawl_errors WHERE crawl_job_id=?`, jid).Scan(&n)
	if n == 0 {
		t.Fatal("expected a crawl_errors row")
	}
}

func TestProcessPendingResumesStaleRunning(t *testing.T) {
	ctx, db := foreignTestDB(t)
	// stale running (2h old) + fresh running (now)
	_, staleJid := insertForeignJob(t, ctx, db, "ST", "ntsb_foreign_search", 100, "running", 7200000)
	_, freshJid := insertForeignJob(t, ctx, db, "FR", "ntsb_foreign_search", 10, "running", 0)
	clients := Clients{NTSB: &fixtureClient{Records: []ForeignRecord{{ForeignRef: "X1", Title: "A", OriginalURL: "https://n/1"}}}}
	processed, err := ProcessPending(ctx, db, clients, 0)
	if err != nil {
		t.Fatal(err)
	}
	if processed < 1 {
		t.Fatalf("processed = %d, want >= 1 (stale running re-picked)", processed)
	}
	var stale, fresh string
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, staleJid).Scan(&stale)
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, freshJid).Scan(&fresh)
	if stale == "running" {
		t.Error("stale running job should have been re-processed (not still running)")
	}
	if fresh != "running" {
		t.Errorf("fresh running job should be untouched, got %q", fresh)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run 'TestRunJob|TestProcessPending' -v`
Expected: FAIL — undefined symbols.

- [ ] **Step 3: Implement `runner.go`**

Create `internal/worker/foreignsearch/runner.go`, mirroring `internal/worker/wayback/runner.go`:

```go
package foreignsearch

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
)

type Job struct {
	ID        int64
	CountryID int64
	ISO2      string
	JobType   string
}

// Clients holds one AuthorityClient per foreign authority.
type Clients struct {
	NTSB AuthorityClient
	BEA  AuthorityClient
	ATSB AuthorityClient
}

type jobStats struct {
	Found  int `json:"found"`
	Staged int `json:"staged"`
	Errors int `json:"errors"`
}

// clientFor returns the client + authority code for a job type.
func clientFor(c Clients, jobType string) (AuthorityClient, string, bool) {
	switch jobType {
	case "ntsb_foreign_search":
		return c.NTSB, "ntsb", c.NTSB != nil
	case "bea_foreign_search":
		return c.BEA, "bea", c.BEA != nil
	case "atsb_search":
		return c.ATSB, "atsb", c.ATSB != nil
	default:
		return nil, "", false
	}
}

// RunJob executes one foreign-search job end-to-end and finalizes the crawl_job.
func RunJob(ctx context.Context, db *sql.DB, c Clients, job Job) error {
	client, authority, ok := clientFor(c, job.JobType)
	if !ok {
		recordError(ctx, db, job.ID, "foreignsearch://"+job.JobType, "unknown",
			fmt.Sprintf("no client for job_type %q (country %s)", job.JobType, job.ISO2))
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}
	recs, err := client.Search(ctx, job.ISO2)
	if err != nil {
		recordError(ctx, db, job.ID, authority+"://"+job.ISO2, "unknown", err.Error())
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}
	staged, err := StageRecords(ctx, db, job.ID, job.CountryID, authority, recs)
	if err != nil {
		_ = finalize(ctx, db, job.ID, "failed", jobStats{})
		return err
	}
	stats := jobStats{Found: len(recs), Staged: staged, Errors: 0}
	return finalize(ctx, db, job.ID, "success", stats)
}

// ProcessPending runs pending (and stale-running) foreign-search jobs, highest
// country priority first. limit <= 0 means no cap.
func ProcessPending(ctx context.Context, db *sql.DB, c Clients, limit int) (int, error) {
	q := `
		SELECT cj.id, c.id, c.iso2, cj.job_type
		  FROM crawl_jobs cj
		  JOIN countries c ON c.id = cj.country_id
		 WHERE cj.job_type IN ('ntsb_foreign_search','bea_foreign_search','atsb_search')
		   AND (
		         cj.status = 'pending'
		      OR (cj.status = 'running' AND cj.started_at IS NOT NULL
		          AND cj.started_at < (CAST(unixepoch('subsec') * 1000 AS INTEGER) - 3600000))
		       )
		 ORDER BY c.priority_score DESC, c.iso2 ASC`
	if limit > 0 {
		q += fmt.Sprintf(" LIMIT %d", limit)
	}
	rows, err := db.QueryContext(ctx, q)
	if err != nil {
		return 0, fmt.Errorf("foreignsearch: select pending jobs: %w", err)
	}
	var jobs []Job
	for rows.Next() {
		var j Job
		if err := rows.Scan(&j.ID, &j.CountryID, &j.ISO2, &j.JobType); err != nil {
			rows.Close()
			return 0, fmt.Errorf("foreignsearch: scan job: %w", err)
		}
		jobs = append(jobs, j)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, err
	}
	rows.Close()

	processed := 0
	for _, j := range jobs {
		if _, err := db.ExecContext(ctx,
			`UPDATE crawl_jobs SET status='running', started_at=CAST(unixepoch('subsec')*1000 AS INTEGER) WHERE id=?`, j.ID); err != nil {
			return processed, fmt.Errorf("foreignsearch: mark running %d: %w", j.ID, err)
		}
		if err := RunJob(ctx, db, c, j); err != nil {
			return processed, err
		}
		processed++
	}
	return processed, nil
}

func finalize(ctx context.Context, db *sql.DB, jobID int64, status string, stats jobStats) error {
	b, _ := json.Marshal(stats)
	if _, err := db.ExecContext(ctx, `
		UPDATE crawl_jobs SET status = ?, stats_json = ?, finished_at = CAST(unixepoch('subsec')*1000 AS INTEGER)
		 WHERE id = ?`, status, string(b), jobID); err != nil {
		return fmt.Errorf("foreignsearch: finalize job %d: %w", jobID, err)
	}
	return nil
}

func recordError(ctx context.Context, db *sql.DB, jobID int64, url, errType, msg string) {
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message) VALUES (?, ?, ?, ?)`,
		jobID, url, errType, msg)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/foreignsearch/ -run 'TestRunJob|TestProcessPending' -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the whole package + vet**

Run: `cd control-plane && go vet ./internal/worker/foreignsearch/ && go test ./internal/worker/foreignsearch/`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add internal/worker/foreignsearch/runner.go internal/worker/foreignsearch/runner_test.go
git commit -m "feat(control-plane): foreignsearch routing + RunJob + ProcessPending"
```

---

## Task 8: Wire the `process-foreign-search` subcommand

**Files:**
- Modify: `internal/app/app.go`
- Test: `internal/app/app_foreign_test.go`

**Interfaces:**
- Consumes: `foreignsearch.NewNTSBClient`, `NewBEAClient`, `NewATSBClient`, `foreignsearch.Clients`, `foreignsearch.ProcessPending`.
- Produces: `aviation-coverage process-foreign-search --db <path> [--limit N] [--source-file FILE]`. Builds `Clients{NTSB: NewNTSBClient(30s), BEA: NewBEAClient(30s), ATSB: NewATSBClient(*sourceFile)}` and calls `ProcessPending`; prints `processed N` to stderr; exit `exitOK`. Missing `--db` → `exitUsage`.

- [ ] **Step 1: Write the failing test**

Create `internal/app/app_foreign_test.go`:

```go
package app

import (
	"bytes"
	"context"
	"testing"
)

func TestProcessForeignRequiresDB(t *testing.T) {
	var out, errb bytes.Buffer
	if code := Run(context.Background(), []string{"process-foreign-search"}, &out, &errb); code != 2 {
		t.Fatalf("exit = %d, want 2 (usage)", code)
	}
}

func TestProcessForeignEmptyQueueOK(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer
	if code := Run(ctx, []string{"migrate", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	errb.Reset()
	if code := Run(ctx, []string{"process-foreign-search", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("process-foreign-search exit %d: %s", code, errb.String())
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/app/ -run TestProcessForeign -v`
Expected: FAIL — unknown command.

- [ ] **Step 3: Add the command + handler**

In `internal/app/app.go`: add the `foreignsearch` import; add `process-foreign-search` to BOTH command-list usage strings; add the switch case; add the handler (mirror `runProcessWayback`):

```go
	case "process-foreign-search":
		return runProcessForeign(ctx, rest, stderr)
```

```go
func runProcessForeign(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("process-foreign-search", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	limit := fs.Int("limit", 0, "max pending jobs to process (0 = no cap)")
	sourceFile := fs.String("source-file", "", "ATSB out-of-band export file (required for atsb_search jobs)")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "process-foreign-search: --db is required")
		fs.Usage()
		return exitUsage
	}
	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "process-foreign-search: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	clients := foreignsearch.Clients{
		NTSB: foreignsearch.NewNTSBClient(30 * time.Second),
		BEA:  foreignsearch.NewBEAClient(30 * time.Second),
		ATSB: foreignsearch.NewATSBClient(*sourceFile),
	}
	processed, err := foreignsearch.ProcessPending(ctx, db, clients, *limit)
	if err != nil {
		fmt.Fprintf(stderr, "process-foreign-search: %v\n", err)
		return exitFailure
	}
	fmt.Fprintf(stderr, "processed %d\n", processed)
	return exitOK
}
```

Add the import line `"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/worker/foreignsearch"` and append `, process-foreign-search` to both usage strings.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/app/ -run TestProcessForeign -v`
Expected: PASS (empty queue touches no network).

- [ ] **Step 5: Run the whole module + vet**

Run: `cd control-plane && go vet ./... && go test ./...`
Expected: PASS across all packages.

- [ ] **Step 6: Commit**

```bash
git add internal/app/app.go internal/app/app_foreign_test.go
git commit -m "feat(control-plane): wire process-foreign-search subcommand"
```

---

## Task 9: Docs

**Files:**
- Modify: `README.md` (add `### process-foreign-search` after `### process-wayback`)

- [ ] **Step 1: Add the README section**

Insert after the `### process-wayback` section in `README.md`:

````markdown
### process-foreign-search

Drains pending `ntsb_foreign_search` / `bea_foreign_search` / `atsb_search` crawl
jobs (created by `plan --enqueue`) for countries whose investigations are
delegated to a foreign authority (`delegate_iso2`). For each job, highest-country-
priority first, it queries the delegate authority's accident records for the
occurrence country and stages them into `staged_foreign_documents`.

```bash
./aviation-coverage process-foreign-search --db coverage.db --limit 20
```

- **NTSB** (US delegates) and **BEA** (FR delegates) are queried live.
- **ATSB** (AU delegates) sits behind Akamai bot-protection and is **out-of-band**:
  export the country's investigations JSON from a real browser (the project's
  mini-PC), then run with `--source-file`:

  ```bash
  ./aviation-coverage process-foreign-search --db coverage.db --source-file atsb_export.json
  ```

  An `atsb_search` job with no `--source-file` is finalized `failed` with a clear
  message.

Jobs are finalized `success` / `partial` / `failed` with `stats_json` of
`{found, staged, errors}` and a `crawl_errors` row per failure. Staging is
idempotent — `UNIQUE(authority, foreign_ref)`. Like the Wayback worker, a job left
`running` > 1h is automatically re-selected and resumed. Downloading the staged
`report_url` PDFs and promotion into `events`/`reports` is a later stage.
````

- [ ] **Step 2: Verify**

Run: `cd control-plane && grep -n "process-foreign-search" README.md`
Expected: the section is present.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(control-plane): document process-foreign-search"
```

---

## Final verification

- [ ] **Run the full module test suite + vet:**

Run: `cd control-plane && go vet ./... && go test ./...`
Expected: all packages PASS, no vet warnings.

- [ ] **Offline end-to-end sanity (no network — empty queue):**

```bash
cd control-plane
go build -o /tmp/aviation-coverage ./cmd/aviation-coverage
D=/tmp/fs-smoke.db; rm -f "$D" "$D"-wal "$D"-shm
/tmp/aviation-coverage migrate --db "$D"
/tmp/aviation-coverage seed --db "$D"
/tmp/aviation-coverage plan --db "$D" --enqueue
/tmp/aviation-coverage process-foreign-search --db "$D" --limit 0
```
Expected: prints `processed N` (N = pending foreign-search jobs). NTSB/BEA jobs hit the live services (live smoke); ATSB jobs without `--source-file` are finalized `failed` with a clear error, which is acceptable.

---

## Notes for the executor

- **Shared test helpers live once per package:** `foreignTestDB` + `stageFixtureJob` in `stage_test.go` (Task 3); `fixtureClient` in `record_test.go` (Task 2); `insertForeignJob` in `runner_test.go` (Task 7). Reuse — do not redefine.
- **Probe tasks (4, 5):** capture REAL fixtures from the live services before writing the parser; map the parser to the captured structure. If a service is unreachable after reasonable attempts, report BLOCKED with evidence — do not fabricate. Task 6 (ATSB) may use a schema-representative constructed fixture (the real export is an operator step), noted in the report.
- **`crawl_errors` error_type set** (from `002_pipeline.sql`): the runner uses `'unknown'`; that value is in the CHECK list. Confirm before Task 7.
- **Mirror, don't reinvent:** read `internal/worker/wayback/runner.go` and `internal/app/app.go`'s `runProcessWayback` for the exact finalize/recordError/stale-running idioms.
- Per-task commits; a fresh reviewer gates each task.
