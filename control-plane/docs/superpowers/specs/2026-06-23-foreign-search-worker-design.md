# Foreign-Search Worker — Discovery + Staging — Design Spec

**Date:** 2026-06-23
**Repo:** `aviation-safety-scrapers/control-plane`
**Sub-project:** 2 of the gap-driven coverage roadmap — the second acquisition
worker, draining `ntsb_foreign_search` / `bea_foreign_search` / `atsb_search`
crawl jobs for countries whose accident investigations are delegated to a foreign
authority.

---

## 1. Context & Goal

The gap scheduler emits foreign-search jobs for countries with
`coverage_status = delegated_to_foreign_authority` (and the scheduler maps the
country's `delegate_iso2` → the job type: `US → ntsb_foreign_search`,
`FR → bea_foreign_search`, `AU → atsb_search`). The country-expansion overlays
now make this real: 14 delegated states — Pacific micro-states → AU (ATSB),
Micronesia/Caribbean → US (NTSB), Andorra → FR (BEA).

Nothing yet executes these jobs. This worker resolves each job's delegate
authority, queries that authority's accident record set **for the occurrence
country**, and stages the discovered records. It is the foreign-investigation
analogue of the merged Wayback worker (sub-project 1): same control-plane Go
pattern — a network seam behind an interface, pure offline-tested parsers, a
staging table, a queue-draining CLI command.

**This spec's deliverable (stage 1):** given pending foreign-search jobs, route
each to the right foreign authority's client, query by occurrence country, parse
the response, and stage the discovered accident records (with the report URL)
idempotently. Downloading the report PDFs, OCR, and promotion into
`events`/`reports` is a **later stage** (mirrors the Wayback worker's split).

### Non-goals (out of scope here)

- Downloading report PDFs / OCR / promotion into `events`/`reports` (later stage).
- The regional, PDF-discovery, manufacturer, and MSN workers (later sub-projects).
- A live, in-process ATSB fetch (ATSB sits behind Akamai bot-protection — see §3).

---

## 2. Design Decisions (locked in brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Scope of authorities | **All three** (NTSB, BEA, ATSB) in one worker | User-chosen; the routing + staging is shared, only the per-authority client/parser differs. |
| Depth | discovery + staging only (report download/promotion later) | Keeps "all three" bounded to one spec; mirrors the Wayback stage split. |
| Delegate resolution | from `countries.delegate_iso2` via `country_id` | Single-valued per country; no new `crawl_jobs` column needed — the roadmap's "per-authority target ref" follow-up is **moot for foreign-search**. |
| ATSB access | **out-of-band `--source-file`** (operator fetches via the mini-PC browser; the worker parses the saved file) | ATSB is Akamai-headed; a clean in-process `net/http` fetch fails. NTSB/BEA fetch live. |
| Isolation | built in a dedicated git **worktree** | The user works the same checkout concurrently; a worktree is immune to their branch switching. |

---

## 3. Architecture

New package `internal/worker/foreignsearch/`. The control-plane Go binary stays the
coordinator. The network seam is a per-authority client behind one interface:

```go
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
    // Search returns the authority's accident records for the given occurrence
    // country (ISO-3166 alpha-2). raw is the unparsed response body (for live
    // clients) or the --source-file bytes (for out-of-band clients).
    Search(ctx context.Context, countryISO2 string) ([]ForeignRecord, error)
}
```

- **`ntsbClient`** — live `net/http` to the NTSB CAROL public query API
  (`POST .../api/Query/Main`, JSON, filtered by occurrence country). Parses the
  JSON result rows into `ForeignRecord`s.
- **`beaClient`** — live `net/http` to the BEA investigation-reports listing
  (`bea.aero`), filtered/searched by country. Parses the HTML listing.
- **`atsbClient`** — **out-of-band**: constructed from a `--source-file` (a JSON
  or HTML export an operator saved via the mini-PC browser, since Akamai blocks a
  data-centre fetch). Parses the saved body. If no source file is supplied for an
  `atsb_search` job, the job fails with a clear "atsb requires --source-file"
  error.

Each client splits into a thin fetch part (network / file read) and a **pure
parser** (`parseNTSB(raw []byte)`, `parseBEA(raw []byte)`, `parseATSB(raw []byte)`)
that is unit-tested offline against a captured fixture. Tests inject a
`fixtureClient`.

### Realistic-fixture requirement

The exact CAROL accident-query payload and the BEA/ATSB response shapes must be
**captured from the real services** before the parsers are written (the in-memory
CAROL recipe is for *recommendations*, not accidents). The implementation plan
includes a per-authority probe step: hit the live endpoint (or, for ATSB, have the
operator export one country's page from the mini-PC), save a real sample under
`testdata/`, and write the parser against that captured fixture. Parsers are never
written against a guessed format.

### Data flow (per job)

```
pending foreign-search job (job_type, country_id)
  → resolve country ISO2 + delegate_iso2
  → select client by job_type (ntsb/bea/atsb)
  → client.Search(countryISO2)              [live net/http, or --source-file for atsb]
  → parse → []ForeignRecord
  → stage each into staged_foreign_documents (dedup on (authority, foreign_ref))
  → finalize crawl_job: success|partial|failed + stats_json{found,staged,errors}
  → crawl_errors per failure
```

---

## 4. Schema — migration `006_foreign.sql`

(New file; 001–005 immutable under the checksum guard.)

```sql
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

`UNIQUE(authority, foreign_ref)` is the idempotency key — re-running a job never
double-stages the same foreign record (a foreign authority's case id is globally
unique within that authority, independent of which country job discovered it).

---

## 5. Worker behavior — `process-foreign-search`

```
aviation-coverage process-foreign-search --db coverage.db \
  [--authority ntsb|bea|atsb] [--limit N] [--source-file FILE] [--country ISO2]
```

- **Job selection:** pending jobs whose `job_type IN
  ('ntsb_foreign_search','bea_foreign_search','atsb_search')`, joined to
  countries, ordered `priority_score DESC, iso2 ASC`, capped by `--limit`.
  `--authority` narrows to one authority's job type. `--country` narrows to one
  country (operator-targeted runs, e.g. feeding an ATSB `--source-file`).
- **Per job:** mark `running`; resolve the country ISO2; pick the client for the
  job type; `client.Search(iso2)`:
  - NTSB/BEA: live fetch + parse.
  - ATSB: requires `--source-file`; absent → job `failed` + a `crawl_errors` row
    (`error_type='unknown'`, message "atsb_search requires --source-file").
- **Stage** each parsed record (`INSERT … ON CONFLICT(authority, foreign_ref) DO
  NOTHING`); count newly inserted.
- **Finalize:** `success` (records found and staged, no errors), `partial`
  (some parse warnings / a record failed), `failed` (transport error, unresolved
  delegate, or missing ATSB source file). Always write
  `stats_json={"found":F,"staged":S,"errors":E}` and `finished_at`.
- Like the Wayback worker, `ProcessPending` also re-selects **stale `running`**
  foreign-search jobs (`started_at` older than 1h) so a killed run is recoverable,
  and unexpected-DB-error early returns finalize the job `failed` (never orphan).

---

## 6. Error handling

- Transport / file-read / parse failures map to `crawl_errors` rows with the
  appropriate `error_type` (`timeout`/`dns_error`/`tls_error`/`http_403`/
  `http_404`/`http_500`/`parse_error`/`unknown`).
- A job whose delegate cannot be resolved, or an `atsb_search` job with no
  `--source-file`, is `failed` with a clear message — never a silent success.
- One bad record degrades the job to `partial` (counted), never aborts the batch.

---

## 7. Testing (TDD)

1. **Per-authority parse:** a captured real fixture per authority
   (`testdata/ntsb_carol.json`, `testdata/bea_listing.html`,
   `testdata/atsb_export.json`) → expected `[]ForeignRecord` (fields populated,
   malformed rows counted as warnings not errors, unparseable body → error).
2. **Routing:** `ntsb_foreign_search → ntsbClient`, `bea_foreign_search →
   beaClient`, `atsb_search → atsbClient`; an unknown job type is rejected.
3. **Staging + dedup:** staging the same `(authority, foreign_ref)` twice inserts
   one row; different refs insert distinct rows.
4. **Job finalize:** all-ok → `success` + correct `stats_json`; a parse warning →
   `partial`; a transport error / unresolved delegate / missing ATSB source →
   `failed` + a `crawl_errors` row.
5. **Stale-running recovery:** a `running` job older than 1h is re-selected and
   finalized; a fresh `running` job is not.
6. **ATSB guard:** an `atsb_search` job with no `--source-file` ends `failed` with
   the documented message.
7. **Migration `006_foreign.sql`:** migrate→insert→`UNIQUE(authority,foreign_ref)`
   enforced; existing migration checksum/name guards stay green.
8. **CLI `process-foreign-search`:** against a migrated+seeded DB with a
   fixtureClient wired in, a pending NTSB job for a delegated country ends
   `success`/`partial` with staged rows and a populated `stats_json`.

---

## 8. Files touched

- `internal/migrations/sql/006_foreign.sql` — new (`staged_foreign_documents`).
- `internal/worker/foreignsearch/record.go` — `ForeignRecord`, `AuthorityClient`.
- `internal/worker/foreignsearch/ntsb.go` / `bea.go` / `atsb.go` — clients + pure
  parsers.
- `internal/worker/foreignsearch/stage.go` — `StageRecords`.
- `internal/worker/foreignsearch/runner.go` — `RunJob`, `ProcessPending`, routing,
  stale-running recovery.
- `internal/worker/foreignsearch/*_test.go` + `testdata/` — tests + `fixtureClient`
  + captured fixtures.
- `internal/app/app.go` — wire `process-foreign-search`.
- `README.md` — document the command + the ATSB out-of-band `--source-file` step.

---

## 9. Roadmap position

Sub-project 2, stage 1 (discovery + staging). A later stage downloads the
`report_url` PDFs, OCRs them, and promotes the staged foreign records into
`events`/`reports` with provenance + dedup — shared with the Wayback worker's
stage-2 extraction path. Worker 3 (regional / ECCAA) follows.
