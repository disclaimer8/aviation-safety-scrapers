# Regional Worker (Discovery + Staging) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `process-regional` command that drains `archive_crawl` jobs for `regional_raio` countries: resolve the country's regional body (ECCAA/BAGAIA/IAC), query that body's accident archive for the country, and stage the discovered records idempotently.

**Architecture:** A new `internal/worker/regional` package with a per-body `RegionalClient` (the only network/file seam) and pure offline-tested parsers. A migration adds `staged_regional_documents`. Routing, staging, runner, and CLI mirror the merged Wayback worker (`internal/worker/wayback`) — read it as the reference for the exact `finalize`/`recordError`/stale-running/`runProcessWayback` idioms.

**Tech Stack:** Go 1.24+, `database/sql` over SQLite, embedded SQL migrations, `net/http`, `encoding/json`, stdlib `strings`/`regexp` for HTML, standard `testing`.

## Global Constraints

- **Work in the worktree:** `/Users/denyskolomiiets/ass-worktrees/regional`. All paths below are relative to `control-plane/` inside it. Run all `git`/`go` from there. Do NOT touch `~/aviation-safety-scrapers`.
- Module path: `github.com/denyskolomiiets/aviation-safety-scrapers/control-plane`. Go 1.24+. **No new third-party dependencies** (stdlib only — HTML via `strings`/`regexp`).
- Migrations immutable: never edit `001`–`006`. New schema only in `internal/migrations/sql/007_regional.sql`. The migration-count guard in `migrations_test.go` currently asserts `6`; bump it to `7`.
- SQLite tables use `STRICT`.
- The only network/file access is behind `RegionalClient`; all package logic is unit-tested with a `fixtureClient` (no real network in tests).
- Job selection orders `priority_score DESC, iso2 ASC`. Idempotency: `staged_regional_documents` has `UNIQUE(body_code, ref)`; staging uses `ON CONFLICT … DO NOTHING`.
- Exit codes: `exitOK=0`, `exitFailure=1`, `exitUsage=2`.
- Test DB helper: `database.Open(t.TempDir()+"/coverage.db")`, then `migrations.Apply(ctx, db)` and (where countries/bodies are needed) `seed.Apply(ctx, db)`.
- **Reference:** `internal/worker/wayback/runner.go` and `internal/app/app.go`'s `runProcessWayback` — mirror their idioms, adapting names.

---

## File Structure

- `internal/migrations/sql/007_regional.sql` — new.
- `internal/worker/regional/record.go` — `RegionalRecord`, `RegionalClient`.
- `internal/worker/regional/resolve.go` — `ResolveBody`.
- `internal/worker/regional/stage.go` — `StageRecords`.
- `internal/worker/regional/eccaa.go` / `bagaia.go` / `iac.go` — client + parser per body.
- `internal/worker/regional/runner.go` — `Job`, `Clients`, `clientFor`, `RunJob`, `ProcessPending`.
- `internal/worker/regional/*_test.go` + `testdata/` — tests, `fixtureClient`, fixtures.
- `internal/app/app.go` — wire `process-regional`.
- `README.md` — document the command.

---

## Task 1: Migration `007_regional.sql`

**Files:** Create `internal/migrations/sql/007_regional.sql`; Modify `internal/migrations/migrations_test.go` (count 6→7); Test `internal/migrations/migrations_regional_test.go`.

- [ ] **Step 1: Write the failing test** — Create `internal/migrations/migrations_regional_test.go`:

```go
package migrations

import (
	"context"
	"testing"
)

func TestMigration007RegionalSchema(t *testing.T) {
	db := applyTestSchema(t)
	ctx := context.Background()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score)
		VALUES ('XR','XRR','Test R','Test','allowed','regional_raio',2,3)`); err != nil {
		t.Fatal(err)
	}
	var cid int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XR'`).Scan(&cid)
	res, err := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES ('t','https://t/','https://t/','regional_body',4)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status)
		VALUES (?,?, 'archive_crawl','running')`, srcID, cid)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()
	ins := `INSERT INTO staged_regional_documents (crawl_job_id, country_id, body_code, ref, title, original_url)
		VALUES (?,?,?,?,?,?)`
	if _, err := db.ExecContext(ctx, ins, jid, cid, "IAC", "2024-RA-01", "Crash A", "https://mak.aero/x"); err != nil {
		t.Fatalf("insert staged regional doc: %v", err)
	}
	if _, err := db.ExecContext(ctx, ins, jid, cid, "IAC", "2024-RA-01", "dup", "https://mak.aero/y"); err == nil {
		t.Fatal("expected UNIQUE(body_code, ref) violation")
	}
	if _, err := db.ExecContext(ctx, ins, jid, cid, "XXX", "r2", "t", "https://x"); err == nil {
		t.Fatal("expected body_code CHECK violation for 'XXX'")
	}
}
```

- [ ] **Step 2: Run RED** — `cd control-plane && go test ./internal/migrations/ -run TestMigration007Regional -v` → FAIL (no such table).
- [ ] **Step 3: Create `internal/migrations/sql/007_regional.sql`:**

```sql
-- 007_regional.sql
-- Staging table for the regional worker: accident records discovered at a
-- regional investigation body (ECCAA/BAGAIA/IAC) for a member country.
CREATE TABLE staged_regional_documents (
  id INTEGER PRIMARY KEY,
  crawl_job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  body_code TEXT NOT NULL CHECK(body_code IN ('ECCAA', 'BAGAIA', 'IAC')),
  ref TEXT NOT NULL,
  title TEXT NOT NULL,
  occurrence_date TEXT,
  original_url TEXT NOT NULL,
  report_url TEXT,
  mimetype TEXT,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  UNIQUE(body_code, ref)
) STRICT;
```

- [ ] **Step 4: Run GREEN** — same command → PASS.
- [ ] **Step 5: Bump count guard** — in `internal/migrations/migrations_test.go` change the migration-count assertion `6` → `7`.
- [ ] **Step 6: Full migrations suite** — `cd control-plane && go test ./internal/migrations/...` → PASS.
- [ ] **Step 7: Commit** — `git add internal/migrations/sql/007_regional.sql internal/migrations/migrations_regional_test.go internal/migrations/migrations_test.go && git commit -m "feat(control-plane): migration 007 staged_regional_documents"`

---

## Task 2: `RegionalRecord` + `RegionalClient` + `fixtureClient`

**Files:** Create `internal/worker/regional/record.go`; Test `internal/worker/regional/record_test.go`.

- [ ] **Step 1: Write the failing test** — Create `record_test.go`:

```go
package regional

import (
	"context"
	"testing"
)

type fixtureClient struct {
	Records []RegionalRecord
	Err     error
}

func (f *fixtureClient) Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, error) {
	if f.Err != nil {
		return nil, f.Err
	}
	return f.Records, nil
}

var _ RegionalClient = (*fixtureClient)(nil)

func TestRegionalRecordFields(t *testing.T) {
	r := RegionalRecord{Ref: "2024-RA-01", Title: "A", OccurrenceDate: "2024-01-02",
		OriginalURL: "https://mak.aero/x", ReportURL: "https://mak.aero/x.pdf", Mimetype: "application/pdf"}
	if r.Ref == "" || r.Title == "" || r.OriginalURL == "" {
		t.Fatal("RegionalRecord required fields must be settable")
	}
}
```

- [ ] **Step 2: RED** — `cd control-plane && go test ./internal/worker/regional/ -run TestRegionalRecordFields -v` → FAIL (undefined).
- [ ] **Step 3: Implement `record.go`:**

```go
// Package regional is the regional-body acquisition worker: it drains
// archive_crawl jobs for regional_raio countries by querying the regional
// investigation body (ECCAA/BAGAIA/IAC) and staging the discovered records.
package regional

import "context"

// RegionalRecord is one accident record discovered at a regional body.
type RegionalRecord struct {
	Ref            string // the body's stable record id / report slug
	Title          string
	OccurrenceDate string // ISO yyyy-mm-dd when known, else ""
	OriginalURL    string // the human page for the record
	ReportURL      string // direct report/PDF URL when present, else ""
	Mimetype       string
}

// RegionalClient queries one regional body for accidents in a member country.
type RegionalClient interface {
	Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, error)
}
```

- [ ] **Step 4: GREEN** — same command → PASS.
- [ ] **Step 5: Commit** — `git add internal/worker/regional/record.go internal/worker/regional/record_test.go && git commit -m "feat(control-plane): regional RegionalRecord + RegionalClient"`

---

## Task 3: `ResolveBody`

**Files:** Create `internal/worker/regional/resolve.go`; Test `internal/worker/regional/resolve_test.go`.

**Interfaces:** Produces `func ResolveBody(ctx context.Context, db *sql.DB, countryID int64) (bodyCode string, ok bool, err error)` — joins `regional_body_members → regional_bodies` for the country, returns the body `code`; no body → `("", false, nil)`.

- [ ] **Step 1: Write the failing test** — Create `resolve_test.go`. It seeds (so the real ECCAA/BAGAIA/IAC bodies + members exist) and resolves a real member:

```go
package regional

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/seed"
)

func seededRegionalDB(t *testing.T) (context.Context, *sql.DB) {
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
	if _, err := seed.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	return ctx, db
}

func countryID(t *testing.T, ctx context.Context, db *sql.DB, iso2 string) int64 {
	t.Helper()
	var id int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2=?`, iso2).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestResolveBody(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	cases := map[string]string{"NG": "BAGAIA", "RU": "IAC", "LC": "ECCAA"}
	for iso2, wantBody := range cases {
		got, ok, err := ResolveBody(ctx, db, countryID(t, ctx, db, iso2))
		if err != nil || !ok {
			t.Errorf("%s: ResolveBody = (%q,%v,%v)", iso2, got, ok, err)
			continue
		}
		if got != wantBody {
			t.Errorf("%s body = %q, want %q", iso2, got, wantBody)
		}
	}
	// US is not a regional-body member → no body.
	got, ok, err := ResolveBody(ctx, db, countryID(t, ctx, db, "US"))
	if err != nil {
		t.Fatal(err)
	}
	if ok || got != "" {
		t.Fatalf("US ResolveBody = (%q,%v), want (\"\",false)", got, ok)
	}
}
```

- [ ] **Step 2: RED** — `cd control-plane && go test ./internal/worker/regional/ -run TestResolveBody -v` → FAIL (undefined).
- [ ] **Step 3: Implement `resolve.go`:**

```go
package regional

import (
	"context"
	"database/sql"
	"fmt"
)

// ResolveBody returns the regional body code (ECCAA/BAGAIA/IAC) that covers the
// country, or ("", false, nil) if the country belongs to no regional body.
func ResolveBody(ctx context.Context, db *sql.DB, countryID int64) (string, bool, error) {
	var code string
	err := db.QueryRowContext(ctx, `
		SELECT rb.code
		  FROM regional_body_members rbm
		  JOIN regional_bodies rb ON rb.id = rbm.regional_body_id
		 WHERE rbm.country_id = ?
		 ORDER BY rb.code ASC
		 LIMIT 1`, countryID).Scan(&code)
	if err == sql.ErrNoRows {
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("regional: resolve body for country %d: %w", countryID, err)
	}
	return code, true, nil
}
```

- [ ] **Step 4: GREEN** — same command → PASS.
- [ ] **Step 5: Commit** — `git add internal/worker/regional/resolve.go internal/worker/regional/resolve_test.go && git commit -m "feat(control-plane): regional body resolution"`

---

## Task 4: `StageRecords`

**Files:** Create `internal/worker/regional/stage.go`; Test `internal/worker/regional/stage_test.go`.

**Interfaces:** `func StageRecords(ctx, db, jobID, countryID int64, bodyCode string, recs []RegionalRecord) (staged int, err error)` — `ON CONFLICT(body_code, ref) DO NOTHING`; returns count newly inserted. Also defines a shared helper `regionalStageFixtureJob(t, ctx, db)` (a regional_raio country + source + running archive_crawl job → countryID, jobID), reused by Task 8.

- [ ] **Step 1: Write the failing test** — Create `stage_test.go`:

```go
package regional

import (
	"context"
	"database/sql"
	"testing"
)

func regionalStageFixtureJob(t *testing.T, ctx context.Context, db *sql.DB) (int64, int64) {
	t.Helper()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score)
		VALUES ('XR','XRR','Test R','Test','allowed','regional_raio',2,3)`); err != nil {
		t.Fatal(err)
	}
	var cid int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XR'`).Scan(&cid)
	res, err := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES ('rg','https://rg/','https://rg/','regional_body',4)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status)
		VALUES (?,?, 'archive_crawl','running')`, srcID, cid)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()
	return cid, jid
}

func TestStageRecordsDedups(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	cid, jid := regionalStageFixtureJob(t, ctx, db)
	recs := []RegionalRecord{
		{Ref: "2024-RA-01", Title: "A", OriginalURL: "https://mak.aero/1", ReportURL: "https://mak.aero/1.pdf", Mimetype: "application/pdf", OccurrenceDate: "2024-01-02"},
		{Ref: "2024-RA-02", Title: "B", OriginalURL: "https://mak.aero/2"},
	}
	n, err := StageRecords(ctx, db, jid, cid, "IAC", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("staged = %d, want 2", n)
	}
	n2, _ := StageRecords(ctx, db, jid, cid, "IAC", recs)
	if n2 != 0 {
		t.Fatalf("re-stage = %d, want 0", n2)
	}
	var total int
	db.QueryRowContext(ctx, `SELECT COUNT(*) FROM staged_regional_documents`).Scan(&total)
	if total != 2 {
		t.Fatalf("total = %d, want 2", total)
	}
}
```

(Note: `regionalStageFixtureJob` inserts country `XR`; `seededRegionalDB` already seeds the 249 ISO countries, so the `XR` insert ON CONFLICT is not an issue — `XR` is not a real ISO code, so it inserts cleanly. The seed helper is reused for the FK-valid bodies in Task 8.)

- [ ] **Step 2: RED** — `cd control-plane && go test ./internal/worker/regional/ -run TestStageRecordsDedups -v` → FAIL (undefined).
- [ ] **Step 3: Implement `stage.go`** (mirror the foreign-search / wayback staging exactly):

```go
package regional

import (
	"context"
	"database/sql"
	"fmt"
)

// StageRecords inserts each record into staged_regional_documents, skipping any
// (body_code, ref) already present. Returns the count newly inserted.
func StageRecords(ctx context.Context, db *sql.DB, jobID, countryID int64, bodyCode string, recs []RegionalRecord) (int, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("regional: stage begin tx: %w", err)
	}
	defer tx.Rollback()
	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO staged_regional_documents
			(crawl_job_id, country_id, body_code, ref, title, occurrence_date, original_url, report_url, mimetype)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(body_code, ref) DO NOTHING`)
	if err != nil {
		return 0, fmt.Errorf("regional: stage prepare: %w", err)
	}
	defer stmt.Close()
	staged := 0
	for _, r := range recs {
		res, err := stmt.ExecContext(ctx, jobID, countryID, bodyCode, r.Ref, r.Title,
			nullIfEmpty(r.OccurrenceDate), r.OriginalURL, nullIfEmpty(r.ReportURL), nullIfEmpty(r.Mimetype))
		if err != nil {
			return 0, fmt.Errorf("regional: stage insert %s/%s: %w", bodyCode, r.Ref, err)
		}
		n, _ := res.RowsAffected()
		staged += int(n)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("regional: stage commit: %w", err)
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

- [ ] **Step 4: GREEN** — same command → PASS.
- [ ] **Step 5: Commit** — `git add internal/worker/regional/stage.go internal/worker/regional/stage_test.go && git commit -m "feat(control-plane): regional record staging with dedup"`

---

## Task 5: IAC client + parser (probe-driven; the richest body)

**Files:** Create `internal/worker/regional/iac.go`, `internal/worker/regional/testdata/iac_listing.html` (or `.json`); Test `internal/worker/regional/iac_test.go`.

**Interfaces:** `func parseIAC(raw []byte) (recs []RegionalRecord, warnings int, err error)`; `type iacClient` implementing `RegionalClient`; `func NewIACClient(timeout time.Duration, sourceFile string) RegionalClient`.

**Probe-and-parse — capture a real IAC report listing.** IAC (Interstate Aviation Committee / МАК) publishes investigation reports at `mak.aero` (e.g. `https://mak.aero/rassledovaniya/` or the aviation-accidents report index). Capture a real listing page that contains several report entries (title, date, link). Save to `testdata/iac_listing.html`.

- [ ] **Step 1: Probe + capture** —
```bash
curl -s 'https://mak.aero/rassledovaniya/' | tee internal/worker/regional/testdata/iac_listing.html | head -c 400
```
Inspect; if `mak.aero` is behind Cloudflare and returns a challenge/403 to a data-centre fetch (likely — same class as ICAO), the IAC client becomes **out-of-band** (`--source-file`): an operator saves the listing from the mini-PC browser; construct a small representative `testdata/iac_listing.html` from the real markup you can see (a handful of report entries) and note in the report that IAC is out-of-band pending an operator export. **If you cannot determine the real markup at all, report BLOCKED.**

- [ ] **Step 2: Write the failing test** — Create `iac_test.go`:

```go
package regional

import (
	"os"
	"testing"
)

func TestParseIAC(t *testing.T) {
	raw, err := os.ReadFile("testdata/iac_listing.html")
	if err != nil {
		t.Fatal(err)
	}
	recs, _, err := parseIAC(raw)
	if err != nil {
		t.Fatalf("parseIAC: %v", err)
	}
	if len(recs) == 0 {
		t.Fatal("expected records from the IAC fixture, got 0")
	}
	for _, r := range recs {
		if r.Ref == "" || r.OriginalURL == "" || r.Title == "" {
			t.Errorf("record missing required field: %+v", r)
		}
	}
}
```

- [ ] **Step 3: RED** — `cd control-plane && go test ./internal/worker/regional/ -run TestParseIAC -v` → FAIL (undefined).
- [ ] **Step 4: Implement `iac.go`** — `parseIAC` PURE (stdlib `strings`/`regexp` for HTML, or `encoding/json` if the source is JSON), mapped to the captured markup: each entry → `Ref` (from the report URL slug/id), `OriginalURL` (absolute — prefix `https://mak.aero` for relative hrefs), `Title` (non-empty), `OccurrenceDate` (yyyy-mm-dd when present). Missing link/title → counted warning. Add `iacClient` + `NewIACClient(timeout, sourceFile)`: if `sourceFile != ""`, read it (out-of-band); else live-fetch the IAC listing URL; then `parseIAC`. Wrap errors `regional:`.
- [ ] **Step 5: GREEN + vet** — `go test ./internal/worker/regional/ -run TestParseIAC -v && go vet ./internal/worker/regional/` → PASS.
- [ ] **Step 6: Commit** — `git add internal/worker/regional/iac.go internal/worker/regional/iac_test.go internal/worker/regional/testdata/iac_listing.html && git commit -m "feat(control-plane): regional IAC client + parser"`

---

## Task 6: ECCAA client + parser (probe-driven)

**Files:** Create `internal/worker/regional/eccaa.go`, `testdata/eccaa_listing.html`; Test `eccaa_test.go`.

**Interfaces:** `func parseECCAA(raw []byte) ([]RegionalRecord, int, error)`; `type eccaaClient`; `func NewECCAAClient(timeout time.Duration, sourceFile string) RegionalClient`.

- [ ] **Step 1: Probe + capture** — ECCAA publishes at `eccaa.org` (look for an accident/incident reports or investigations page). Capture a real listing to `testdata/eccaa_listing.html`:
```bash
curl -s 'https://www.eccaa.org/' | tee internal/worker/regional/testdata/eccaa_listing.html | head -c 400
```
Find the actual reports/investigations listing URL on the site and capture THAT (not just the homepage). If ECCAA has no public report listing (small body — possible), capture whatever investigation/safety page exists and parse its document links; if there is genuinely nothing parseable, construct a small representative fixture from the site's markup and note it. **If the site is wholly unreachable, report BLOCKED.**
- [ ] **Step 2: Write `eccaa_test.go`** (same shape as `iac_test.go`, asserting non-empty records with required fields) → RED.
- [ ] **Step 3: RED** — `go test ./internal/worker/regional/ -run TestParseECCAA -v` → FAIL.
- [ ] **Step 4: Implement `eccaa.go`** — `parseECCAA` PURE stdlib, mapped to captured markup (Ref from doc URL slug, absolute OriginalURL with `https://www.eccaa.org` prefix, Title, date). `eccaaClient` + `NewECCAAClient(timeout, sourceFile)` (live or `--source-file`). Errors `regional:`.
- [ ] **Step 5: GREEN + vet.**
- [ ] **Step 6: Commit** — `git add internal/worker/regional/eccaa.go internal/worker/regional/eccaa_test.go internal/worker/regional/testdata/eccaa_listing.html && git commit -m "feat(control-plane): regional ECCAA client + parser"`

---

## Task 7: BAGAIA client + parser (probe-driven)

**Files:** Create `internal/worker/regional/bagaia.go`, `testdata/bagaia_listing.html`; Test `bagaia_test.go`.

**Interfaces:** `func parseBAGAIA(raw []byte) ([]RegionalRecord, int, error)`; `type bagaiaClient`; `func NewBAGAIAClient(timeout time.Duration, sourceFile string) RegionalClient`.

- [ ] **Step 1: Probe + capture** — BAGAIA / BAGASOO publishes at `bagasoo.org` (look for an accident investigation / reports page; BAGAIA is the investigation arm). Capture the real reports listing to `testdata/bagaia_listing.html`. Same fallback rules as Task 6 (construct-from-real-markup if no clean listing; BLOCKED if wholly unreachable).
- [ ] **Step 2: Write `bagaia_test.go`** (same shape, TestParseBAGAIA) → RED.
- [ ] **Step 3: RED.**
- [ ] **Step 4: Implement `bagaia.go`** — `parseBAGAIA` PURE stdlib mapped to captured markup (Ref from slug, absolute OriginalURL with `https://www.bagasoo.org` prefix, Title, date). `bagaiaClient` + `NewBAGAIAClient(timeout, sourceFile)`. Errors `regional:`.
- [ ] **Step 5: GREEN + vet.**
- [ ] **Step 6: Commit** — `git add internal/worker/regional/bagaia.go internal/worker/regional/bagaia_test.go internal/worker/regional/testdata/bagaia_listing.html && git commit -m "feat(control-plane): regional BAGAIA client + parser"`

---

## Task 8: Routing + `RunJob` + `ProcessPending`

**Files:** Create `internal/worker/regional/runner.go`; Test `internal/worker/regional/runner_test.go`.

**Interfaces:**
- `type Job struct { ID, CountryID int64; ISO2, BodyCode string }`
- `type Clients struct { ECCAA, BAGAIA, IAC RegionalClient }`
- `func clientFor(c Clients, bodyCode string) (RegionalClient, bool)` — ECCAA/BAGAIA/IAC → client; unknown or nil → false.
- `func RunJob(ctx, db, c Clients, job Job) error` — pick client by `job.BodyCode`; unknown/nil client → crawl_errors + finalize "failed"; `Search` error → crawl_errors + finalize "failed"; StageRecords DB error → finalize "failed" + return err; success → finalize "success" + stats{found,staged,errors}.
- `func ProcessPending(ctx, db, c Clients, limit int) (processed int, err error)` — selects `job_type='archive_crawl'` jobs JOIN countries **WHERE c.coverage_status='regional_raio'** AND (pending OR stale-running >1h), ORDER BY priority_score DESC, iso2 ASC, LIMIT when limit>0; for each, resolve body via `ResolveBody` (set Job.BodyCode; if no body → finalize failed + crawl_errors, skip); mark running; RunJob.

**Mirror `internal/worker/wayback/runner.go`** for finalize/recordError/jobStats/stale-running idioms. error_type `'unknown'`.

- [ ] **Step 1: Write the failing test** — Create `runner_test.go`:

```go
package regional

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"testing"
)

// insertRegionalJob inserts a regional_raio country that is a real member of the
// given body (so ResolveBody finds it), a source, and an archive_crawl job.
func insertRegionalJob(t *testing.T, ctx context.Context, db *sql.DB, iso2 string, status string, startedAgoMs int64) (int64, int64) {
	t.Helper()
	// iso2 must be a real seeded regional-body member (e.g. NG=BAGAIA, RU=IAC, LC=ECCAA).
	// Set its coverage_status to regional_raio (the expansion may already have done so;
	// force it here for determinism).
	if _, err := db.ExecContext(ctx, `UPDATE countries SET coverage_status='regional_raio' WHERE iso2=?`, iso2); err != nil {
		t.Fatal(err)
	}
	cid := countryID(t, ctx, db, iso2)
	res, _ := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES (?, ?, ?, 'regional_body',4)`, "s"+iso2, "https://s/"+iso2, "https://s/"+iso2)
	srcID, _ := res.LastInsertId()
	res, err := db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status) VALUES (?,?, 'archive_crawl', ?)`, srcID, cid, status)
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
	ctx, db := seededRegionalDB(t)
	cid, jid := insertRegionalJob(t, ctx, db, "RU", "running", 0)
	clients := Clients{IAC: &fixtureClient{Records: []RegionalRecord{
		{Ref: "2024-RA-01", Title: "A", OriginalURL: "https://mak.aero/1"},
		{Ref: "2024-RA-02", Title: "B", OriginalURL: "https://mak.aero/2"},
	}}}
	if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "RU", BodyCode: "IAC"}); err != nil {
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
	ctx, db := seededRegionalDB(t)
	cid, jid := insertRegionalJob(t, ctx, db, "RU", "running", 0)
	clients := Clients{IAC: &fixtureClient{Err: errors.New("boom")}}
	if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "RU", BodyCode: "IAC"}); err != nil {
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

func TestProcessPendingOnlyRegionalRaioAndResumesStale(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	// regional_raio member (RU=IAC) stale-running 2h → re-picked.
	_, staleJid := insertRegionalJob(t, ctx, db, "RU", "running", 7200000)
	// a non-regional archive_crawl job (US is direct_public_archive after expansion;
	// force direct_public_archive) must NOT be selected.
	db.ExecContext(ctx, `UPDATE countries SET coverage_status='direct_public_archive' WHERE iso2='US'`)
	usCid := countryID(t, ctx, db, "US")
	res, _ := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier) VALUES ('us','https://us/','https://us/','regulator',4)`)
	usSrc, _ := res.LastInsertId()
	res, _ = db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status) VALUES (?,?, 'archive_crawl','pending')`, usSrc, usCid)
	usJid, _ := res.LastInsertId()

	clients := Clients{IAC: &fixtureClient{Records: []RegionalRecord{{Ref: "X1", Title: "A", OriginalURL: "https://mak.aero/1"}}}}
	processed, err := ProcessPending(ctx, db, clients, 0)
	if err != nil {
		t.Fatal(err)
	}
	if processed < 1 {
		t.Fatalf("processed = %d, want >= 1", processed)
	}
	var stale, us string
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, staleJid).Scan(&stale)
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, usJid).Scan(&us)
	if stale == "running" {
		t.Error("stale regional_raio job should have been re-processed")
	}
	if us != "pending" {
		t.Errorf("non-regional (direct_public_archive) job must be untouched, got %q", us)
	}
}
```

- [ ] **Step 2: RED** — `go test ./internal/worker/regional/ -run 'TestRunJob|TestProcessPending' -v` → FAIL.
- [ ] **Step 3: Implement `runner.go`** — mirror `internal/worker/wayback/runner.go`. `clientFor` switch on bodyCode. `RunJob`: client lookup → Search → StageRecords → finalize. `ProcessPending` query:

```sql
SELECT cj.id, c.id, c.iso2
  FROM crawl_jobs cj
  JOIN countries c ON c.id = cj.country_id
 WHERE cj.job_type = 'archive_crawl'
   AND c.coverage_status = 'regional_raio'
   AND (
         cj.status = 'pending'
      OR (cj.status = 'running' AND cj.started_at IS NOT NULL
          AND cj.started_at < (CAST(unixepoch('subsec') * 1000 AS INTEGER) - 3600000))
       )
 ORDER BY c.priority_score DESC, c.iso2 ASC
```
For each selected job: `ResolveBody(ctx, db, countryID)` → if `!ok`, `recordError`+`finalize("failed")` and continue (count as processed); else set `Job.BodyCode`, mark running, `RunJob`. Include `finalize`/`recordError`/`jobStats` helpers identical to the wayback runner.

- [ ] **Step 4: GREEN + vet** — `go test ./internal/worker/regional/ -run 'TestRunJob|TestProcessPending' -v && go vet ./internal/worker/regional/` → PASS.
- [ ] **Step 5: Full package suite** — `go test ./internal/worker/regional/`.
- [ ] **Step 6: Commit** — `git add internal/worker/regional/runner.go internal/worker/regional/runner_test.go && git commit -m "feat(control-plane): regional routing + RunJob + ProcessPending"`

---

## Task 9: Wire `process-regional` subcommand

**Files:** Modify `internal/app/app.go`; Test `internal/app/app_regional_test.go`.

Mirror `runProcessWayback`/`runProcessForeign`. Flags: `--db` (required), `--limit`, `--source-file` (for out-of-band bodies). Build `regional.Clients{ECCAA: NewECCAAClient(30s, *sourceFile), BAGAIA: NewBAGAIAClient(30s, *sourceFile), IAC: NewIACClient(30s, *sourceFile)}` → `ProcessPending`; print `processed N` to stderr; exitOK. Missing --db → exitUsage. Add the `regional` import + `process-regional` to BOTH usage strings + switch case.

- [ ] **Step 1: Write `internal/app/app_regional_test.go`:**

```go
package app

import (
	"bytes"
	"context"
	"testing"
)

func TestProcessRegionalRequiresDB(t *testing.T) {
	var out, errb bytes.Buffer
	if code := Run(context.Background(), []string{"process-regional"}, &out, &errb); code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
}

func TestProcessRegionalEmptyQueueOK(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer
	if code := Run(ctx, []string{"migrate", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	errb.Reset()
	if code := Run(ctx, []string{"process-regional", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("process-regional exit %d: %s", code, errb.String())
	}
}
```

- [ ] **Step 2: RED** — `go test ./internal/app/ -run TestProcessRegional -v` → FAIL (unknown command).
- [ ] **Step 3: Implement** — add `regional` import (`.../internal/worker/regional`), `process-regional` to both usage strings, the switch case `case "process-regional": return runProcessRegional(ctx, rest, stderr)`, and the `runProcessRegional` handler (mirror `runProcessForeign` from the foreign-search worker / `runProcessWayback`, building `regional.Clients` as above).
- [ ] **Step 4: GREEN** — `go test ./internal/app/ -run TestProcessRegional -v` → PASS.
- [ ] **Step 5: Full module + vet** — `go vet ./... && go test ./...` → all PASS.
- [ ] **Step 6: Commit** — `git add internal/app/app.go internal/app/app_regional_test.go && git commit -m "feat(control-plane): wire process-regional subcommand"`

---

## Task 10: Docs

**Files:** Modify `README.md` (add `### process-regional` after `### process-wayback`).

- [ ] **Step 1:** Insert a `### process-regional` section after `### process-wayback`:

````markdown
### process-regional

Drains pending `archive_crawl` crawl jobs for `regional_raio` countries (created by
`plan --enqueue`) — states covered by a regional investigation body. For each job
(highest country priority first), it resolves the country's regional body
(ECCAA / BAGAIA / IAC via `regional_body_members`) and queries that body's accident
archive, staging discovered records into `staged_regional_documents`.

```bash
./aviation-coverage process-regional --db coverage.db --limit 20
```

- ECCAA and BAGAIA are queried live. **IAC (`mak.aero`)** may sit behind Cloudflare;
  if so it is **out-of-band** — export the listing from a real browser and run with
  `--source-file`.

Only `archive_crawl` jobs whose country is `regional_raio` are processed;
`archive_crawl` jobs for `direct_public_archive` countries are left for a future
authority-archive worker. Jobs are finalized `success`/`partial`/`failed` with
`stats_json{found,staged,errors}`; staging is idempotent
(`UNIQUE(body_code, ref)`); a job left `running` > 1h is auto-resumed. Report
download + promotion into `events`/`reports` is a later stage.
````

- [ ] **Step 2: Verify** — `grep -n "process-regional" README.md`.
- [ ] **Step 3: Commit** — `git add README.md && git commit -m "docs(control-plane): document process-regional"`

---

## Final verification

- [ ] **Full suite + vet:** `cd control-plane && go vet ./... && go test ./...` → all PASS.
- [ ] **Offline sanity:** build the binary; `migrate` + `seed` + `plan --enqueue` + `process-regional --db <db> --limit 0` → prints `processed N` (regional jobs; live bodies hit the net, out-of-band IAC without `--source-file` finalizes failed — acceptable).

---

## Notes for the executor

- **Shared test helpers once per package:** `seededRegionalDB` + `countryID` in `resolve_test.go` (Task 3); `regionalStageFixtureJob` in `stage_test.go` (Task 4); `fixtureClient` in `record_test.go` (Task 2); `insertRegionalJob` in `runner_test.go` (Task 8). Reuse — do not redefine.
- **Probe tasks (5,6,7):** capture REAL listings from mak.aero / eccaa.org / bagasoo.org before writing each parser. If a site is Cloudflare-blocked from this environment (likely mak.aero), make that body out-of-band (`--source-file`) and construct a small representative fixture from the markup you can observe, noting it in the report. Report BLOCKED only if a site's markup is wholly undeterminable.
- **Migration number coordination:** this uses `007`; the foreign-search PR (#9) also added a migration — whichever merges second renumbers.
- **error_type 'unknown'** is in the `crawl_errors` CHECK set (002_pipeline.sql).
- Per-task commits; a fresh reviewer gates each task.
```
