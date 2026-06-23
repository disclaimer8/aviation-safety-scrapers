# Wayback Worker (Discovery + Acquisition) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `process-wayback` command to the aviation-coverage control-plane that drains pending `wayback_cdx` crawl jobs: resolve each country's defunct-archive target, query the Internet Archive CDX index for archived PDFs, stage the discovered snapshots, and download them to a local store with checksums — all idempotently and offline-testable.

**Architecture:** A new `internal/worker/wayback` package holds pure logic (CDX JSON parsing, target resolution, staging, download orchestration) behind a single injectable `Fetcher` network seam. A new migration adds the `wayback_target` column and a `staged_wayback_documents` table. A `process-wayback` subcommand wires it to the CLI in the existing command style. OCR/LLM extraction into events/reports is explicitly a later spec.

**Tech Stack:** Go 1.24+, `database/sql` over SQLite (`modernc.org/sqlite`), embedded SQL migrations, embedded JSON seed data, `crypto/sha256`, `encoding/json`, `net/http`, standard `testing`.

## Global Constraints

- Module path: `github.com/denyskolomiiets/aviation-safety-scrapers/control-plane` — all internal imports use this prefix.
- Go 1.24.0+. No new third-party dependencies.
- Migrations immutable once shipped: `internal/migrations/migrations.go` enforces filename + SHA-256 checksum drift detection. **Never edit `001`–`004`.** New schema goes only in a new `sql/NNN_name.sql` file matching `^sql/(\d{3})_([a-z0-9]+(?:_[a-z0-9]+)*)\.sql$`. The next number is `005`.
- SQLite tables use `STRICT`.
- The only network access is behind the `Fetcher` interface. All package logic is unit-tested with a `fixtureFetcher` (no real network in tests). The real `httpFetcher` hits `web.archive.org` directly (Wayback is not TLS-fingerprint-blocked).
- Determinism: queries that drive batches order by a stable key (`priority_score DESC, iso2 ASC`).
- Idempotency: `staged_wayback_documents` has `UNIQUE(country_id, digest)`; staging uses `ON CONFLICT … DO NOTHING`.
- Exit codes (from `internal/app/app.go`): `exitOK=0`, `exitFailure=1`, `exitUsage=2`.
- Test DB helper pattern: `database.Open(t.TempDir()+"/coverage.db")`, then `migrations.Apply(ctx, db)` and (where countries are needed) `seed.Apply(ctx, db)`.
- Downloaded files and the store dir are runtime artifacts — gitignored, never committed.

---

## File Structure

- `internal/migrations/sql/005_wayback.sql` — **new.** `countries.wayback_target` + `staged_wayback_documents`.
- `internal/seed/seed.go` — **modify.** Read/write `wayback_target` (mirror `delegate_iso2`).
- `internal/seed/data/country_overlays.json` — **modify.** +~6 pilot overlay rows with `wayback_target`.
- `internal/worker/wayback/cdx.go` — **new.** `Snapshot` type + `ParseCDX`.
- `internal/worker/wayback/target.go` — **new.** `ResolveTarget`.
- `internal/worker/wayback/fetcher.go` — **new.** `Fetcher` interface + `httpFetcher` (real).
- `internal/worker/wayback/stage.go` — **new.** `StageSnapshots`.
- `internal/worker/wayback/download.go` — **new.** `DownloadStaged`.
- `internal/worker/wayback/runner.go` — **new.** `RunJob`, `ProcessPending`, `Job`.
- `internal/worker/wayback/*_test.go` + `internal/worker/wayback/testdata/` — **new.** Tests + `fixtureFetcher` + CDX fixtures.
- `internal/app/app.go` — **modify.** Wire `process-wayback`.
- `README.md` — **modify.** Document `process-wayback`.
- `.gitignore` — **modify.** Ignore `wayback-store/`.

---

## Task 1: Migration `005_wayback.sql`

**Files:**
- Create: `internal/migrations/sql/005_wayback.sql`
- Test: `internal/migrations/migrations_wayback_test.go`

**Interfaces:**
- Consumes: `migrations.Apply(ctx, db)`; `database.Open`.
- Produces: `countries.wayback_target TEXT` (nullable) and table `staged_wayback_documents` with `UNIQUE(country_id, digest)`.

- [ ] **Step 1: Write the failing test**

Create `internal/migrations/migrations_wayback_test.go`:

```go
package migrations

import (
	"context"
	"testing"
)

func TestMigration005WaybackSchema(t *testing.T) {
	db := applyTestSchema(t)
	ctx := context.Background()

	// wayback_target column exists and is nullable.
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score, wayback_target)
		VALUES ('XW','XWW','Test W','Test','allowed','no_public_archive',1,3,'caa.example.gov')
	`); err != nil {
		t.Fatalf("insert with wayback_target: %v", err)
	}
	var got *string
	if err := db.QueryRowContext(ctx,
		`SELECT wayback_target FROM countries WHERE iso2='XW'`).Scan(&got); err != nil {
		t.Fatalf("select wayback_target: %v", err)
	}
	if got == nil || *got != "caa.example.gov" {
		t.Fatalf("wayback_target = %v, want caa.example.gov", got)
	}

	// staged_wayback_documents accepts a row and enforces UNIQUE(country_id,digest).
	var countryID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XW'`).Scan(&countryID); err != nil {
		t.Fatal(err)
	}
	// crawl_jobs needs a source; reuse any country and a fake source via a job row.
	var jobID int64
	// Insert a source + crawl_job to satisfy the FK.
	res, err := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('t','https://t/','https://t/','wayback',5)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'running')`, srcID, countryID)
	if err != nil {
		t.Fatal(err)
	}
	jobID, _ = res.LastInsertId()

	ins := `INSERT INTO staged_wayback_documents
		(crawl_job_id, country_id, original_url, archived_url, timestamp, mimetype, digest)
		VALUES (?,?,?,?,?,?,?)`
	if _, err := db.ExecContext(ctx, ins, jobID, countryID,
		"http://caa.example.gov/a.pdf", "https://web.archive.org/web/2010id_/http://caa.example.gov/a.pdf",
		"20100101000000", "application/pdf", "DIGEST1"); err != nil {
		t.Fatalf("insert staged doc: %v", err)
	}
	// Same (country_id, digest) must conflict.
	_, err = db.ExecContext(ctx, ins, jobID, countryID,
		"http://caa.example.gov/a.pdf", "https://web.archive.org/web/2011id_/http://caa.example.gov/a.pdf",
		"20110101000000", "application/pdf", "DIGEST1")
	if err == nil {
		t.Fatal("expected UNIQUE(country_id,digest) violation on duplicate digest")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/migrations/ -run TestMigration005Wayback -v`
Expected: FAIL — `no such column: wayback_target` (or no such table).

- [ ] **Step 3: Create the migration**

Create `internal/migrations/sql/005_wayback.sql`:

```sql
-- 005_wayback.sql
-- Adds the Wayback acquisition worker's target column and staging table.

ALTER TABLE countries
  ADD COLUMN wayback_target TEXT;

CREATE TABLE staged_wayback_documents (
  id INTEGER PRIMARY KEY,
  crawl_job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  original_url TEXT NOT NULL,
  archived_url TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  mimetype TEXT NOT NULL,
  digest TEXT NOT NULL,
  length INTEGER,
  local_file_path TEXT,
  checksum TEXT,
  download_status TEXT NOT NULL DEFAULT 'pending' CHECK(download_status IN (
    'pending',
    'downloaded',
    'failed',
    'skipped'
  )),
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  UNIQUE(country_id, digest)
) STRICT;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/migrations/ -run TestMigration005Wayback -v`
Expected: PASS.

- [ ] **Step 5: Bump the migration-count guard**

In `internal/migrations/migrations_test.go` find the assertion that the embedded migration count equals `4` and change it to `5`. (Same guard that went `3→4` for migration 004.)

- [ ] **Step 6: Run the full migrations suite**

Run: `cd control-plane && go test ./internal/migrations/...`
Expected: PASS — checksum/name guards green, count guard updated.

- [ ] **Step 7: Commit**

```bash
git add internal/migrations/sql/005_wayback.sql internal/migrations/migrations_wayback_test.go internal/migrations/migrations_test.go
git commit -m "feat(control-plane): migration 005 wayback_target + staged_wayback_documents"
```

---

## Task 2: Seed reads/writes `wayback_target`

**Files:**
- Modify: `internal/seed/seed.go` (the `overlayEntry` struct ~line 57; the country upsert `INSERT`/`ON CONFLICT`/`ExecContext` ~lines 152–230)
- Test: `internal/seed/seed_wayback_test.go`

**Interfaces:**
- Consumes: `seed.Apply(ctx, db) (Stats, error)`.
- Produces: `countries.wayback_target` populated from the overlay JSON (NULL when absent).

- [ ] **Step 1: Write the failing test**

Create `internal/seed/seed_wayback_test.go`:

```go
package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func TestSeedPopulatesWaybackTarget(t *testing.T) {
	ctx := context.Background()
	db, err := database.Open(t.TempDir() + "/coverage.db")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := migrations.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	if _, err := Apply(ctx, db); err != nil {
		t.Fatal(err)
	}

	// A country with no overlay wayback_target stays NULL (US).
	var us *string
	if err := db.QueryRowContext(ctx,
		`SELECT wayback_target FROM countries WHERE iso2='US'`).Scan(&us); err != nil {
		t.Fatalf("select US: %v", err)
	}
	if us != nil {
		t.Fatalf("US wayback_target = %v, want NULL", *us)
	}

	// At least one country has a non-NULL wayback_target after seeding
	// (the pilot batch from Task 3). This count is 0 until Task 3 lands; this
	// test only asserts the column is wired (US NULL). The pilot assertion lives
	// in Task 3's test.
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/seed/ -run TestSeedPopulatesWaybackTarget -v`
Expected: FAIL — compile error (no `wayback_target` handling) until Step 3–4.

- [ ] **Step 3: Add the overlay field**

In `internal/seed/seed.go`, add to the `overlayEntry` struct (after `DelegateISO2`):

```go
	WaybackTarget string `json:"wayback_target"`
```

- [ ] **Step 4: Thread it through the country upsert**

In `seed.go`:
1. Add `wayback_target` to the `INSERT INTO countries (...)` column list (after `delegate_iso2`) and add one more `?` to the `VALUES (...)`.
2. Add `wayback_target=excluded.wayback_target` to the `ON CONFLICT(iso2) DO UPDATE SET` clause.
3. Declare `var waybackTarget *string` alongside the other nullable locals.
4. Inside the `if o, ok := overlayMap[c.ISO2]; ok {` block add:

```go
			if o.WaybackTarget != "" {
				w := o.WaybackTarget
				waybackTarget = &w
			}
```

5. Add `waybackTarget` as the final argument to `stmtCountry.ExecContext(...)` (after `delegateISO2`).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/seed/ -run TestSeedPopulatesWaybackTarget -v`
Expected: PASS.

- [ ] **Step 6: Run the full seed + export suites**

Run: `cd control-plane && go test ./internal/seed/... ./internal/export/...`
Expected: PASS (export is unaffected — it does not select `wayback_target`).

- [ ] **Step 7: Commit**

```bash
git add internal/seed/seed.go internal/seed/seed_wayback_test.go
git commit -m "feat(control-plane): seed wayback_target from country overlays"
```

---

## Task 3: Pilot overlay batch (~6 countries with a defunct archive)

**Files:**
- Modify: `internal/seed/data/country_overlays.json`
- Test: `internal/seed/seed_wayback_pilot_test.go`

**Interfaces:**
- Consumes: `seed.Apply(ctx, db) (Stats, error)`.
- Produces: ~6 pilot countries seeded with a non-NULL `wayback_target` and a Wayback-appropriate `coverage_status`.

**Authoring (research step — produce verified real values, like the RAIO batch):**
Pick these six data-poor states whose national AAI/CAA has a weak or defunct web presence: **BO** (Bolivia), **PY** (Paraguay), **HN** (Honduras), **CM** (Cameroon), **CD** (DR Congo), **MG** (Madagascar). For each, research the former regulator/AAI domain (or accident-report URL prefix) and **verify it has PDF captures in the Internet Archive** before recording it:

```bash
# Verify captures exist (replace DOMAIN); a non-empty JSON array (beyond the header) means yes:
curl -s 'https://web.archive.org/cdx/search/cdx?url=DOMAIN/*&output=json&filter=mimetype:application/pdf&limit=5'
```

Record the verified domain as `wayback_target`. If a country has no PDF captures, substitute another data-poor state and note the swap in the report. Set `coverage_status` to `no_public_archive` (or `source_exists_unstable` if a live-but-flaky archive exists), `policy_status:"allowed"`, `refresh_cadence:"quarterly"`, and authored `group`(C3/D)/`coverage_score`/`effort_score`/`expected_records`/`expected_source_quality` consistent with a data-poor state (e.g. group D, coverage_score 1, effort_score 4, expected_records 15, expected_source_quality 2). Every row's `wayback_target` must be a domain you verified returns PDF captures.

- [ ] **Step 1: Write the failing test**

Create `internal/seed/seed_wayback_pilot_test.go`:

```go
package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func TestSeedWaybackPilot(t *testing.T) {
	ctx := context.Background()
	db, err := database.Open(t.TempDir() + "/coverage.db")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := migrations.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	if _, err := Apply(ctx, db); err != nil {
		t.Fatal(err)
	}

	pilot := []string{"BO", "PY", "HN", "CM", "CD", "MG"}
	for _, iso2 := range pilot {
		var target *string
		var coverage string
		if err := db.QueryRowContext(ctx,
			`SELECT wayback_target, coverage_status FROM countries WHERE iso2=?`, iso2).
			Scan(&target, &coverage); err != nil {
			t.Errorf("%s: %v", iso2, err)
			continue
		}
		if target == nil || *target == "" {
			t.Errorf("%s wayback_target is NULL, want a verified domain", iso2)
		}
		if coverage != "no_public_archive" && coverage != "source_exists_unstable" {
			t.Errorf("%s coverage_status = %q, want no_public_archive|source_exists_unstable", iso2, coverage)
		}
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/seed/ -run TestSeedWaybackPilot -v`
Expected: FAIL — pilot countries default to `coverage_status='unknown'`, `wayback_target` NULL.

- [ ] **Step 3: Add the 6 pilot overlay rows**

Add six objects to `internal/seed/data/country_overlays.json`. Example shape (use your verified domain per country):

```json
{ "iso2": "BO", "group": "D", "policy_status": "allowed", "coverage_status": "no_public_archive", "coverage_score": 1, "effort_score": 4, "expected_records": 15, "expected_source_quality": 2, "refresh_cadence": "quarterly", "wayback_target": "<verified-defunct-domain>", "notes": "Defunct/weak AAI web presence; recover via Wayback CDX." }
```

If any pilot country (BO/PY/HN/CM/CD/MG) turns out to have no PDF captures, swap it for another data-poor state, update the test's `pilot` slice and the JSON together, and note the swap in your report.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/seed/ -run TestSeedWaybackPilot -v`
Expected: PASS.

- [ ] **Step 5: Run full seed + export suites**

Run: `cd control-plane && go test ./internal/seed/... ./internal/export/...`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add internal/seed/data/country_overlays.json internal/seed/seed_wayback_pilot_test.go
git commit -m "feat(control-plane): seed Wayback pilot overlay batch (6 data-poor states)"
```

---

## Task 4: `Snapshot` type + `ParseCDX`

**Files:**
- Create: `internal/worker/wayback/cdx.go`
- Test: `internal/worker/wayback/cdx_test.go`, `internal/worker/wayback/testdata/cdx_sample.json`, `internal/worker/wayback/testdata/cdx_malformed.json`

**Interfaces:**
- Produces:
  - `type Snapshot struct { OriginalURL, ArchivedURL, Timestamp, Mimetype, Digest string; Length int64 }`
  - `func ParseCDX(raw []byte) (snaps []Snapshot, warnings int, err error)` — parses Internet Archive CDX JSON (array-of-arrays with a header row: `urlkey,timestamp,original,mimetype,statuscode,digest,length`). Drops the header; keeps only `statuscode=="200"` rows with a PDF mimetype; collapses by `digest` (first wins); builds `ArchivedURL = "https://web.archive.org/web/" + timestamp + "id_/" + original`. A row with the wrong field count or a non-numeric length is skipped and counted in `warnings` (never an error). A completely unparseable body returns an error.

- [ ] **Step 1: Write the failing test + fixtures**

Create `internal/worker/wayback/testdata/cdx_sample.json`:

```json
[["urlkey","timestamp","original","mimetype","statuscode","digest","length"],
 ["gov,example)/a.pdf","20100101000000","http://example.gov/a.pdf","application/pdf","200","DIGESTA","1024"],
 ["gov,example)/a.pdf","20120101000000","http://example.gov/a.pdf","application/pdf","200","DIGESTA","1024"],
 ["gov,example)/b.pdf","20110101000000","http://example.gov/b.pdf","application/pdf","200","DIGESTB","2048"],
 ["gov,example)/c.htm","20110101000000","http://example.gov/c.htm","text/html","200","DIGESTC","100"],
 ["gov,example)/d.pdf","20110101000000","http://example.gov/d.pdf","application/pdf","404","DIGESTD","0"]]
```

Create `internal/worker/wayback/testdata/cdx_malformed.json`:

```json
[["urlkey","timestamp","original","mimetype","statuscode","digest","length"],
 ["gov,example)/e.pdf","20100101000000","http://example.gov/e.pdf","application/pdf","200","DIGESTE"],
 ["gov,example)/f.pdf","20100101000000","http://example.gov/f.pdf","application/pdf","200","DIGESTF","notanumber"]]
```

Create `internal/worker/wayback/cdx_test.go`:

```go
package wayback

import (
	"os"
	"testing"
)

func TestParseCDXFiltersAndCollapses(t *testing.T) {
	raw, err := os.ReadFile("testdata/cdx_sample.json")
	if err != nil {
		t.Fatal(err)
	}
	snaps, warnings, err := ParseCDX(raw)
	if err != nil {
		t.Fatal(err)
	}
	if warnings != 0 {
		t.Errorf("warnings = %d, want 0", warnings)
	}
	// DIGESTA collapsed to one, DIGESTB kept, DIGESTC (html) dropped, DIGESTD (404) dropped.
	if len(snaps) != 2 {
		t.Fatalf("len(snaps) = %d, want 2 (%+v)", len(snaps), snaps)
	}
	byDigest := map[string]Snapshot{}
	for _, s := range snaps {
		byDigest[s.Digest] = s
	}
	a, ok := byDigest["DIGESTA"]
	if !ok {
		t.Fatal("DIGESTA missing")
	}
	if a.ArchivedURL != "https://web.archive.org/web/20100101000000id_/http://example.gov/a.pdf" {
		t.Errorf("ArchivedURL = %q", a.ArchivedURL)
	}
	if a.Length != 1024 {
		t.Errorf("Length = %d, want 1024", a.Length)
	}
}

func TestParseCDXCountsMalformedAsWarnings(t *testing.T) {
	raw, err := os.ReadFile("testdata/cdx_malformed.json")
	if err != nil {
		t.Fatal(err)
	}
	snaps, warnings, err := ParseCDX(raw)
	if err != nil {
		t.Fatal(err)
	}
	// One short row (skipped) + one bad length (skipped) = 2 warnings, 0 snapshots.
	if warnings != 2 {
		t.Errorf("warnings = %d, want 2", warnings)
	}
	if len(snaps) != 0 {
		t.Errorf("len(snaps) = %d, want 0", len(snaps))
	}
}

func TestParseCDXErrorsOnGarbage(t *testing.T) {
	if _, _, err := ParseCDX([]byte("not json")); err == nil {
		t.Fatal("expected error on unparseable body")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run TestParseCDX -v`
Expected: FAIL — package/`ParseCDX` undefined.

- [ ] **Step 3: Implement `cdx.go`**

Create `internal/worker/wayback/cdx.go`:

```go
// Package wayback is the Internet-Archive acquisition worker: it drains
// wayback_cdx crawl jobs by querying the CDX index for archived PDFs, staging
// the discovered snapshots, and downloading them to a local store.
package wayback

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// Snapshot is one archived PDF capture worth staging.
type Snapshot struct {
	OriginalURL string
	ArchivedURL string
	Timestamp   string
	Mimetype    string
	Digest      string
	Length      int64
}

// ParseCDX parses Internet Archive CDX JSON (array-of-arrays, first row is the
// header). It keeps HTTP-200 PDF captures, collapses by digest (first wins), and
// builds the raw archived URL. Malformed rows are skipped and counted in
// warnings; a non-array body returns an error.
func ParseCDX(raw []byte) (snaps []Snapshot, warnings int, err error) {
	var rows [][]string
	if err := json.Unmarshal(raw, &rows); err != nil {
		return nil, 0, fmt.Errorf("wayback: parse cdx json: %w", err)
	}
	seen := map[string]bool{}
	for i, r := range rows {
		if i == 0 {
			continue // header
		}
		if len(r) < 7 {
			warnings++
			continue
		}
		timestamp, original, mimetype, statuscode, digest, lengthStr := r[1], r[2], r[3], r[4], r[5], r[6]
		if statuscode != "200" {
			continue
		}
		if !strings.Contains(mimetype, "pdf") {
			continue
		}
		length, convErr := strconv.ParseInt(lengthStr, 10, 64)
		if convErr != nil {
			warnings++
			continue
		}
		if seen[digest] {
			continue
		}
		seen[digest] = true
		snaps = append(snaps, Snapshot{
			OriginalURL: original,
			ArchivedURL: "https://web.archive.org/web/" + timestamp + "id_/" + original,
			Timestamp:   timestamp,
			Mimetype:    mimetype,
			Digest:      digest,
			Length:      length,
		})
	}
	return snaps, warnings, nil
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run TestParseCDX -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/cdx.go internal/worker/wayback/cdx_test.go internal/worker/wayback/testdata/
git commit -m "feat(control-plane): wayback CDX JSON parsing"
```

---

## Task 5: `ResolveTarget`

**Files:**
- Create: `internal/worker/wayback/target.go`
- Test: `internal/worker/wayback/target_test.go`

**Interfaces:**
- Consumes: `*sql.DB`; `countries.wayback_target`; `authorities.archive_url`.
- Produces: `func ResolveTarget(ctx context.Context, db *sql.DB, countryID int64) (target string, ok bool, err error)` — returns the country's `wayback_target` if non-empty; else the first non-empty `authorities.archive_url` for that country; else `("", false, nil)`.

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/target_test.go`:

```go
package wayback

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func waybackTestDB(t *testing.T) (context.Context, *sql.DB) {
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

func insertCountry(t *testing.T, ctx context.Context, db *sql.DB, iso2 string, target *string) int64 {
	t.Helper()
	res, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score, wayback_target)
		VALUES (?, ?, 'N','R','allowed','no_public_archive',1,3, ?)`,
		iso2, iso2+"X", target)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}

func TestResolveTargetPrefersOverlay(t *testing.T) {
	ctx, db := waybackTestDB(t)
	target := "caa.aa.gov"
	id := insertCountry(t, ctx, db, "AA", &target)

	got, ok, err := ResolveTarget(ctx, db, id)
	if err != nil || !ok {
		t.Fatalf("ResolveTarget = (%q,%v,%v)", got, ok, err)
	}
	if got != "caa.aa.gov" {
		t.Fatalf("target = %q, want caa.aa.gov", got)
	}
}

func TestResolveTargetFallsBackToAuthority(t *testing.T) {
	ctx, db := waybackTestDB(t)
	id := insertCountry(t, ctx, db, "BB", nil) // no overlay target
	if _, err := db.ExecContext(ctx, `
		INSERT INTO authorities
			(country_id, normalized_name, name, type, archive_url, source_url, source_name)
		VALUES (?, 'aai', 'AAI', 'national_aai', 'archive.bb.gov', 'https://bb/', 'seed')`, id); err != nil {
		t.Fatal(err)
	}
	got, ok, err := ResolveTarget(ctx, db, id)
	if err != nil || !ok {
		t.Fatalf("ResolveTarget = (%q,%v,%v)", got, ok, err)
	}
	if got != "archive.bb.gov" {
		t.Fatalf("target = %q, want archive.bb.gov", got)
	}
}

func TestResolveTargetNoneWhenNeither(t *testing.T) {
	ctx, db := waybackTestDB(t)
	id := insertCountry(t, ctx, db, "CC", nil)
	got, ok, err := ResolveTarget(ctx, db, id)
	if err != nil {
		t.Fatal(err)
	}
	if ok || got != "" {
		t.Fatalf("ResolveTarget = (%q,%v), want (\"\",false)", got, ok)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run TestResolveTarget -v`
Expected: FAIL — `ResolveTarget` undefined.

- [ ] **Step 3: Implement `target.go`**

Create `internal/worker/wayback/target.go`:

```go
package wayback

import (
	"context"
	"database/sql"
	"fmt"
)

// ResolveTarget returns the Wayback query target for a country: its overlay
// wayback_target if set, else the first non-empty authority archive_url, else
// ("", false, nil).
func ResolveTarget(ctx context.Context, db *sql.DB, countryID int64) (string, bool, error) {
	var overlay sql.NullString
	if err := db.QueryRowContext(ctx,
		`SELECT wayback_target FROM countries WHERE id = ?`, countryID).Scan(&overlay); err != nil {
		return "", false, fmt.Errorf("wayback: resolve target country %d: %w", countryID, err)
	}
	if overlay.Valid && overlay.String != "" {
		return overlay.String, true, nil
	}

	var archive sql.NullString
	err := db.QueryRowContext(ctx, `
		SELECT archive_url FROM authorities
		 WHERE country_id = ? AND archive_url IS NOT NULL AND archive_url != ''
		 ORDER BY id ASC LIMIT 1`, countryID).Scan(&archive)
	if err == sql.ErrNoRows {
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("wayback: resolve authority archive %d: %w", countryID, err)
	}
	if archive.Valid && archive.String != "" {
		return archive.String, true, nil
	}
	return "", false, nil
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run TestResolveTarget -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/target.go internal/worker/wayback/target_test.go
git commit -m "feat(control-plane): wayback target resolution (overlay then authority)"
```

---

## Task 6: `Fetcher` interface + `httpFetcher` + `fixtureFetcher`

**Files:**
- Create: `internal/worker/wayback/fetcher.go`
- Test: `internal/worker/wayback/fetcher_test.go`

**Interfaces:**
- Produces:
  - `type Fetcher interface { CDX(ctx context.Context, domain string) ([]byte, error); Get(ctx context.Context, archivedURL string) ([]byte, error) }`
  - `func NewHTTPFetcher(timeout time.Duration) Fetcher` — real impl (returns the interface, not the unexported concrete type); `CDX` requests `https://web.archive.org/cdx/search/cdx?url=<domain>/*&output=json&filter=mimetype:application/pdf&collapse=digest`, `Get` requests the archived URL.
  - `func cdxURL(domain string) string` — unexported, builds the CDX request URL (unit-tested without network).
  - A `fixtureFetcher` test helper (in `fetcher_test.go`, exported within the package for reuse by later tasks' tests): `type fixtureFetcher struct { CDXBody []byte; Files map[string][]byte; GetErr map[string]error }` implementing `Fetcher`.

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/fetcher_test.go`:

```go
package wayback

import (
	"context"
	"strings"
	"testing"
)

// fixtureFetcher is the offline Fetcher used across the package's tests.
type fixtureFetcher struct {
	CDXBody []byte
	Files   map[string][]byte // archivedURL -> bytes
	GetErr  map[string]error  // archivedURL -> error to return
}

func (f *fixtureFetcher) CDX(ctx context.Context, domain string) ([]byte, error) {
	return f.CDXBody, nil
}

func (f *fixtureFetcher) Get(ctx context.Context, archivedURL string) ([]byte, error) {
	if f.GetErr != nil {
		if err, ok := f.GetErr[archivedURL]; ok {
			return nil, err
		}
	}
	if b, ok := f.Files[archivedURL]; ok {
		return b, nil
	}
	return []byte("default-pdf-bytes"), nil
}

func TestCDXURLConstruction(t *testing.T) {
	got := cdxURL("caa.example.gov")
	for _, want := range []string{
		"https://web.archive.org/cdx/search/cdx?",
		"url=caa.example.gov/*",
		"output=json",
		"filter=mimetype:application/pdf",
		"collapse=digest",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("cdxURL missing %q in %q", want, got)
		}
	}
}

// Compile-time check that *httpFetcher satisfies Fetcher.
var _ Fetcher = (*httpFetcher)(nil)
var _ Fetcher = (*fixtureFetcher)(nil)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run TestCDXURL -v`
Expected: FAIL — `cdxURL`/`httpFetcher` undefined.

- [ ] **Step 3: Implement `fetcher.go`**

Create `internal/worker/wayback/fetcher.go`:

```go
package wayback

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// Fetcher is the only network seam in the wayback worker. Production uses
// httpFetcher; tests use a fixtureFetcher.
type Fetcher interface {
	CDX(ctx context.Context, domain string) ([]byte, error)
	Get(ctx context.Context, archivedURL string) ([]byte, error)
}

type httpFetcher struct {
	client *http.Client
}

// NewHTTPFetcher returns a Fetcher backed by net/http against web.archive.org.
func NewHTTPFetcher(timeout time.Duration) Fetcher {
	return &httpFetcher{client: &http.Client{Timeout: timeout}}
}

func cdxURL(domain string) string {
	q := url.Values{}
	q.Set("url", domain+"/*")
	q.Set("output", "json")
	q.Set("filter", "mimetype:application/pdf")
	q.Set("collapse", "digest")
	return "https://web.archive.org/cdx/search/cdx?" + q.Encode()
}

func (h *httpFetcher) CDX(ctx context.Context, domain string) ([]byte, error) {
	return h.fetch(ctx, cdxURL(domain))
}

func (h *httpFetcher) Get(ctx context.Context, archivedURL string) ([]byte, error) {
	return h.fetch(ctx, archivedURL)
}

func (h *httpFetcher) fetch(ctx context.Context, u string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("wayback: build request %s: %w", u, err)
	}
	resp, err := h.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("wayback: fetch %s: %w", u, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("wayback: fetch %s: status %d", u, resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("wayback: read %s: %w", u, err)
	}
	return body, nil
}
```

Note: `q.Encode()` URL-encodes the `/` and `:` in the query values. The test asserts substrings `url=caa.example.gov/*` and `filter=mimetype:application/pdf` — adjust the test's expected substrings to the encoded forms if `Encode()` escapes them. To keep the asserted substrings literal and human-readable, build the query by hand instead of `url.Values.Encode()`:

```go
func cdxURL(domain string) string {
	return "https://web.archive.org/cdx/search/cdx?" +
		"url=" + domain + "/*" +
		"&output=json" +
		"&filter=mimetype:application/pdf" +
		"&collapse=digest"
}
```

Use this hand-built form (drop the `net/url` import) so the literal substrings in the test match. Domains here are trusted seed/authority values, not user input.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run TestCDXURL -v && go vet ./internal/worker/wayback/`
Expected: PASS; vet clean.

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/fetcher.go internal/worker/wayback/fetcher_test.go
git commit -m "feat(control-plane): wayback Fetcher interface + httpFetcher"
```

---

## Task 7: `StageSnapshots`

**Files:**
- Create: `internal/worker/wayback/stage.go`
- Test: `internal/worker/wayback/stage_test.go`

**Interfaces:**
- Consumes: `*sql.DB`; `Snapshot` (Task 4); `staged_wayback_documents`.
- Produces: `func StageSnapshots(ctx context.Context, db *sql.DB, jobID, countryID int64, snaps []Snapshot) (staged int, err error)` — inserts each snapshot with `download_status='pending'` via `INSERT … ON CONFLICT(country_id, digest) DO NOTHING`; returns the number of rows actually inserted (new ones).

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/stage_test.go`:

```go
package wayback

import (
	"context"
	"database/sql"
	"testing"
)

// stageFixtureJob inserts a country + source + running crawl_job and returns
// (countryID, jobID) for staging/download tests.
func stageFixtureJob(t *testing.T, ctx context.Context, db *sql.DB) (int64, int64) {
	t.Helper()
	cid := insertCountry(t, ctx, db, "ZZ", nil)
	res, err := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'running')`, srcID, cid)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()
	return cid, jid
}

func TestStageSnapshotsDedupsByDigest(t *testing.T) {
	ctx, db := waybackTestDB(t)
	cid, jid := stageFixtureJob(t, ctx, db)

	snaps := []Snapshot{
		{OriginalURL: "http://x/a.pdf", ArchivedURL: "https://web.archive.org/web/2010id_/http://x/a.pdf", Timestamp: "2010", Mimetype: "application/pdf", Digest: "D1", Length: 10},
		{OriginalURL: "http://x/b.pdf", ArchivedURL: "https://web.archive.org/web/2011id_/http://x/b.pdf", Timestamp: "2011", Mimetype: "application/pdf", Digest: "D2", Length: 20},
	}
	staged, err := StageSnapshots(ctx, db, jid, cid, snaps)
	if err != nil {
		t.Fatal(err)
	}
	if staged != 2 {
		t.Fatalf("staged = %d, want 2", staged)
	}

	// Re-staging the same digests inserts nothing.
	staged2, err := StageSnapshots(ctx, db, jid, cid, snaps)
	if err != nil {
		t.Fatal(err)
	}
	if staged2 != 0 {
		t.Fatalf("re-stage = %d, want 0 (dedup)", staged2)
	}

	var total int
	if err := db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM staged_wayback_documents WHERE country_id=?`, cid).Scan(&total); err != nil {
		t.Fatal(err)
	}
	if total != 2 {
		t.Fatalf("total staged = %d, want 2", total)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run TestStageSnapshots -v`
Expected: FAIL — `StageSnapshots` undefined.

- [ ] **Step 3: Implement `stage.go`**

Create `internal/worker/wayback/stage.go`:

```go
package wayback

import (
	"context"
	"database/sql"
	"fmt"
)

// StageSnapshots inserts each snapshot into staged_wayback_documents, skipping
// any (country_id, digest) already present. Returns the count newly inserted.
func StageSnapshots(ctx context.Context, db *sql.DB, jobID, countryID int64, snaps []Snapshot) (int, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("wayback: stage begin tx: %w", err)
	}
	defer tx.Rollback()

	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO staged_wayback_documents
			(crawl_job_id, country_id, original_url, archived_url, timestamp, mimetype, digest, length)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(country_id, digest) DO NOTHING`)
	if err != nil {
		return 0, fmt.Errorf("wayback: stage prepare: %w", err)
	}
	defer stmt.Close()

	staged := 0
	for _, s := range snaps {
		res, err := stmt.ExecContext(ctx, jobID, countryID, s.OriginalURL, s.ArchivedURL,
			s.Timestamp, s.Mimetype, s.Digest, s.Length)
		if err != nil {
			return 0, fmt.Errorf("wayback: stage insert %s: %w", s.Digest, err)
		}
		n, _ := res.RowsAffected()
		staged += int(n)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("wayback: stage commit: %w", err)
	}
	return staged, nil
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run TestStageSnapshots -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/stage.go internal/worker/wayback/stage_test.go
git commit -m "feat(control-plane): wayback snapshot staging with digest dedup"
```

---

## Task 8: `DownloadStaged`

**Files:**
- Create: `internal/worker/wayback/download.go`
- Test: `internal/worker/wayback/download_test.go`

**Interfaces:**
- Consumes: `*sql.DB`; `Fetcher` (Task 6); `staged_wayback_documents`.
- Produces:
  - `type StagedDoc struct { ID int64; ArchivedURL, Digest string }`
  - `func PendingDocs(ctx context.Context, db *sql.DB, countryID int64) ([]StagedDoc, error)` — staged docs with `download_status='pending'` for a country.
  - `func DownloadStaged(ctx context.Context, db *sql.DB, f Fetcher, storeDir, iso2 string, doc StagedDoc) error` — fetches `doc.ArchivedURL`, writes `<storeDir>/<iso2>/<digest>.pdf`, computes SHA-256, and updates the row (`local_file_path`, `checksum`, `download_status='downloaded'`). On fetch/write error sets `download_status='failed'` and returns the error.

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/download_test.go`:

```go
package wayback

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestDownloadStagedWritesFileAndChecksum(t *testing.T) {
	ctx, db := waybackTestDB(t)
	cid, jid := stageFixtureJob(t, ctx, db)
	archived := "https://web.archive.org/web/2010id_/http://x/a.pdf"
	if _, err := StageSnapshots(ctx, db, jid, cid, []Snapshot{
		{OriginalURL: "http://x/a.pdf", ArchivedURL: archived, Timestamp: "2010", Mimetype: "application/pdf", Digest: "D1", Length: 5},
	}); err != nil {
		t.Fatal(err)
	}
	docs, err := PendingDocs(ctx, db, cid)
	if err != nil || len(docs) != 1 {
		t.Fatalf("PendingDocs = %v, %v", docs, err)
	}

	body := []byte("PDF-A")
	f := &fixtureFetcher{Files: map[string][]byte{archived: body}}
	store := t.TempDir()
	if err := DownloadStaged(ctx, db, f, store, "ZZ", docs[0]); err != nil {
		t.Fatal(err)
	}

	wantPath := filepath.Join(store, "ZZ", "D1.pdf")
	got, err := os.ReadFile(wantPath)
	if err != nil {
		t.Fatalf("read %s: %v", wantPath, err)
	}
	if string(got) != "PDF-A" {
		t.Fatalf("file bytes = %q", got)
	}
	sum := sha256.Sum256(body)
	wantChecksum := hex.EncodeToString(sum[:])

	var status, path, checksum string
	if err := db.QueryRowContext(ctx,
		`SELECT download_status, local_file_path, checksum FROM staged_wayback_documents WHERE id=?`,
		docs[0].ID).Scan(&status, &path, &checksum); err != nil {
		t.Fatal(err)
	}
	if status != "downloaded" {
		t.Errorf("status = %q, want downloaded", status)
	}
	if path != wantPath {
		t.Errorf("local_file_path = %q, want %q", path, wantPath)
	}
	if checksum != wantChecksum {
		t.Errorf("checksum = %q, want %q", checksum, wantChecksum)
	}
}

func TestDownloadStagedMarksFailedOnFetchError(t *testing.T) {
	ctx, db := waybackTestDB(t)
	cid, jid := stageFixtureJob(t, ctx, db)
	archived := "https://web.archive.org/web/2010id_/http://x/a.pdf"
	if _, err := StageSnapshots(ctx, db, jid, cid, []Snapshot{
		{OriginalURL: "http://x/a.pdf", ArchivedURL: archived, Timestamp: "2010", Mimetype: "application/pdf", Digest: "D1", Length: 5},
	}); err != nil {
		t.Fatal(err)
	}
	docs, _ := PendingDocs(ctx, db, cid)
	f := &fixtureFetcher{GetErr: map[string]error{archived: errors.New("boom")}}
	if err := DownloadStaged(ctx, db, f, t.TempDir(), "ZZ", docs[0]); err == nil {
		t.Fatal("expected error from failed fetch")
	}
	var status string
	if err := db.QueryRowContext(ctx,
		`SELECT download_status FROM staged_wayback_documents WHERE id=?`, docs[0].ID).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "failed" {
		t.Fatalf("status = %q, want failed", status)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run TestDownloadStaged -v`
Expected: FAIL — `PendingDocs`/`DownloadStaged` undefined.

- [ ] **Step 3: Implement `download.go`**

Create `internal/worker/wayback/download.go`:

```go
package wayback

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
)

// StagedDoc is a staged document awaiting download.
type StagedDoc struct {
	ID          int64
	ArchivedURL string
	Digest      string
}

// PendingDocs returns the country's staged documents still pending download.
func PendingDocs(ctx context.Context, db *sql.DB, countryID int64) ([]StagedDoc, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT id, archived_url, digest FROM staged_wayback_documents
		 WHERE country_id = ? AND download_status = 'pending'
		 ORDER BY id ASC`, countryID)
	if err != nil {
		return nil, fmt.Errorf("wayback: pending docs %d: %w", countryID, err)
	}
	defer rows.Close()
	var out []StagedDoc
	for rows.Next() {
		var d StagedDoc
		if err := rows.Scan(&d.ID, &d.ArchivedURL, &d.Digest); err != nil {
			return nil, fmt.Errorf("wayback: scan pending doc: %w", err)
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// DownloadStaged fetches one staged document, writes it under
// <storeDir>/<iso2>/<digest>.pdf, records the checksum, and marks it downloaded.
// On failure it marks the row failed and returns the error.
func DownloadStaged(ctx context.Context, db *sql.DB, f Fetcher, storeDir, iso2 string, doc StagedDoc) error {
	body, err := f.Get(ctx, doc.ArchivedURL)
	if err != nil {
		markFailed(ctx, db, doc.ID)
		return fmt.Errorf("wayback: download %s: %w", doc.ArchivedURL, err)
	}
	dir := filepath.Join(storeDir, iso2)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		markFailed(ctx, db, doc.ID)
		return fmt.Errorf("wayback: mkdir %s: %w", dir, err)
	}
	path := filepath.Join(dir, doc.Digest+".pdf")
	if err := os.WriteFile(path, body, 0o644); err != nil {
		markFailed(ctx, db, doc.ID)
		return fmt.Errorf("wayback: write %s: %w", path, err)
	}
	sum := sha256.Sum256(body)
	checksum := hex.EncodeToString(sum[:])
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_wayback_documents
		   SET local_file_path = ?, checksum = ?, download_status = 'downloaded'
		 WHERE id = ?`, path, checksum, doc.ID); err != nil {
		return fmt.Errorf("wayback: mark downloaded %d: %w", doc.ID, err)
	}
	return nil
}

func markFailed(ctx context.Context, db *sql.DB, id int64) {
	_, _ = db.ExecContext(ctx,
		`UPDATE staged_wayback_documents SET download_status = 'failed' WHERE id = ?`, id)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run TestDownloadStaged -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/download.go internal/worker/wayback/download_test.go
git commit -m "feat(control-plane): wayback download to local store with checksum"
```

---

## Task 9: `RunJob` + `ProcessPending`

**Files:**
- Create: `internal/worker/wayback/runner.go`
- Test: `internal/worker/wayback/runner_test.go`

**Interfaces:**
- Consumes: everything above (`ResolveTarget`, `Fetcher.CDX`, `ParseCDX`, `StageSnapshots`, `PendingDocs`, `DownloadStaged`); `crawl_jobs`; `crawl_errors`.
- Produces:
  - `type Job struct { ID, CountryID int64; ISO2 string }`
  - `func RunJob(ctx context.Context, db *sql.DB, f Fetcher, storeDir string, job Job) error` — resolve target → CDX → parse → stage → download each pending doc → finalize the crawl_job (`success`/`partial`/`failed` + `stats_json{found,staged,downloaded,errors}` + `finished_at`). Records a `crawl_errors` row for an unresolved target, a CDX transport error, and each failed download. Never panics on one bad document.
  - `func ProcessPending(ctx context.Context, db *sql.DB, f Fetcher, storeDir string, limit int) (processed int, err error)` — selects pending `wayback_cdx` jobs joined to countries, ordered `priority_score DESC, iso2 ASC`, capped by `limit` (0 = no cap); marks each `running` then calls `RunJob`; returns the count processed.

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/runner_test.go`:

```go
package wayback

import (
	"context"
	"encoding/json"
	"os"
	"testing"
)

func TestRunJobSuccessStagesAndDownloads(t *testing.T) {
	ctx, db := waybackTestDB(t)
	target := "example.gov"
	cid := insertCountry(t, ctx, db, "ZZ", &target)
	res, _ := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	srcID, _ := res.LastInsertId()
	res, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'running')`, srcID, cid)
	jid, _ := res.LastInsertId()

	cdxBody, err := os.ReadFile("testdata/cdx_sample.json")
	if err != nil {
		t.Fatal(err)
	}
	f := &fixtureFetcher{CDXBody: cdxBody} // Get returns default bytes for any archived URL
	if err := RunJob(ctx, db, f, t.TempDir(), Job{ID: jid, CountryID: cid, ISO2: "ZZ"}); err != nil {
		t.Fatal(err)
	}

	var status string
	var stats string
	if err := db.QueryRowContext(ctx,
		`SELECT status, stats_json FROM crawl_jobs WHERE id=?`, jid).Scan(&status, &stats); err != nil {
		t.Fatal(err)
	}
	if status != "success" {
		t.Fatalf("status = %q, want success", status)
	}
	var s struct{ Found, Staged, Downloaded, Errors int }
	if err := json.Unmarshal([]byte(stats), &s); err != nil {
		t.Fatalf("stats_json: %v (%s)", err, stats)
	}
	// cdx_sample.json yields 2 PDF snapshots (DIGESTA, DIGESTB).
	if s.Found != 2 || s.Staged != 2 || s.Downloaded != 2 || s.Errors != 0 {
		t.Fatalf("stats = %+v, want found2 staged2 downloaded2 errors0", s)
	}
}

func TestRunJobNoTargetMarksFailed(t *testing.T) {
	ctx, db := waybackTestDB(t)
	cid := insertCountry(t, ctx, db, "ZZ", nil) // no target, no authority
	res, _ := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	srcID, _ := res.LastInsertId()
	res, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'running')`, srcID, cid)
	jid, _ := res.LastInsertId()

	if err := RunJob(ctx, db, &fixtureFetcher{}, t.TempDir(), Job{ID: jid, CountryID: cid, ISO2: "ZZ"}); err != nil {
		t.Fatal(err) // RunJob itself does not error on an unresolved target; it records it
	}
	var status string
	if err := db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, jid).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "failed" {
		t.Fatalf("status = %q, want failed", status)
	}
	var errCount int
	if err := db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM crawl_errors WHERE crawl_job_id=?`, jid).Scan(&errCount); err != nil {
		t.Fatal(err)
	}
	if errCount == 0 {
		t.Fatal("expected a crawl_errors row for unresolved target")
	}
}

func TestProcessPendingOrdersByPriority(t *testing.T) {
	ctx, db := waybackTestDB(t)
	// Two countries with targets; HI has higher priority_score.
	hiTarget, loTarget := "hi.gov", "lo.gov"
	hi := insertCountryPriority(t, ctx, db, "HI", &hiTarget, 100)
	lo := insertCountryPriority(t, ctx, db, "LO", &loTarget, 1)
	res, _ := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	srcID, _ := res.LastInsertId()
	for _, cid := range []int64{lo, hi} {
		db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
			VALUES (?,?, 'wayback_cdx', 'pending')`, srcID, cid)
	}
	cdxBody, _ := os.ReadFile("testdata/cdx_sample.json")
	f := &fixtureFetcher{CDXBody: cdxBody}
	processed, err := ProcessPending(ctx, db, f, t.TempDir(), 1) // limit 1 → only highest priority
	if err != nil {
		t.Fatal(err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	// HI job must be done (success), LO still pending.
	var hiStatus, loStatus string
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE country_id=?`, hi).Scan(&hiStatus)
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE country_id=?`, lo).Scan(&loStatus)
	if hiStatus != "success" {
		t.Errorf("HI status = %q, want success", hiStatus)
	}
	if loStatus != "pending" {
		t.Errorf("LO status = %q, want pending (limit 1, lower priority)", loStatus)
	}
}
```

Add this helper to `target_test.go` (it extends `insertCountry` with an explicit priority_score):

```go
func insertCountryPriority(t *testing.T, ctx context.Context, db *sql.DB, iso2 string, target *string, priority float64) int64 {
	t.Helper()
	res, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score, priority_score, wayback_target)
		VALUES (?, ?, 'N','R','allowed','no_public_archive',1,3, ?, ?)`,
		iso2, iso2+"X", priority, target)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run 'TestRunJob|TestProcessPending' -v`
Expected: FAIL — `RunJob`/`ProcessPending`/`Job` undefined.

- [ ] **Step 3: Implement `runner.go`**

Create `internal/worker/wayback/runner.go`:

```go
package wayback

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
)

// Job identifies one wayback_cdx crawl job to run.
type Job struct {
	ID        int64
	CountryID int64
	ISO2      string
}

type jobStats struct {
	Found      int `json:"found"`
	Staged     int `json:"staged"`
	Downloaded int `json:"downloaded"`
	Errors     int `json:"errors"`
}

// RunJob executes one wayback_cdx job end-to-end and finalizes the crawl_job
// row. It returns an error only on an unexpected DB failure; data-level problems
// (no target, transport error, bad document) are recorded against the job.
func RunJob(ctx context.Context, db *sql.DB, f Fetcher, storeDir string, job Job) error {
	target, ok, err := ResolveTarget(ctx, db, job.CountryID)
	if err != nil {
		return err
	}
	if !ok {
		recordError(ctx, db, job.ID, "wayback://"+job.ISO2, "unknown",
			fmt.Sprintf("no wayback target for %s", job.ISO2))
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}

	raw, err := f.CDX(ctx, target)
	if err != nil {
		recordError(ctx, db, job.ID, target, "unknown", err.Error())
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}

	snaps, warnings, err := ParseCDX(raw)
	if err != nil {
		recordError(ctx, db, job.ID, target, "parse_error", err.Error())
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}

	staged, err := StageSnapshots(ctx, db, job.ID, job.CountryID, snaps)
	if err != nil {
		return err
	}

	docs, err := PendingDocs(ctx, db, job.CountryID)
	if err != nil {
		return err
	}
	downloaded, dlErrors := 0, 0
	for _, d := range docs {
		if err := DownloadStaged(ctx, db, f, storeDir, job.ISO2, d); err != nil {
			recordError(ctx, db, job.ID, d.ArchivedURL, "unknown", err.Error())
			dlErrors++
			continue
		}
		downloaded++
	}

	stats := jobStats{Found: len(snaps), Staged: staged, Downloaded: downloaded, Errors: dlErrors}
	status := "success"
	if warnings > 0 || dlErrors > 0 {
		status = "partial"
	}
	return finalize(ctx, db, job.ID, status, stats)
}

// ProcessPending runs up to limit pending wayback_cdx jobs, highest country
// priority first. limit <= 0 means no cap.
func ProcessPending(ctx context.Context, db *sql.DB, f Fetcher, storeDir string, limit int) (int, error) {
	q := `
		SELECT cj.id, c.id, c.iso2
		  FROM crawl_jobs cj
		  JOIN countries c ON c.id = cj.country_id
		 WHERE cj.job_type = 'wayback_cdx' AND cj.status = 'pending'
		 ORDER BY c.priority_score DESC, c.iso2 ASC`
	if limit > 0 {
		q += fmt.Sprintf(" LIMIT %d", limit)
	}
	rows, err := db.QueryContext(ctx, q)
	if err != nil {
		return 0, fmt.Errorf("wayback: select pending jobs: %w", err)
	}
	var jobs []Job
	for rows.Next() {
		var j Job
		if err := rows.Scan(&j.ID, &j.CountryID, &j.ISO2); err != nil {
			rows.Close()
			return 0, fmt.Errorf("wayback: scan job: %w", err)
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
			`UPDATE crawl_jobs SET status='running', started_at=unixepoch('subsec')*1000 WHERE id=?`, j.ID); err != nil {
			return processed, fmt.Errorf("wayback: mark running %d: %w", j.ID, err)
		}
		if err := RunJob(ctx, db, f, storeDir, j); err != nil {
			return processed, err
		}
		processed++
	}
	return processed, nil
}

func finalize(ctx context.Context, db *sql.DB, jobID int64, status string, stats jobStats) error {
	b, _ := json.Marshal(stats)
	if _, err := db.ExecContext(ctx, `
		UPDATE crawl_jobs
		   SET status = ?, stats_json = ?, finished_at = unixepoch('subsec')*1000
		 WHERE id = ?`, status, string(b), jobID); err != nil {
		return fmt.Errorf("wayback: finalize job %d: %w", jobID, err)
	}
	return nil
}

func recordError(ctx context.Context, db *sql.DB, jobID int64, url, errType, msg string) {
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message)
		VALUES (?, ?, ?, ?)`, jobID, url, errType, msg)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/worker/wayback/ -run 'TestRunJob|TestProcessPending' -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the whole package + vet**

Run: `cd control-plane && go vet ./internal/worker/wayback/ && go test ./internal/worker/wayback/`
Expected: PASS, vet clean.

- [ ] **Step 6: Commit**

```bash
git add internal/worker/wayback/runner.go internal/worker/wayback/runner_test.go internal/worker/wayback/target_test.go
git commit -m "feat(control-plane): wayback job runner + ProcessPending"
```

---

## Task 10: Wire the `process-wayback` subcommand

**Files:**
- Modify: `internal/app/app.go` (the `Run` switch ~line 60; the two usage strings; add a `runProcessWayback` handler)
- Test: `internal/app/app_wayback_test.go`

**Interfaces:**
- Consumes: `wayback.NewHTTPFetcher`, `wayback.ProcessPending`; `database.Open`.
- Produces: `aviation-coverage process-wayback --db <path> [--limit N] [--store-dir DIR]`. Builds an `httpFetcher` (30s timeout) and calls `ProcessPending`; prints `processed N` to stderr; exit `exitOK`. Missing `--db` → `exitUsage`.

- [ ] **Step 1: Write the failing test**

Create `internal/app/app_wayback_test.go`:

```go
package app

import (
	"bytes"
	"context"
	"testing"
)

func TestProcessWaybackRequiresDB(t *testing.T) {
	var out, errb bytes.Buffer
	code := Run(context.Background(), []string{"process-wayback"}, &out, &errb)
	if code != 2 { // exitUsage
		t.Fatalf("exit = %d, want 2 (usage)", code)
	}
}

func TestProcessWaybackEmptyQueueOK(t *testing.T) {
	// A migrated+seeded DB with no pending wayback jobs: command succeeds,
	// processes zero. (No network is touched because there are no jobs to run.)
	dir := t.TempDir()
	path := dir + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer
	if code := Run(ctx, []string{"migrate", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	// Do NOT seed/enqueue — empty queue keeps this test offline.
	errb.Reset()
	code := Run(ctx, []string{"process-wayback", "--db", path}, &out, &errb)
	if code != 0 {
		t.Fatalf("process-wayback exit %d: %s", code, errb.String())
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/app/ -run TestProcessWayback -v`
Expected: FAIL — unknown command `process-wayback`.

- [ ] **Step 3: Add the command + handler**

In `internal/app/app.go`: add the `wayback` and `time` imports (if `time` isn't already imported, it is — used by `export`/`plan`); add `process-wayback` to both command-list usage strings; add the switch case; add the handler.

```go
	// import block:
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/worker/wayback"
```

```go
	// in Run's switch:
	case "process-wayback":
		return runProcessWayback(ctx, rest, stderr)
```

Append `, process-wayback` to both `commands: …` usage strings.

```go
// ── process-wayback ──────────────────────────────────────────────────────────

func runProcessWayback(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("process-wayback", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	limit := fs.Int("limit", 0, "max pending jobs to process (0 = no cap)")
	storeDir := fs.String("store-dir", "./wayback-store", "directory for downloaded PDFs")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "process-wayback: --db is required")
		fs.Usage()
		return exitUsage
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "process-wayback: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	fetcher := wayback.NewHTTPFetcher(30 * time.Second)
	processed, err := wayback.ProcessPending(ctx, db, fetcher, *storeDir, *limit)
	if err != nil {
		fmt.Fprintf(stderr, "process-wayback: %v\n", err)
		return exitFailure
	}
	fmt.Fprintf(stderr, "processed %d\n", processed)
	return exitOK
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/app/ -run TestProcessWayback -v`
Expected: PASS (the empty-queue test touches no network).

- [ ] **Step 5: Run the whole module + vet**

Run: `cd control-plane && go vet ./... && go test ./...`
Expected: PASS across all packages.

- [ ] **Step 6: Commit**

```bash
git add internal/app/app.go internal/app/app_wayback_test.go
git commit -m "feat(control-plane): wire process-wayback subcommand"
```

---

## Task 11: Docs + gitignore

**Files:**
- Modify: `README.md` (add a `### process-wayback` section after `### plan`)
- Modify: `.gitignore` (ignore `wayback-store/`)

- [ ] **Step 1: Add the README section**

Insert after the `### plan` section in `README.md`:

````markdown
### process-wayback

Drains pending `wayback_cdx` crawl jobs (created by `plan --enqueue`). For each
job, highest-country-priority first, it resolves the country's defunct-archive
target (overlay `wayback_target`, falling back to the country's authority
`archive_url`), queries the Internet Archive CDX index for archived PDFs, stages
the discovered captures into `staged_wayback_documents`, and downloads them to a
local store with SHA-256 checksums.

```bash
./aviation-coverage process-wayback --db coverage.db --limit 20 --store-dir ./wayback-store
```

Each job is finalized as `success` (all staged docs downloaded, no warnings),
`partial` (some downloads failed or malformed CDX rows skipped), or `failed` (no
resolvable target or a CDX transport error), with a `stats_json` of
`{found, staged, downloaded, errors}` and a `crawl_errors` row per failure.
Staging is idempotent — `UNIQUE(country_id, digest)` means a re-run never
double-stages a capture.

OCR of the downloaded PDFs and extraction into `events`/`reports` is a later
stage (Spec 2). Flags: `--limit N` (0 = no cap), `--store-dir DIR` (default
`./wayback-store`). The store directory is a runtime artifact and is gitignored.
````

- [ ] **Step 2: Update .gitignore**

Add to `control-plane/.gitignore` (create the entry if the file exists; if the project's `.gitignore` is at repo root, add it there under a control-plane section):

```
# Wayback worker local PDF store (runtime artifact)
wayback-store/
```

- [ ] **Step 3: Verify**

Run: `cd control-plane && grep -n "process-wayback" README.md && grep -n "wayback-store" .gitignore 2>/dev/null || grep -rn "wayback-store" ../.gitignore`
Expected: both present.

- [ ] **Step 4: Commit**

```bash
git add README.md .gitignore ../.gitignore 2>/dev/null; git commit -m "docs(control-plane): document process-wayback + ignore wayback-store"
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
D=/tmp/wb-smoke.db; rm -f "$D" "$D"-wal "$D"-shm
/tmp/aviation-coverage migrate --db "$D"
/tmp/aviation-coverage seed --db "$D"
/tmp/aviation-coverage plan --db "$D" --enqueue            # enqueues wayback_cdx jobs
/tmp/aviation-coverage process-wayback --db "$D" --limit 0 --store-dir /tmp/wb-store
```
Expected: `process-wayback` prints `processed N` (N = pending wayback jobs). This run DOES hit web.archive.org for pilot/unknown countries — it is a live smoke, not part of the offline suite. If run without network, jobs whose CDX fetch fails are finalized `failed` with `crawl_errors`, which is acceptable (the command still exits 0).

---

## Notes for the executor

- **Shared test helpers live once per package:** `waybackTestDB` + `insertCountry` in `target_test.go` (Task 5); `insertCountryPriority` appended to `target_test.go` (Task 9); `fixtureFetcher` in `fetcher_test.go` (Task 6); `stageFixtureJob` in `stage_test.go` (Task 7). Later tasks reuse them — do not redefine.
- **Constant/column names:** verify `crawl_errors` columns (`crawl_job_id, url, error_type, message`) and the `error_type` CHECK set against `internal/migrations/sql/002_pipeline.sql` before Task 9 — the plan uses `'unknown'` and `'parse_error'`, both in the CHECK list.
- The `download_status='skipped'` enum value is reserved (a future "local file already exists" path); v1 does not emit it. That is intentional, not dead code to remove.
- Per-task commits; a fresh reviewer gates each task.
