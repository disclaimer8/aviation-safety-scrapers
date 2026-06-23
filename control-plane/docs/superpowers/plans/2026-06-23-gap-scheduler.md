# Gap Scheduler (`plan` command) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `plan` command to the aviation-coverage control-plane that ranks coverage gaps by ROI and emits `crawl_jobs` of the applicable type(s) per country, plus the `delegate_iso2` data-model addition and a first overlay batch (20 RAIO member states) so the ranking runs on real data.

**Architecture:** A new `internal/planner` package holds the pure mapping/ranking/resolution logic (table-driven, unit-tested in isolation) and the DB-facing build/enqueue functions. A new `plan` subcommand in `internal/app/app.go` wires it to the CLI in the read-only-by-default style of `export` (prints a JSON plan; `--enqueue` writes). Data additions ship as a new migration plus seed-file edits.

**Tech Stack:** Go 1.24+, `database/sql` over SQLite (`modernc.org/sqlite` via `internal/database`), embedded SQL migrations, embedded JSON seed data, standard `testing` package.

## Global Constraints

- Module path: `github.com/denyskolomiiets/aviation-safety-scrapers/control-plane` — all internal imports use this prefix.
- Go 1.24.0 or later. No new third-party dependencies.
- Migrations are immutable once shipped: `internal/migrations/migrations.go` enforces name + SHA-256 checksum drift detection. **Never edit `001_core.sql`, `002_pipeline.sql`, or `003_provenance.sql`.** New schema goes in a new `sql/NNN_name.sql` file matching `^sql/(\d{3})_([a-z0-9]+(?:_[a-z0-9]+)*)\.sql$`.
- SQLite tables use `STRICT`. `crawl_jobs.source_id` is `NOT NULL REFERENCES sources(id)` — every emitted job MUST resolve a real source row; never insert a dangling FK.
- Ranking is read from the existing `countries.priority_score` column (`model.PriorityScore = expected_records × expected_source_quality ÷ effort_score`). Do not recompute it in the planner.
- Determinism: every query and output orders by a stable key (`priority_score DESC, iso2 ASC` for countries) so output is reproducible — the project's `export` convention.
- `plan` is read-only by default; it writes `crawl_jobs` only when `--enqueue` is passed, inside a single transaction.
- Policy-excluded countries (`policy_status = 'excluded'`) are never scheduled.
- Exit codes (from `internal/app/app.go`): `exitOK=0`, `exitFailure=1`, `exitUsage=2`.
- Test DB helper pattern: `database.Open(t.TempDir() + "/coverage.db")`, then `migrations.Apply(ctx, db)` and `seed.Apply(ctx, db)`.

---

## File Structure

- `internal/migrations/sql/004_delegate.sql` — **new.** Adds nullable `countries.delegate_iso2`.
- `internal/seed/seed.go` — **modify.** Read/write `delegate_iso2` on overlays.
- `internal/seed/data/country_overlays.json` — **modify.** Add `delegate_iso2` to AD; add 20 RAIO-member rows.
- `internal/seed/data/sources.json` — **modify.** Add method-source rows for job types lacking one.
- `internal/model/types.go` — **modify.** Allow `wayback` source type in `SourceTierAllowsType` (tier 5).
- `internal/planner/mapping.go` — **new.** `coverage_status → []job_type` + `delegate_iso2 → foreign-search job_type`.
- `internal/planner/planner.go` — **new.** Candidate ranking query, source resolution, idempotency + cadence gate, `BuildPlan`, `Enqueue`, plan types.
- `internal/planner/*_test.go` — **new.** Unit tests per the spec's §8.
- `internal/app/app.go` — **modify.** Wire the `plan` subcommand.
- `README.md` — **modify.** Document `plan`.

---

## Task 1: Migration `004_delegate.sql` — `delegate_iso2` column

**Files:**
- Create: `internal/migrations/sql/004_delegate.sql`
- Test: `internal/migrations/migrations_delegate_test.go`

**Interfaces:**
- Consumes: `migrations.Apply(ctx context.Context, db *sql.DB) error`; `database.Open(path string) (*sql.DB, error)`.
- Produces: a `delegate_iso2 TEXT` nullable column on `countries`, referencing `countries(iso2)`.

- [ ] **Step 1: Write the failing test**

Create `internal/migrations/migrations_delegate_test.go`:

```go
package migrations

import (
	"context"
	"testing"
)

func TestMigration004AddsDelegateISO2Column(t *testing.T) {
	db := applyTestSchema(t)
	ctx := context.Background()

	// Inserting a country with a delegate_iso2 value must succeed.
	_, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score, delegate_iso2)
		VALUES ('XA','XAA','Test A','Test',
			'allowed','delegated_to_foreign_authority',3,2,'FR')
	`)
	if err != nil {
		t.Fatalf("insert with delegate_iso2: %v", err)
	}

	var got *string
	if err := db.QueryRowContext(ctx,
		`SELECT delegate_iso2 FROM countries WHERE iso2='XA'`).Scan(&got); err != nil {
		t.Fatalf("select delegate_iso2: %v", err)
	}
	if got == nil || *got != "FR" {
		t.Fatalf("delegate_iso2 = %v, want \"FR\"", got)
	}

	// Default must be NULL when not supplied.
	_, err = db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score)
		VALUES ('XB','XBB','Test B','Test','allowed','unknown',0,3)
	`)
	if err != nil {
		t.Fatalf("insert without delegate_iso2: %v", err)
	}
	var nullGot *string
	if err := db.QueryRowContext(ctx,
		`SELECT delegate_iso2 FROM countries WHERE iso2='XB'`).Scan(&nullGot); err != nil {
		t.Fatalf("select null delegate_iso2: %v", err)
	}
	if nullGot != nil {
		t.Fatalf("delegate_iso2 = %v, want NULL", *nullGot)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/migrations/ -run TestMigration004 -v`
Expected: FAIL — `no such column: delegate_iso2`.

- [ ] **Step 3: Create the migration**

Create `internal/migrations/sql/004_delegate.sql`:

```sql
-- 004_delegate.sql
-- Adds the optional delegate authority pointer used by the gap scheduler to
-- select the correct foreign-search job type for delegated countries.
ALTER TABLE countries
  ADD COLUMN delegate_iso2 TEXT
    REFERENCES countries(iso2);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/migrations/ -run TestMigration004 -v`
Expected: PASS.

- [ ] **Step 5: Run the full migrations suite (checksum/name guards must stay green)**

Run: `cd control-plane && go test ./internal/migrations/...`
Expected: PASS — existing `TestApplyCreatesCompleteSchemaAndIsIdempotent` and hardening/identity tests still pass.

- [ ] **Step 6: Commit**

```bash
git add internal/migrations/sql/004_delegate.sql internal/migrations/migrations_delegate_test.go
git commit -m "feat(control-plane): migration 004 adds countries.delegate_iso2"
```

---

## Task 2: Seed reads/writes `delegate_iso2`

**Files:**
- Modify: `internal/seed/seed.go` (the `overlayEntry` struct ~line 48; the country upsert statement ~line 144; the defaults + overlay-apply block ~line 170-210)
- Modify: `internal/seed/data/country_overlays.json` (add `delegate_iso2` to the AD entry)
- Test: `internal/seed/seed_delegate_test.go`

**Interfaces:**
- Consumes: `seed.Apply(ctx, db) (Stats, error)`.
- Produces: `delegate_iso2` populated on `countries` from the overlay JSON (NULL when the overlay omits it).

- [ ] **Step 1: Write the failing test**

Create `internal/seed/seed_delegate_test.go`:

```go
package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func TestSeedPopulatesDelegateISO2(t *testing.T) {
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

	// AD overlay sets delegate_iso2 = "FR".
	var ad *string
	if err := db.QueryRowContext(ctx,
		`SELECT delegate_iso2 FROM countries WHERE iso2='AD'`).Scan(&ad); err != nil {
		t.Fatalf("select AD: %v", err)
	}
	if ad == nil || *ad != "FR" {
		t.Fatalf("AD delegate_iso2 = %v, want \"FR\"", ad)
	}

	// A country with no overlay delegate stays NULL (US has no delegate).
	var us *string
	if err := db.QueryRowContext(ctx,
		`SELECT delegate_iso2 FROM countries WHERE iso2='US'`).Scan(&us); err != nil {
		t.Fatalf("select US: %v", err)
	}
	if us != nil {
		t.Fatalf("US delegate_iso2 = %v, want NULL", *us)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/seed/ -run TestSeedPopulatesDelegateISO2 -v`
Expected: FAIL — either compile error (no `delegate_iso2` handling) or `AD delegate_iso2 = <nil>`.

- [ ] **Step 3: Add the field to `overlayEntry`**

In `internal/seed/seed.go`, add to the `overlayEntry` struct (after `ExpectedSourceQuality`):

```go
	DelegateISO2 string `json:"delegate_iso2"`
```

- [ ] **Step 4: Thread it through the country upsert**

In `seed.go`, the country `INSERT` statement and its `ExecContext` call must include `delegate_iso2`. Add `delegate_iso2` to the column list and a corresponding parameter, plus `delegate_iso2=excluded.delegate_iso2` in the `ON CONFLICT` update clause. Add a local resolved as a nullable string:

```go
	var delegateISO2 *string // declared alongside the other per-country defaults

	// inside the overlay-apply block (if o, ok := overlayMap[c.ISO2]; ok { ... }):
	if o.DelegateISO2 != "" {
		d := o.DelegateISO2
		delegateISO2 = &d
	}
```

Pass `delegateISO2` as the matching parameter in `stmtCountry.ExecContext(...)`. (Place the new column last in the column list and last in the args to minimise churn.)

- [ ] **Step 5: Set AD's delegate in the overlay JSON**

In `internal/seed/data/country_overlays.json`, add `"delegate_iso2": "FR"` to the `AD` object (it already has `coverage_status: "delegated_to_foreign_authority"`).

- [ ] **Step 6: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/seed/ -run TestSeedPopulatesDelegateISO2 -v`
Expected: PASS.

- [ ] **Step 7: Run the full seed suite**

Run: `cd control-plane && go test ./internal/seed/...`
Expected: PASS — existing seed tests unaffected.

- [ ] **Step 8: Commit**

```bash
git add internal/seed/seed.go internal/seed/data/country_overlays.json internal/seed/seed_delegate_test.go
git commit -m "feat(control-plane): seed delegate_iso2 from country overlays"
```

---

## Task 3: Allow `wayback` source type in `SourceTierAllowsType`

**Files:**
- Modify: `internal/model/types.go` (`SourceTierAllowsType`, case 5)
- Test: `internal/model/types_wayback_test.go`

**Interfaces:**
- Consumes: `model.SourceTierAllowsType(tier int, typ model.SourceType) bool`; `model.SourceWayback`.
- Produces: tier 5 now permits `wayback` (so a Wayback method-source can be seeded in Task 4).

**Why:** the schema's `sources.source_type` CHECK lists `wayback`, but `SourceTierAllowsType` maps no tier to it, so seeding any wayback source fails the seed-time tier validation. Treat Wayback as a tier-5 trusted index of archived pages.

- [ ] **Step 1: Write the failing test**

Create `internal/model/types_wayback_test.go`:

```go
package model

import "testing"

func TestSourceTierAllowsWaybackAtTier5(t *testing.T) {
	if !SourceTierAllowsType(5, SourceWayback) {
		t.Fatal("tier 5 should allow wayback source type")
	}
	if SourceTierAllowsType(1, SourceWayback) {
		t.Fatal("tier 1 should not allow wayback source type")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/model/ -run TestSourceTierAllowsWaybackAtTier5 -v`
Expected: FAIL — "tier 5 should allow wayback source type".

- [ ] **Step 3: Extend case 5**

In `internal/model/types.go`, change case 5 of `SourceTierAllowsType`:

```go
	case 5:
		return typ == SourceTrustedIndex || typ == SourceWayback
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/model/ -run TestSourceTierAllowsWaybackAtTier5 -v`
Expected: PASS.

- [ ] **Step 5: Run full model suite**

Run: `cd control-plane && go test ./internal/model/...`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add internal/model/types.go internal/model/types_wayback_test.go
git commit -m "fix(control-plane): allow wayback source type at tier 5"
```

---

## Task 4: Add method-source rows to `sources.json`

**Files:**
- Modify: `internal/seed/data/sources.json`
- Test: `internal/seed/seed_method_sources_test.go`

**Interfaces:**
- Consumes: `seed.Apply(ctx, db) (Stats, error)`.
- Produces: one resolvable `sources` row per job-type "method" channel, each with a unique `name` the planner looks up (Task 8). Names are exact and stable:
  - `Authority Health Check (method)` — `regulator`, tier 4
  - `Authority Archive Crawl (method)` — `regulator`, tier 4
  - `Wayback Machine CDX (method)` — `wayback`, tier 5
  - `Scholarly PDF Discovery (method)` — `trusted_index`, tier 5
  - `Direct Authority Request (method)` — `ministry`, tier 4
  - `NTSB Foreign Investigations (method)` — `official_foreign_accredited_rep`, tier 2
  - `BEA Foreign Investigations (method)` — `official_foreign_accredited_rep`, tier 2
  - `ATSB Foreign Investigations (method)` — `official_foreign_accredited_rep`, tier 2
  - (`icao_elibrary_search` reuses the existing `ICAO e-Library Final Reports` row — do not add a new one.)

- [ ] **Step 1: Write the failing test**

Create `internal/seed/seed_method_sources_test.go`:

```go
package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func TestSeedHasMethodSources(t *testing.T) {
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

	wantNames := []string{
		"Authority Health Check (method)",
		"Authority Archive Crawl (method)",
		"Wayback Machine CDX (method)",
		"Scholarly PDF Discovery (method)",
		"Direct Authority Request (method)",
		"NTSB Foreign Investigations (method)",
		"BEA Foreign Investigations (method)",
		"ATSB Foreign Investigations (method)",
	}
	for _, name := range wantNames {
		var id int
		if err := db.QueryRowContext(ctx,
			`SELECT id FROM sources WHERE name = ?`, name).Scan(&id); err != nil {
			t.Errorf("method source %q not seeded: %v", name, err)
		}
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/seed/ -run TestSeedHasMethodSources -v`
Expected: FAIL — method source rows not seeded.

- [ ] **Step 3: Add the rows**

Append these objects to the array in `internal/seed/data/sources.json` (match existing field shape; `robots_policy: "allowed"`):

```json
{ "name": "Authority Health Check (method)", "url": "https://control-plane.local/method/health-check", "canonical_url": "https://control-plane.local/method/health-check", "source_type": "regulator", "source_tier": 4, "robots_policy": "allowed", "copyright_policy_notes": "Internal method channel: authority liveness probe" },
{ "name": "Authority Archive Crawl (method)", "url": "https://control-plane.local/method/archive-crawl", "canonical_url": "https://control-plane.local/method/archive-crawl", "source_type": "regulator", "source_tier": 4, "robots_policy": "allowed", "copyright_policy_notes": "Internal method channel: official archive crawl" },
{ "name": "Wayback Machine CDX (method)", "url": "https://web.archive.org/cdx/search/cdx", "canonical_url": "https://web.archive.org/cdx/search/cdx", "source_type": "wayback", "source_tier": 5, "robots_policy": "allowed", "copyright_policy_notes": "Internet Archive CDX index of archived regulator pages" },
{ "name": "Scholarly PDF Discovery (method)", "url": "https://control-plane.local/method/pdf-discovery", "canonical_url": "https://control-plane.local/method/pdf-discovery", "source_type": "trusted_index", "source_tier": 5, "robots_policy": "allowed", "copyright_policy_notes": "Internal method channel: scholarly/Google-Scholar PDF discovery" },
{ "name": "Direct Authority Request (method)", "url": "https://control-plane.local/method/direct-request", "canonical_url": "https://control-plane.local/method/direct-request", "source_type": "ministry", "source_tier": 4, "robots_policy": "allowed", "copyright_policy_notes": "Internal method channel: direct authority data request" },
{ "name": "NTSB Foreign Investigations (method)", "url": "https://www.ntsb.gov/investigations/Pages/foreign.aspx", "canonical_url": "https://www.ntsb.gov/investigations/Pages/foreign.aspx", "source_type": "official_foreign_accredited_rep", "source_tier": 2, "robots_policy": "allowed", "copyright_policy_notes": "NTSB as accredited representative; US government public domain" },
{ "name": "BEA Foreign Investigations (method)", "url": "https://www.bea.aero/en/investigation-reports/foreign/", "canonical_url": "https://www.bea.aero/en/investigation-reports/foreign/", "source_type": "official_foreign_accredited_rep", "source_tier": 2, "robots_policy": "allowed", "copyright_policy_notes": "BEA as accredited representative; public access" },
{ "name": "ATSB Foreign Investigations (method)", "url": "https://www.atsb.gov.au/aviation/foreign/", "canonical_url": "https://www.atsb.gov.au/aviation/foreign/", "source_type": "official_foreign_accredited_rep", "source_tier": 2, "robots_policy": "allowed", "copyright_policy_notes": "ATSB as accredited representative; Australian government copyright" }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/seed/ -run TestSeedHasMethodSources -v`
Expected: PASS.

- [ ] **Step 5: Run full seed suite (tier validation must accept all new rows)**

Run: `cd control-plane && go test ./internal/seed/...`
Expected: PASS — no `tier N does not allow type` error (Task 3 enabled wayback@5).

- [ ] **Step 6: Commit**

```bash
git add internal/seed/data/sources.json internal/seed/seed_method_sources_test.go
git commit -m "feat(control-plane): seed per-job-type method sources"
```

---

## Task 5: First overlay batch — 20 RAIO member states

**Files:**
- Modify: `internal/seed/data/country_overlays.json`
- Test: `internal/seed/seed_raio_batch_test.go`

**Interfaces:**
- Consumes: `seed.Apply(ctx, db) (Stats, error)`.
- Produces: 20 RAIO member countries with `country_group`, `coverage_status='regional_raio'`, non-zero `priority_score`.

**Data (authored, verified against regional-body membership + known archive state):**
The 20 members are ECCAA (DM, GD, KN, LC, VC), BAGAIA (CV, GM, GH, GN, LR, NG, SL), IAC (RU, AM, AZ, BY, KZ, KG, TJ, TM). Each gets `coverage_status: "regional_raio"`, `refresh_cadence: "quarterly"`, and authored scores. Use these values (group/score/effort/expected_records/quality), which a reviewer can sanity-check against each state's aviation activity:

| iso2 | group | cov_score | effort | exp_records | exp_quality |
|---|---|---|---|---|---|
| RU | C2 | 2 | 4 | 400 | 3 |
| NG | C2 | 2 | 4 | 120 | 3 |
| KZ | C2 | 2 | 3 | 90 | 3 |
| GH | C3 | 2 | 3 | 40 | 3 |
| AZ | C3 | 2 | 3 | 35 | 3 |
| BY | C3 | 2 | 3 | 35 | 3 |
| AM | C3 | 2 | 3 | 25 | 3 |
| KG | C3 | 2 | 3 | 25 | 3 |
| TJ | C3 | 2 | 3 | 20 | 3 |
| TM | C3 | 2 | 4 | 20 | 2 |
| LR | C3 | 2 | 4 | 20 | 2 |
| SL | C3 | 2 | 4 | 18 | 2 |
| GN | C3 | 2 | 4 | 18 | 2 |
| GM | D | 1 | 4 | 12 | 2 |
| CV | D | 1 | 3 | 12 | 3 |
| LC | D | 1 | 3 | 12 | 3 |
| GD | D | 1 | 3 | 10 | 3 |
| VC | D | 1 | 3 | 10 | 3 |
| DM | D | 1 | 3 | 8 | 3 |
| KN | D | 1 | 3 | 8 | 3 |

- [ ] **Step 1: Write the failing test**

Create `internal/seed/seed_raio_batch_test.go`:

```go
package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func TestSeedRAIOBatch(t *testing.T) {
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

	members := []string{
		"DM", "GD", "KN", "LC", "VC",
		"CV", "GM", "GH", "GN", "LR", "NG", "SL",
		"RU", "AM", "AZ", "BY", "KZ", "KG", "TJ", "TM",
	}
	for _, iso2 := range members {
		var coverage string
		var priority float64
		var group *string
		if err := db.QueryRowContext(ctx,
			`SELECT coverage_status, priority_score, country_group
			   FROM countries WHERE iso2 = ?`, iso2).
			Scan(&coverage, &priority, &group); err != nil {
			t.Errorf("%s: %v", iso2, err)
			continue
		}
		if coverage != "regional_raio" {
			t.Errorf("%s coverage_status = %q, want regional_raio", iso2, coverage)
		}
		if priority <= 0 {
			t.Errorf("%s priority_score = %v, want > 0", iso2, priority)
		}
		if group == nil {
			t.Errorf("%s country_group is NULL, want C/D", iso2)
		}
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/seed/ -run TestSeedRAIOBatch -v`
Expected: FAIL — members default to `coverage_status='unknown'`, `priority_score=0`, `country_group=NULL`.

- [ ] **Step 3: Add the 20 overlay rows**

Add 20 objects to `internal/seed/data/country_overlays.json`, one per row of the table above. Shape (example for NG):

```json
{ "iso2": "NG", "group": "C2", "policy_status": "allowed", "coverage_status": "regional_raio", "coverage_score": 2, "effort_score": 4, "expected_records": 120, "expected_source_quality": 3, "refresh_cadence": "quarterly", "notes": "BAGAIA member; route via regional RAIO + Wayback." }
```

Do not set `delegate_iso2` for these (regional, not delegated).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/seed/ -run TestSeedRAIOBatch -v`
Expected: PASS.

- [ ] **Step 5: Run full seed + export suites (export round-trips the new rows)**

Run: `cd control-plane && go test ./internal/seed/... ./internal/export/...`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add internal/seed/data/country_overlays.json internal/seed/seed_raio_batch_test.go
git commit -m "feat(control-plane): seed first overlay batch (20 RAIO member states)"
```

---

## Task 6: Planner mapping table

**Files:**
- Create: `internal/planner/mapping.go`
- Test: `internal/planner/mapping_test.go`

**Interfaces:**
- Consumes: `model.CoverageStatus`, `model.CrawlJobType` constants.
- Produces:
  - `func JobTypesFor(status model.CoverageStatus, delegateISO2 string) []model.CrawlJobType` — returns the ordered, deduplicated job types for a coverage status. For `delegated_to_foreign_authority`, prepends the foreign-search type chosen by `delegateISO2` (US→ntsb, FR→bea, AU→atsb); unknown/empty delegate falls back to `icao_elibrary_search` + `wayback_cdx` only.
  - `func foreignSearchFor(delegateISO2 string) (model.CrawlJobType, bool)` — the delegate→foreign-search lookup.

- [ ] **Step 1: Write the failing test**

Create `internal/planner/mapping_test.go`:

```go
package planner

import (
	"reflect"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

func TestJobTypesFor(t *testing.T) {
	cases := []struct {
		name     string
		status   model.CoverageStatus
		delegate string
		want     []model.CrawlJobType
	}{
		{"direct_archive", model.CoverageStatusDirectPublicArchive, "",
			[]model.CrawlJobType{model.CrawlJobTypeAuthorityHealthCheck, model.CrawlJobTypeArchiveCrawl}},
		{"unstable", model.CoverageStatusSourceExistsUnstable, "",
			[]model.CrawlJobType{model.CrawlJobTypeArchiveCrawl, model.CrawlJobTypeWaybackCDX}},
		{"regional", model.CoverageStatusRegionalRAIO, "",
			[]model.CrawlJobType{model.CrawlJobTypeArchiveCrawl, model.CrawlJobTypeWaybackCDX}},
		{"contact_only", model.CoverageStatusOfficialContactOnly, "",
			[]model.CrawlJobType{model.CrawlJobTypeDirectRequestNeeded, model.CrawlJobTypeICAOELibrarySearch}},
		{"no_archive", model.CoverageStatusNoPublicArchive, "",
			[]model.CrawlJobType{model.CrawlJobTypeWaybackCDX, model.CrawlJobTypePDFDiscovery, model.CrawlJobTypeDirectRequestNeeded}},
		{"unknown", model.CoverageStatusUnknown, "",
			[]model.CrawlJobType{model.CrawlJobTypeICAOELibrarySearch, model.CrawlJobTypeWaybackCDX}},
		{"delegated_FR", model.CoverageStatusDelegatedToForeign, "FR",
			[]model.CrawlJobType{model.CrawlJobTypeBEAForeignSearch, model.CrawlJobTypeICAOELibrarySearch}},
		{"delegated_US", model.CoverageStatusDelegatedToForeign, "US",
			[]model.CrawlJobType{model.CrawlJobTypeNTSBForeignSearch, model.CrawlJobTypeICAOELibrarySearch}},
		{"delegated_unknown", model.CoverageStatusDelegatedToForeign, "ES",
			[]model.CrawlJobType{model.CrawlJobTypeICAOELibrarySearch, model.CrawlJobTypeWaybackCDX}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := JobTypesFor(tc.status, tc.delegate)
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("JobTypesFor(%q,%q) = %v, want %v", tc.status, tc.delegate, got, tc.want)
			}
		})
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/planner/ -run TestJobTypesFor -v`
Expected: FAIL — package/`JobTypesFor` undefined.

- [ ] **Step 3: Implement the mapping**

Create `internal/planner/mapping.go`:

```go
// Package planner turns the control-plane's coverage map into ranked crawl jobs.
package planner

import "github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"

// foreignSearchByDelegate maps an accredited-representative state to its
// foreign-search job type. Only the three states with a dedicated job type are
// present; any other delegate uses the safe fallback in JobTypesFor.
var foreignSearchByDelegate = map[string]model.CrawlJobType{
	"US": model.CrawlJobTypeNTSBForeignSearch,
	"FR": model.CrawlJobTypeBEAForeignSearch,
	"AU": model.CrawlJobTypeATSBSearch,
}

func foreignSearchFor(delegateISO2 string) (model.CrawlJobType, bool) {
	jt, ok := foreignSearchByDelegate[delegateISO2]
	return jt, ok
}

// staticMapping is the coverage_status → job types table for every status that
// does not depend on the delegate.
var staticMapping = map[model.CoverageStatus][]model.CrawlJobType{
	model.CoverageStatusDirectPublicArchive: {model.CrawlJobTypeAuthorityHealthCheck, model.CrawlJobTypeArchiveCrawl},
	model.CoverageStatusSourceExistsUnstable: {model.CrawlJobTypeArchiveCrawl, model.CrawlJobTypeWaybackCDX},
	model.CoverageStatusRegionalRAIO:         {model.CrawlJobTypeArchiveCrawl, model.CrawlJobTypeWaybackCDX},
	model.CoverageStatusOfficialContactOnly:  {model.CrawlJobTypeDirectRequestNeeded, model.CrawlJobTypeICAOELibrarySearch},
	model.CoverageStatusNoPublicArchive:      {model.CrawlJobTypeWaybackCDX, model.CrawlJobTypePDFDiscovery, model.CrawlJobTypeDirectRequestNeeded},
	model.CoverageStatusUnknown:              {model.CrawlJobTypeICAOELibrarySearch, model.CrawlJobTypeWaybackCDX},
}

// delegateFallback is used when a delegated country has no recognised delegate.
var delegateFallback = []model.CrawlJobType{model.CrawlJobTypeICAOELibrarySearch, model.CrawlJobTypeWaybackCDX}

// JobTypesFor returns the job types to schedule for a country's coverage status.
// policy_excluded returns nil (such countries are filtered out before mapping).
func JobTypesFor(status model.CoverageStatus, delegateISO2 string) []model.CrawlJobType {
	if status == model.CoverageStatusDelegatedToForeign {
		if jt, ok := foreignSearchFor(delegateISO2); ok {
			return []model.CrawlJobType{jt, model.CrawlJobTypeICAOELibrarySearch}
		}
		return append([]model.CrawlJobType(nil), delegateFallback...)
	}
	if jts, ok := staticMapping[status]; ok {
		return append([]model.CrawlJobType(nil), jts...)
	}
	return nil
}
```

> Verify the constant name `model.CoverageStatusDelegatedToForeign` matches `internal/model/enums.go`. If the file declares it as `CoverageStatusDelegatedToForeign`, use that; this plan assumes that exact name (it is defined in enums.go).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/planner/ -run TestJobTypesFor -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/planner/mapping.go internal/planner/mapping_test.go
git commit -m "feat(control-plane): planner coverage_status to job_type mapping"
```

---

## Task 7: Planner candidate ranking query

**Files:**
- Modify: `internal/planner/planner.go` (create)
- Test: `internal/planner/planner_query_test.go`

**Interfaces:**
- Consumes: `*sql.DB`; the `countries` table.
- Produces:
  - `type Candidate struct { ISO2 string; CoverageStatus model.CoverageStatus; PriorityScore float64; DelegateISO2 string; RefreshCadence string; CountryID int64 }`
  - `func Candidates(ctx context.Context, db *sql.DB) ([]Candidate, error)` — non-excluded countries ordered `priority_score DESC, iso2 ASC`.

- [ ] **Step 1: Write the failing test**

Create `internal/planner/planner_query_test.go` (this file also defines the shared `seededDB` helper reused by Tasks 8–11):

```go
package planner

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/seed"
)

func seededDB(t *testing.T) (context.Context, *sql.DB) {
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

func TestCandidatesRankedAndFiltered(t *testing.T) {
	ctx, db := seededDB(t)

	cands, err := Candidates(ctx, db)
	if err != nil {
		t.Fatal(err)
	}
	if len(cands) == 0 {
		t.Fatal("no candidates")
	}

	// Excluded countries (AF, KP, SY) must never appear.
	for _, c := range cands {
		if c.ISO2 == "AF" || c.ISO2 == "KP" || c.ISO2 == "SY" {
			t.Fatalf("excluded country %s present in candidates", c.ISO2)
		}
	}

	// Ordering: priority_score descending, iso2 ascending tiebreak.
	for i := 1; i < len(cands); i++ {
		prev, cur := cands[i-1], cands[i]
		if cur.PriorityScore > prev.PriorityScore {
			t.Fatalf("not sorted by priority desc at %d: %v > %v", i, cur.PriorityScore, prev.PriorityScore)
		}
		if cur.PriorityScore == prev.PriorityScore && cur.ISO2 < prev.ISO2 {
			t.Fatalf("tiebreak not iso2 asc at %d: %s < %s", i, cur.ISO2, prev.ISO2)
		}
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/planner/ -run TestCandidatesRanked -v`
Expected: FAIL — `Candidates` undefined.

- [ ] **Step 3: Implement the query**

Create `internal/planner/planner.go`:

```go
package planner

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

// Candidate is a non-excluded country eligible for scheduling.
type Candidate struct {
	CountryID      int64
	ISO2           string
	CoverageStatus model.CoverageStatus
	PriorityScore  float64
	DelegateISO2   string
	RefreshCadence string
}

// Candidates returns all schedulable countries (policy_status != 'excluded')
// ordered by priority_score DESC, iso2 ASC.
func Candidates(ctx context.Context, db *sql.DB) ([]Candidate, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT id, iso2, coverage_status, priority_score,
		       COALESCE(delegate_iso2, ''), COALESCE(refresh_cadence, '')
		  FROM countries
		 WHERE policy_status != 'excluded'
		 ORDER BY priority_score DESC, iso2 ASC
	`)
	if err != nil {
		return nil, fmt.Errorf("planner: query candidates: %w", err)
	}
	defer rows.Close()

	var out []Candidate
	for rows.Next() {
		var c Candidate
		var cov string
		if err := rows.Scan(&c.CountryID, &c.ISO2, &cov, &c.PriorityScore,
			&c.DelegateISO2, &c.RefreshCadence); err != nil {
			return nil, fmt.Errorf("planner: scan candidate: %w", err)
		}
		c.CoverageStatus = model.CoverageStatus(cov)
		out = append(out, c)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("planner: iterate candidates: %w", err)
	}
	return out, nil
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/planner/ -run TestCandidatesRanked -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/planner/planner.go internal/planner/planner_query_test.go
git commit -m "feat(control-plane): planner candidate ranking query"
```

---

## Task 8: Planner source resolution

**Files:**
- Modify: `internal/planner/planner.go`
- Test: `internal/planner/planner_source_test.go`

**Interfaces:**
- Consumes: `*sql.DB`; the `sources` table; `model.CrawlJobType`.
- Produces:
  - `func (r *SourceResolver) Resolve(jobType model.CrawlJobType) (int64, bool)` — returns the method-source id for a job type, false if none.
  - `func NewSourceResolver(ctx context.Context, db *sql.DB) (*SourceResolver, error)` — loads the job-type→source-id map once.

The fixed job-type → source-name mapping (names match Task 4 / existing rows):

| job_type | source name |
|---|---|
| `authority_health_check` | `Authority Health Check (method)` |
| `archive_crawl` | `Authority Archive Crawl (method)` |
| `wayback_cdx` | `Wayback Machine CDX (method)` |
| `pdf_discovery` | `Scholarly PDF Discovery (method)` |
| `icao_elibrary_search` | `ICAO e-Library Final Reports` |
| `direct_request_needed` | `Direct Authority Request (method)` |
| `ntsb_foreign_search` | `NTSB Foreign Investigations (method)` |
| `bea_foreign_search` | `BEA Foreign Investigations (method)` |
| `atsb_search` | `ATSB Foreign Investigations (method)` |

- [ ] **Step 1: Write the failing test**

Create `internal/planner/planner_source_test.go`:

```go
package planner

import (
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

func TestSourceResolverResolvesEveryJobType(t *testing.T) {
	ctx, db := seededDB(t)
	r, err := NewSourceResolver(ctx, db)
	if err != nil {
		t.Fatal(err)
	}
	jobTypes := []model.CrawlJobType{
		model.CrawlJobTypeAuthorityHealthCheck,
		model.CrawlJobTypeArchiveCrawl,
		model.CrawlJobTypeWaybackCDX,
		model.CrawlJobTypePDFDiscovery,
		model.CrawlJobTypeICAOELibrarySearch,
		model.CrawlJobTypeDirectRequestNeeded,
		model.CrawlJobTypeNTSBForeignSearch,
		model.CrawlJobTypeBEAForeignSearch,
		model.CrawlJobTypeATSBSearch,
	}
	for _, jt := range jobTypes {
		id, ok := r.Resolve(jt)
		if !ok || id <= 0 {
			t.Errorf("Resolve(%q) = (%d,%v), want a real source id", jt, id, ok)
		}
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/planner/ -run TestSourceResolver -v`
Expected: FAIL — `NewSourceResolver` undefined.

- [ ] **Step 3: Implement the resolver**

Append to `internal/planner/planner.go`:

```go
// sourceNameByJobType maps each job type to the name of the sources row that
// represents its acquisition channel. Names must match the seeded rows.
var sourceNameByJobType = map[model.CrawlJobType]string{
	model.CrawlJobTypeAuthorityHealthCheck: "Authority Health Check (method)",
	model.CrawlJobTypeArchiveCrawl:         "Authority Archive Crawl (method)",
	model.CrawlJobTypeWaybackCDX:           "Wayback Machine CDX (method)",
	model.CrawlJobTypePDFDiscovery:         "Scholarly PDF Discovery (method)",
	model.CrawlJobTypeICAOELibrarySearch:   "ICAO e-Library Final Reports",
	model.CrawlJobTypeDirectRequestNeeded:  "Direct Authority Request (method)",
	model.CrawlJobTypeNTSBForeignSearch:    "NTSB Foreign Investigations (method)",
	model.CrawlJobTypeBEAForeignSearch:     "BEA Foreign Investigations (method)",
	model.CrawlJobTypeATSBSearch:           "ATSB Foreign Investigations (method)",
}

// SourceResolver maps a job type to a sources.id, loaded once from the DB.
type SourceResolver struct {
	byJobType map[model.CrawlJobType]int64
}

// NewSourceResolver loads the job-type → source-id map. A job type whose source
// row is missing is simply absent from the map (Resolve returns false).
func NewSourceResolver(ctx context.Context, db *sql.DB) (*SourceResolver, error) {
	r := &SourceResolver{byJobType: make(map[model.CrawlJobType]int64, len(sourceNameByJobType))}
	for jt, name := range sourceNameByJobType {
		var id int64
		err := db.QueryRowContext(ctx, `SELECT id FROM sources WHERE name = ?`, name).Scan(&id)
		if err == sql.ErrNoRows {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("planner: resolve source %q: %w", name, err)
		}
		r.byJobType[jt] = id
	}
	return r, nil
}

// Resolve returns the source id for a job type, or false if none is mapped.
func (r *SourceResolver) Resolve(jobType model.CrawlJobType) (int64, bool) {
	id, ok := r.byJobType[jobType]
	return id, ok
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/planner/ -run TestSourceResolver -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/planner/planner.go internal/planner/planner_source_test.go
git commit -m "feat(control-plane): planner job-type to source resolution"
```

---

## Task 9: Idempotency + cadence gate

**Files:**
- Modify: `internal/planner/planner.go`
- Test: `internal/planner/planner_gate_test.go`

**Interfaces:**
- Consumes: `*sql.DB`; the `crawl_jobs` table.
- Produces:
  - `type JobState struct { HasActive bool; LastFinishedAtMs int64; HasTerminal bool }`
  - `func JobStateFor(ctx context.Context, db *sql.DB, countryID int64, jobType model.CrawlJobType) (JobState, error)` — active = any pending/running row; terminal = latest finished_at among success/failed/partial.
  - `func cadenceDurationMs(cadence string) int64` — `weekly`→7d, `biweekly`→14d, `monthly`→30d, `quarterly`→90d, default 90d (all in milliseconds).
  - `func cadenceElapsed(nowMs, lastFinishedMs int64, cadence string) bool`.

- [ ] **Step 1: Write the failing test**

Create `internal/planner/planner_gate_test.go`:

```go
package planner

import (
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

func TestCadenceDurationMs(t *testing.T) {
	day := int64(24 * 60 * 60 * 1000)
	cases := map[string]int64{
		"weekly": 7 * day, "biweekly": 14 * day, "monthly": 30 * day,
		"quarterly": 90 * day, "": 90 * day, "garbage": 90 * day,
	}
	for c, want := range cases {
		if got := cadenceDurationMs(c); got != want {
			t.Errorf("cadenceDurationMs(%q) = %d, want %d", c, got, want)
		}
	}
}

func TestCadenceElapsed(t *testing.T) {
	day := int64(24 * 60 * 60 * 1000)
	now := int64(1_000_000_000_000)
	if cadenceElapsed(now, now-10*day, "quarterly") {
		t.Error("10 days < quarterly: should NOT be elapsed")
	}
	if !cadenceElapsed(now, now-100*day, "quarterly") {
		t.Error("100 days > quarterly: should be elapsed")
	}
}

func TestJobStateFor(t *testing.T) {
	ctx, db := seededDB(t)
	// Use a real country + source so FKs hold.
	var countryID, sourceID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='NG'`).Scan(&countryID); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT id FROM sources WHERE name='Wayback Machine CDX (method)'`).Scan(&sourceID); err != nil {
		t.Fatal(err)
	}

	// No jobs yet → no active, no terminal.
	st, err := JobStateFor(ctx, db, countryID, model.CrawlJobTypeWaybackCDX)
	if err != nil {
		t.Fatal(err)
	}
	if st.HasActive || st.HasTerminal {
		t.Fatalf("fresh state = %+v, want empty", st)
	}

	// Insert a pending job → active.
	if _, err := db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'pending')`, sourceID, countryID); err != nil {
		t.Fatal(err)
	}
	st, _ = JobStateFor(ctx, db, countryID, model.CrawlJobTypeWaybackCDX)
	if !st.HasActive {
		t.Fatal("expected HasActive after inserting pending job")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/planner/ -run 'TestCadence|TestJobStateFor' -v`
Expected: FAIL — undefined `cadenceDurationMs` / `JobStateFor`.

- [ ] **Step 3: Implement the gate**

Append to `internal/planner/planner.go`:

```go
const dayMs = int64(24 * 60 * 60 * 1000)

func cadenceDurationMs(cadence string) int64 {
	switch cadence {
	case "weekly":
		return 7 * dayMs
	case "biweekly":
		return 14 * dayMs
	case "monthly":
		return 30 * dayMs
	case "quarterly":
		return 90 * dayMs
	default:
		return 90 * dayMs
	}
}

func cadenceElapsed(nowMs, lastFinishedMs int64, cadence string) bool {
	return nowMs-lastFinishedMs >= cadenceDurationMs(cadence)
}

// JobState summarises the existing crawl_jobs for a (country, job_type) pair.
type JobState struct {
	HasActive        bool  // a pending or running job exists
	HasTerminal      bool  // at least one success/failed/partial job exists
	LastFinishedAtMs int64 // max(finished_at) among terminal jobs (0 if none)
}

// JobStateFor inspects crawl_jobs for the given country and job type.
func JobStateFor(ctx context.Context, db *sql.DB, countryID int64, jobType model.CrawlJobType) (JobState, error) {
	var st JobState
	var active int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM crawl_jobs
		 WHERE country_id = ? AND job_type = ? AND status IN ('pending','running')
	`, countryID, string(jobType)).Scan(&active); err != nil {
		return st, fmt.Errorf("planner: job active count: %w", err)
	}
	st.HasActive = active > 0

	var cnt int
	var maxFinished sql.NullInt64
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*), MAX(finished_at) FROM crawl_jobs
		 WHERE country_id = ? AND job_type = ?
		   AND status IN ('success','failed','partial')
	`, countryID, string(jobType)).Scan(&cnt, &maxFinished); err != nil {
		return st, fmt.Errorf("planner: job terminal state: %w", err)
	}
	st.HasTerminal = cnt > 0
	if maxFinished.Valid {
		st.LastFinishedAtMs = maxFinished.Int64
	}
	return st, nil
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/planner/ -run 'TestCadence|TestJobStateFor' -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/planner/planner.go internal/planner/planner_gate_test.go
git commit -m "feat(control-plane): planner idempotency and cadence gate"
```

---

## Task 10: `BuildPlan` — assemble ranked decisions

**Files:**
- Modify: `internal/planner/planner.go`
- Test: `internal/planner/planner_build_test.go`

**Interfaces:**
- Consumes: `Candidates`, `JobTypesFor`, `NewSourceResolver`, `JobStateFor`, `cadenceElapsed`.
- Produces:
  - `type Decision string` with consts `DecisionWouldEnqueue = "would_enqueue"`, `DecisionSkippedActive = "skipped_active"`, `DecisionSkippedCadence = "skipped_cadence"`, `DecisionSkippedNoSource = "skipped_no_source"`.
  - `type PlannedJob struct { ISO2 string; PriorityScore float64; CoverageStatus model.CoverageStatus; JobType model.CrawlJobType; SourceID int64; Decision Decision; CountryID int64 }`
  - `type Plan struct { GeneratedAt time.Time; CandidateCountries int; JobsPlanned int; Jobs []PlannedJob; Warnings []string }`
  - `func BuildPlan(ctx context.Context, db *sql.DB, nowMs int64, limit int) (Plan, error)` — limit ≤ 0 means no cap. `JobsPlanned` counts only `would_enqueue` decisions.

- [ ] **Step 1: Write the failing test**

Create `internal/planner/planner_build_test.go`:

```go
package planner

import "testing"

func TestBuildPlanProducesDecisions(t *testing.T) {
	ctx, db := seededDB(t)
	nowMs := int64(1_700_000_000_000)

	plan, err := BuildPlan(ctx, db, nowMs, 0)
	if err != nil {
		t.Fatal(err)
	}
	if plan.CandidateCountries == 0 || len(plan.Jobs) == 0 {
		t.Fatalf("empty plan: %+v", plan)
	}
	// All decisions on a fresh DB are would_enqueue (no existing jobs).
	for _, j := range plan.Jobs {
		if j.Decision != DecisionWouldEnqueue {
			t.Fatalf("%s/%s decision = %q, want would_enqueue", j.ISO2, j.JobType, j.Decision)
		}
		if j.SourceID <= 0 {
			t.Fatalf("%s/%s has no source id", j.ISO2, j.JobType)
		}
	}
	if plan.JobsPlanned != len(plan.Jobs) {
		t.Fatalf("JobsPlanned %d != len(Jobs) %d on fresh DB", plan.JobsPlanned, len(plan.Jobs))
	}

	// NG (regional_raio) must yield archive_crawl + wayback_cdx.
	var ngTypes []string
	for _, j := range plan.Jobs {
		if j.ISO2 == "NG" {
			ngTypes = append(ngTypes, string(j.JobType))
		}
	}
	if len(ngTypes) != 2 {
		t.Fatalf("NG job types = %v, want 2 (archive_crawl, wayback_cdx)", ngTypes)
	}
}

func TestBuildPlanRespectsLimit(t *testing.T) {
	ctx, db := seededDB(t)
	plan, err := BuildPlan(ctx, db, int64(1_700_000_000_000), 3)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, j := range plan.Jobs {
		seen[j.ISO2] = true
	}
	if len(seen) > 3 {
		t.Fatalf("limit 3 but %d countries in plan", len(seen))
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/planner/ -run TestBuildPlan -v`
Expected: FAIL — `BuildPlan` undefined.

- [ ] **Step 3: Implement `BuildPlan`**

Append to `internal/planner/planner.go` (add `"time"` to the import block):

```go
// Decision records why a candidate job is or is not enqueued.
type Decision string

const (
	DecisionWouldEnqueue    Decision = "would_enqueue"
	DecisionSkippedActive   Decision = "skipped_active"
	DecisionSkippedCadence  Decision = "skipped_cadence"
	DecisionSkippedNoSource Decision = "skipped_no_source"
)

// PlannedJob is one (country, job_type) decision.
type PlannedJob struct {
	CountryID      int64                `json:"-"`
	ISO2           string               `json:"iso2"`
	PriorityScore  float64              `json:"priority_score"`
	CoverageStatus model.CoverageStatus `json:"coverage_status"`
	JobType        model.CrawlJobType   `json:"job_type"`
	SourceID       int64                `json:"source_id"`
	Decision       Decision             `json:"decision"`
}

// Plan is the full scheduling plan.
type Plan struct {
	GeneratedAt        time.Time    `json:"generated_at"`
	CandidateCountries int          `json:"candidate_countries"`
	JobsPlanned        int          `json:"jobs_planned"`
	Jobs               []PlannedJob `json:"jobs"`
	Warnings           []string     `json:"warnings"`
}

// BuildPlan ranks gaps and produces a decision per (country, job_type). It does
// not write anything; pass the result to Enqueue to persist would_enqueue rows.
// limit <= 0 means no country cap.
func BuildPlan(ctx context.Context, db *sql.DB, nowMs int64, limit int) (Plan, error) {
	cands, err := Candidates(ctx, db)
	if err != nil {
		return Plan{}, err
	}
	if limit > 0 && len(cands) > limit {
		cands = cands[:limit]
	}
	resolver, err := NewSourceResolver(ctx, db)
	if err != nil {
		return Plan{}, err
	}

	plan := Plan{
		GeneratedAt:        time.UnixMilli(nowMs).UTC(),
		CandidateCountries: len(cands),
		Warnings:           []string{},
		Jobs:               []PlannedJob{},
	}

	for _, c := range cands {
		for _, jt := range JobTypesFor(c.CoverageStatus, c.DelegateISO2) {
			pj := PlannedJob{
				CountryID:      c.CountryID,
				ISO2:           c.ISO2,
				PriorityScore:  c.PriorityScore,
				CoverageStatus: c.CoverageStatus,
				JobType:        jt,
			}
			sourceID, ok := resolver.Resolve(jt)
			if !ok {
				pj.Decision = DecisionSkippedNoSource
				plan.Warnings = append(plan.Warnings,
					fmt.Sprintf("%s/%s: no source resolved", c.ISO2, jt))
				plan.Jobs = append(plan.Jobs, pj)
				continue
			}
			pj.SourceID = sourceID

			state, err := JobStateFor(ctx, db, c.CountryID, jt)
			if err != nil {
				return Plan{}, err
			}
			switch {
			case state.HasActive:
				pj.Decision = DecisionSkippedActive
			case state.HasTerminal && !cadenceElapsed(nowMs, state.LastFinishedAtMs, c.RefreshCadence):
				pj.Decision = DecisionSkippedCadence
			default:
				pj.Decision = DecisionWouldEnqueue
				plan.JobsPlanned++
			}
			plan.Jobs = append(plan.Jobs, pj)
		}
	}
	return plan, nil
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/planner/ -run TestBuildPlan -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/planner/planner.go internal/planner/planner_build_test.go
git commit -m "feat(control-plane): planner BuildPlan assembles ranked decisions"
```

---

## Task 11: `Enqueue` — persist `would_enqueue` rows

**Files:**
- Modify: `internal/planner/planner.go`
- Test: `internal/planner/planner_enqueue_test.go`

**Interfaces:**
- Consumes: `*sql.DB`, `Plan`.
- Produces:
  - `func Enqueue(ctx context.Context, db *sql.DB, plan Plan) (int, error)` — inserts one `crawl_jobs` row (`status='pending'`) per `would_enqueue` decision inside a single transaction; returns the count inserted.

- [ ] **Step 1: Write the failing test**

Create `internal/planner/planner_enqueue_test.go`:

```go
package planner

import (
	"context"
	"database/sql"
	"testing"
)

func countJobs(t *testing.T, ctx context.Context, db *sql.DB) int {
	t.Helper()
	var n int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM crawl_jobs`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestEnqueueInsertsAndIsIdempotent(t *testing.T) {
	ctx, db := seededDB(t)
	nowMs := int64(1_700_000_000_000)

	plan, err := BuildPlan(ctx, db, nowMs, 0)
	if err != nil {
		t.Fatal(err)
	}
	inserted, err := Enqueue(ctx, db, plan)
	if err != nil {
		t.Fatal(err)
	}
	if inserted != plan.JobsPlanned || inserted == 0 {
		t.Fatalf("inserted %d, want JobsPlanned %d (>0)", inserted, plan.JobsPlanned)
	}
	if got := countJobs(t, ctx, db); got != inserted {
		t.Fatalf("crawl_jobs has %d rows, want %d", got, inserted)
	}

	// Re-plan + re-enqueue: all pairs now have an active job → zero inserts.
	plan2, err := BuildPlan(ctx, db, nowMs, 0)
	if err != nil {
		t.Fatal(err)
	}
	if plan2.JobsPlanned != 0 {
		t.Fatalf("second plan JobsPlanned = %d, want 0 (all active)", plan2.JobsPlanned)
	}
	inserted2, err := Enqueue(ctx, db, plan2)
	if err != nil {
		t.Fatal(err)
	}
	if inserted2 != 0 {
		t.Fatalf("second enqueue inserted %d, want 0", inserted2)
	}
	if got := countJobs(t, ctx, db); got != inserted {
		t.Fatalf("crawl_jobs grew to %d, want stable %d", got, inserted)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/planner/ -run TestEnqueue -v`
Expected: FAIL — `Enqueue` undefined.

- [ ] **Step 3: Implement `Enqueue`**

Append to `internal/planner/planner.go`:

```go
// Enqueue inserts a pending crawl_jobs row for every would_enqueue decision in
// the plan, inside one transaction. Returns the number of rows inserted.
func Enqueue(ctx context.Context, db *sql.DB, plan Plan) (int, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("planner: begin tx: %w", err)
	}
	defer tx.Rollback()

	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?, ?, ?, 'pending')
	`)
	if err != nil {
		return 0, fmt.Errorf("planner: prepare insert: %w", err)
	}
	defer stmt.Close()

	inserted := 0
	for _, j := range plan.Jobs {
		if j.Decision != DecisionWouldEnqueue {
			continue
		}
		if _, err := stmt.ExecContext(ctx, j.SourceID, j.CountryID, string(j.JobType)); err != nil {
			return 0, fmt.Errorf("planner: insert %s/%s: %w", j.ISO2, j.JobType, err)
		}
		inserted++
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("planner: commit: %w", err)
	}
	return inserted, nil
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/planner/ -run TestEnqueue -v`
Expected: PASS.

- [ ] **Step 5: Run the whole planner package**

Run: `cd control-plane && go test ./internal/planner/...`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add internal/planner/planner.go internal/planner/planner_enqueue_test.go
git commit -m "feat(control-plane): planner Enqueue persists pending crawl jobs"
```

---

## Task 12: Wire the `plan` subcommand

**Files:**
- Modify: `internal/app/app.go` (the `Run` switch ~line 46; the usage strings ~line 38/59; add a `runPlan` handler)
- Test: `internal/app/app_plan_test.go`

**Interfaces:**
- Consumes: `planner.BuildPlan`, `planner.Enqueue`, `database.Open`, `migrations.Apply`, `seed.Apply`.
- Produces: `aviation-coverage plan --db <path> [--enqueue] [--limit N] [--generated-at <RFC3339>]`. Dry-run prints the `Plan` as JSON to stdout. `--enqueue` writes rows and prints `enqueued N, skipped M` to stderr.

- [ ] **Step 1: Write the failing test**

Create `internal/app/app_plan_test.go`:

```go
package app

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
)

// prepDB builds a migrated+seeded DB and returns its path.
func prepDB(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	path := dir + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer
	if code := Run(ctx, []string{"migrate", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	if code := Run(ctx, []string{"seed", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("seed exit %d: %s", code, errb.String())
	}
	return path
}

func TestPlanDryRunPrintsJSON(t *testing.T) {
	path := prepDB(t)
	var out, errb bytes.Buffer
	code := Run(context.Background(),
		[]string{"plan", "--db", path, "--generated-at", "2026-06-23T12:00:00Z"},
		&out, &errb)
	if code != 0 {
		t.Fatalf("plan exit %d: %s", code, errb.String())
	}
	var plan struct {
		CandidateCountries int `json:"candidate_countries"`
		JobsPlanned        int `json:"jobs_planned"`
		Jobs               []struct {
			ISO2     string `json:"iso2"`
			JobType  string `json:"job_type"`
			Decision string `json:"decision"`
		} `json:"jobs"`
	}
	if err := json.Unmarshal(out.Bytes(), &plan); err != nil {
		t.Fatalf("output not JSON: %v\n%s", err, out.String())
	}
	if plan.CandidateCountries == 0 || len(plan.Jobs) == 0 {
		t.Fatalf("empty plan: %+v", plan)
	}
}

func TestPlanEnqueueWritesJobs(t *testing.T) {
	path := prepDB(t)
	var out, errb bytes.Buffer
	code := Run(context.Background(),
		[]string{"plan", "--db", path, "--enqueue"}, &out, &errb)
	if code != 0 {
		t.Fatalf("plan --enqueue exit %d: %s", code, errb.String())
	}
	if !strings.Contains(errb.String(), "enqueued") {
		t.Fatalf("stderr missing summary: %q", errb.String())
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd control-plane && go test ./internal/app/ -run TestPlan -v`
Expected: FAIL — unknown command `plan`.

- [ ] **Step 3: Add the command + handler**

In `internal/app/app.go`: add the `planner` import; add `plan` to both usage strings; add the switch case; add the handler.

```go
	// import block:
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/planner"
```

```go
	// in Run's switch:
	case "plan":
		return runPlan(ctx, rest, stdout, stderr)
```

Update both `commands: migrate, seed, import-aia, import-raio, validate, export` strings to append `, plan`.

```go
// ── plan ─────────────────────────────────────────────────────────────────────

func runPlan(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("plan", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	enqueue := fs.Bool("enqueue", false, "write pending crawl_jobs instead of dry-run")
	limit := fs.Int("limit", 0, "cap to the top-N ranked countries (0 = no cap)")
	generatedAt := fs.String("generated-at", "", "RFC3339 timestamp for generated_at (default: now)")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "plan: --db is required")
		fs.Usage()
		return exitUsage
	}

	var nowT time.Time
	if *generatedAt != "" {
		t, err := time.Parse(time.RFC3339, *generatedAt)
		if err != nil {
			fmt.Fprintf(stderr, "plan: --generated-at: invalid RFC3339 value %q: %v\n", *generatedAt, err)
			return exitUsage
		}
		nowT = t.UTC()
	} else {
		nowT = time.Now().UTC()
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "plan: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	p, err := planner.BuildPlan(ctx, db, nowT.UnixMilli(), *limit)
	if err != nil {
		fmt.Fprintf(stderr, "plan: %v\n", err)
		return exitFailure
	}

	if *enqueue {
		inserted, err := planner.Enqueue(ctx, db, p)
		if err != nil {
			fmt.Fprintf(stderr, "plan: enqueue: %v\n", err)
			return exitFailure
		}
		fmt.Fprintf(stderr, "enqueued %d, skipped %d\n", inserted, len(p.Jobs)-inserted)
		return exitOK
	}

	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(p); err != nil {
		fmt.Fprintf(stderr, "plan: encode: %v\n", err)
		return exitFailure
	}
	return exitOK
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd control-plane && go test ./internal/app/ -run TestPlan -v`
Expected: PASS.

- [ ] **Step 5: Run the whole module + vet**

Run: `cd control-plane && go vet ./... && go test ./...`
Expected: PASS across all packages.

- [ ] **Step 6: Commit**

```bash
git add internal/app/app.go internal/app/app_plan_test.go
git commit -m "feat(control-plane): wire plan subcommand (dry-run + --enqueue)"
```

---

## Task 13: Document the `plan` command

**Files:**
- Modify: `README.md` (add a `### plan` section after `### export`; add `plan` to the command list intro if one exists)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add the README section**

Insert after the `export` command section in `README.md`:

````markdown
### plan

Ranks coverage gaps by ROI (`priority_score = expected_records ×
expected_source_quality ÷ effort_score`) and produces a scheduling plan. For each
non-policy-excluded country, the applicable crawl-job types (derived from its
`coverage_status`, and for delegated countries its `delegate_iso2`) are emitted as
`crawl_jobs`.

**Dry-run (default)** prints a deterministic JSON plan to stdout; nothing is
written:

```bash
./aviation-coverage plan --db coverage.db
./aviation-coverage plan --db coverage.db --limit 50
```

**Enqueue** writes one `pending` `crawl_jobs` row per `would_enqueue` decision and
prints `enqueued N, skipped M` to stderr:

```bash
./aviation-coverage plan --db coverage.db --enqueue
```

The planner is idempotent: a (country, job_type) pair with a `pending`/`running`
job is `skipped_active`; a completed pair is re-emitted only after its
`refresh_cadence` window elapses (`skipped_cadence`). A pair whose source cannot be
resolved is `skipped_no_source` and listed under `warnings`.

Flags: `--enqueue`, `--limit N` (0 = no cap), `--generated-at <RFC3339>`.
````

- [ ] **Step 2: Verify the docs build/read correctly**

Run: `cd control-plane && grep -n "### plan" README.md`
Expected: the new section is present.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(control-plane): document the plan command"
```

---

## Final verification

- [ ] **Run the full module test suite + vet:**

Run: `cd control-plane && go vet ./... && go test ./...`
Expected: all packages PASS, no vet warnings.

- [ ] **Smoke-test the binary end-to-end:**

```bash
cd control-plane
go build -o /tmp/aviation-coverage ./cmd/aviation-coverage
D=/tmp/cov-smoke.db; rm -f "$D"
/tmp/aviation-coverage migrate --db "$D"
/tmp/aviation-coverage seed --db "$D"
/tmp/aviation-coverage plan --db "$D" --limit 5            # prints JSON plan
/tmp/aviation-coverage plan --db "$D" --enqueue            # enqueued N, skipped M
/tmp/aviation-coverage validate --db "$D"                  # still exits 0
```
Expected: the dry-run prints a JSON plan with `would_enqueue` decisions; `--enqueue` reports a non-zero `enqueued` count; `validate` stays green.

---

## Notes for the executor

- **Type-name verification:** before Task 6, confirm the exact `model` constant names by reading `internal/model/enums.go` — this plan uses `model.CoverageStatusDelegatedToForeign`, `model.CrawlJobTypeWaybackCDX`, `model.CrawlJobTypeNTSBForeignSearch`, `model.CrawlJobTypeBEAForeignSearch`, `model.CrawlJobTypeATSBSearch`, `model.CrawlJobTypeICAOELibrarySearch`, `model.CrawlJobTypePDFDiscovery`, `model.CrawlJobTypeDirectRequestNeeded`, `model.CrawlJobTypeArchiveCrawl`, `model.CrawlJobTypeAuthorityHealthCheck`, `model.SourceWayback`, `model.SourceTrustedIndex`. They exist as written in the current enums.go; if any differ, adjust uniformly.
- **Test helper duplication:** the `seededDB` helper is defined once in `planner_query_test.go` (Task 7) and reused by Tasks 8–11 (same package, so no re-declaration). The `prepDB` helper lives in `app_plan_test.go` (Task 12).
- Keep commits per-task; a fresh reviewer gates each task between commits.
