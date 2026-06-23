# Regional Worker — Discovery + Staging — Design Spec

**Date:** 2026-06-23
**Repo:** `aviation-safety-scrapers/control-plane`
**Sub-project:** 3 of the gap-driven coverage roadmap — the regional acquisition
worker, draining `archive_crawl` jobs for `regional_raio` countries by querying
the regional investigation body that covers them.

---

## 1. Context & Goal

The gap scheduler emits `archive_crawl` (+ `wayback_cdx`) for countries whose
`coverage_status = regional_raio` — states covered by a regional accident
investigation body rather than a national one. The seed defines three such bodies
with members:

- **ECCAA** (Eastern Caribbean, `eccaa.org`): DM, GD, KN, LC, VC
- **BAGAIA** (Banjul Accord Group, West Africa, `bagasoo.org`): CV, GM, GH, GN, LR, NG, SL
- **IAC** (Interstate Aviation Committee, CIS, `mak.aero`): RU, AM, AZ, BY, KZ, KG, TJ, TM

This worker resolves each `archive_crawl` job's country to its regional body,
queries that body's accident-report archive for the country, and stages the
discovered records. It is the direct sibling of the merged Wayback worker and the
foreign-search worker (sub-project 2): same control-plane Go pattern — a network/
file seam behind one interface, pure offline-tested per-body parsers, a staging
table, a queue-draining CLI.

**This spec's deliverable (stage 1):** given pending `archive_crawl` jobs **whose
country is `regional_raio`**, resolve the regional body, query it for the country's
accident records, parse, and stage them idempotently. Downloading the report PDFs,
OCR, and promotion into `events`/`reports` is a later stage.

### Non-goals (out of scope here)

- Report download / OCR / promotion into `events`/`reports` (later stage).
- `archive_crawl` jobs for **non-regional** countries (`direct_public_archive` /
  `source_exists_unstable`) — those crawl a country's *own* national archive, a
  different acquisition; this worker leaves them untouched (they stay `pending`
  for a future authority-archive worker). Worker 3 selects only
  `coverage_status='regional_raio'` jobs.
- The PDF-discovery, manufacturer, and MSN workers (later sub-projects).

---

## 2. Design Decisions (locked in brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Bodies | **All three** (ECCAA, BAGAIA, IAC) | User-chosen; routing + staging shared, only the per-body client/parser differs. |
| Depth | discovery + staging only | Mirrors the Wayback / foreign-search split. |
| Body resolution | from `regional_body_members → regional_bodies` via `country_id` | Single body per regional_raio country in the seed; no new `crawl_jobs` column. |
| Job scope | only `archive_crawl` jobs where `coverage_status='regional_raio'` | `archive_crawl` is shared; this worker handles only the regional subset. |
| Access | live HTML per body; **out-of-band `--source-file`** fallback for a body behind CF/Akamai (likely `mak.aero`) | Mirror the BEA (live) / ATSB (out-of-band) split from sub-project 2. |
| Isolation | dedicated git worktree | Concurrent-work mitigation (the user works the same checkout). |

---

## 3. Architecture

New package `internal/worker/regional/`. The network/file seam is a per-body client
behind one interface:

```go
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

- **`eccaaClient` / `bagaiaClient` / `iacClient`** — each a thin fetch (live
  `net/http` to the body's report listing, or `--source-file` read for an
  out-of-band body) + a **pure parser** (`parseECCAA` / `parseBAGAIA` / `parseIAC`)
  unit-tested against a captured real fixture. Tests inject a `fixtureClient`.
- **Body resolution:** `ResolveBody(ctx, db, countryID) (bodyCode string, ok bool,
  err error)` joins `regional_body_members → regional_bodies` for the country and
  returns the body's `code` (`ECCAA`/`BAGAIA`/`IAC`). A country with no regional
  body → `("", false, nil)` → job `failed` with a clear message.

### Realistic-fixture requirement

Each body's report-listing format is captured from the live site before its parser
is written (the implementation plan includes a probe step per body: fetch the
listing, save a real sample under `testdata/`, write the parser against it). A body
that is CF/Akamai-blocked from a data-centre fetch (probe will reveal) becomes an
out-of-band `--source-file` client; its parser is still offline-tested against a
captured/saved sample. Parsers are never written against a guessed format.

### Data flow (per job)

```
pending archive_crawl job (country is regional_raio)
  → resolve country ISO2 + body_code (members→bodies)
  → select client by body_code
  → client.Search(countryISO2)        [live net/http, or --source-file]
  → parse → []RegionalRecord
  → stage each into staged_regional_documents (dedup on (body_code, ref))
  → finalize crawl_job: success|partial|failed + stats_json{found,staged,errors}
  → crawl_errors per failure
```

---

## 4. Schema — migration `007_regional.sql`

(New file; predecessors immutable under the checksum guard. **Merge-coordination
note:** the foreign-search PR (#9) also introduces a `00N` migration; whichever of
{#9, this} merges second renumbers — standard stacked-PR migration-dup handling.)

```sql
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

`UNIQUE(body_code, ref)` is the idempotency key — a body's report id is unique
within that body, so re-running any member country's job never double-stages it.

---

## 5. Worker behavior — `process-regional`

```
aviation-coverage process-regional --db coverage.db [--limit N] [--source-file FILE] [--country ISO2]
```

- **Job selection:** pending (or stale-running >1h) jobs with
  `job_type='archive_crawl'`, joined to countries **WHERE
  `coverage_status='regional_raio'`**, ordered `priority_score DESC, iso2 ASC`,
  capped by `--limit`. `--country` narrows to one country (operator-targeted runs,
  e.g. feeding an out-of-band body's `--source-file`).
- **Per job:** mark `running`; resolve ISO2 + body_code; pick the client; if the
  body has no live access (out-of-band) and no `--source-file` is supplied → job
  `failed` + a clear `crawl_errors` message; else `client.Search(iso2)` → parse →
  stage.
- **Finalize:** `success` / `partial` / `failed` + `stats_json={found,staged,
  errors}` + `finished_at`; `crawl_errors` per failure. Stale-`running` jobs (>1h)
  are re-selected (resume-safe) and unexpected-DB-error returns finalize `failed`
  — identical to the Wayback / foreign-search workers.

---

## 6. Error handling

Same contract as sub-project 2: transport/file/parse failures → `crawl_errors`
rows with the right `error_type`; unresolved body or missing out-of-band source →
`failed` with a clear message; one bad record degrades the job to `partial`
(counted), never aborts the batch.

---

## 7. Testing (TDD)

1. **Per-body parse:** a captured real fixture per body → expected
   `[]RegionalRecord` (fields populated; malformed rows counted as warnings;
   unparseable body → error for JSON, zero-records for HTML).
2. **Body resolution:** a country in ECCAA/BAGAIA/IAC resolves to the right code; a
   country in no body → `("", false, nil)`.
3. **Routing:** `body_code → client`; an unknown/unmapped body is rejected.
4. **Staging + dedup:** same `(body_code, ref)` twice → one row; different refs →
   distinct rows.
5. **Job finalize:** all-ok → `success` + stats; parse warning → `partial`;
   transport error / unresolved body / missing source → `failed` + a `crawl_errors`
   row.
6. **Stale-running recovery:** a `running` job older than 1h is re-selected; a fresh
   one (recent non-NULL `started_at`) is not.
7. **Job scope:** an `archive_crawl` job for a `direct_public_archive` country is
   NOT selected by `ProcessPending` (only `regional_raio`).
8. **Migration `007_regional.sql`:** migrate→insert→`UNIQUE(body_code, ref)`
   enforced; existing checksum/name guards stay green.
9. **CLI `process-regional`:** against a migrated+seeded DB with a fixtureClient,
   a pending regional `archive_crawl` job ends `success`/`partial` with staged rows.

---

## 8. Files touched

- `internal/migrations/sql/007_regional.sql` — new.
- `internal/worker/regional/record.go` — `RegionalRecord`, `RegionalClient`.
- `internal/worker/regional/resolve.go` — `ResolveBody`.
- `internal/worker/regional/eccaa.go` / `bagaia.go` / `iac.go` — client + parser.
- `internal/worker/regional/stage.go` — `StageRecords`.
- `internal/worker/regional/runner.go` — routing, `RunJob`, `ProcessPending`.
- `internal/worker/regional/*_test.go` + `testdata/` — tests, fixtureClient, fixtures.
- `internal/app/app.go` — wire `process-regional`.
- `README.md` — document the command + out-of-band `--source-file`.

---

## 9. Roadmap position

Sub-project 3, stage 1 (discovery + staging). A later stage downloads the
`report_url` PDFs, OCRs, and promotes the staged regional records into
`events`/`reports` (shared with the Wayback/foreign-search extraction path).
Workers 4 (PDF-discovery), 5 (manufacturer), 6 (MSN) follow. A future
authority-archive worker handles the `archive_crawl` jobs for
`direct_public_archive` countries that this worker intentionally skips.
