# Wayback Worker — Extraction (OCR + LLM → events/reports) — Design Spec

**Date:** 2026-06-23
**Repo:** `aviation-safety-scrapers/control-plane`
**Sub-project:** 1 of the gap-driven coverage roadmap, **stage 2 of 2**. Stage 1
(discovery + acquisition, PR #5, merged `origin/main@183d5d3`) drains the
`wayback_cdx` queue into `staged_wayback_documents` with downloaded PDFs. This spec
is **Spec 2**, which the stage-1 design named as the consumer of those rows.

---

## 1. Context & Goal

Stage 1 produced, per country, a set of `staged_wayback_documents` rows with
`download_status='downloaded'`, each pointing at a local PDF
(`local_file_path = <store>/<iso2>/<digest>.pdf`) recovered from the Internet
Archive. Nothing yet reads those PDFs.

**Goal (this spec):** turn each downloaded PDF into structured safety data — OCR the
PDF to text, extract event fields with an LLM, and promote the result into the
existing `events` and `reports` tables with provenance, a deterministic confidence
score, and deterministic dedup against events already in the coverage DB.

This completes the end-to-end extraction pipeline the stage-1 spec described in its
§10 ("Spec 2 consumes `staged_wayback_documents` rows: OCR → LLM field extraction →
`events`/`reports` with provenance + dedup").

### Non-goals (out of scope here)

- Re-processing or back-filling events from sources other than Wayback.
- Fuzzy / semantic dedup, cross-source entity resolution, or an LLM-based merge
  judge. v1 dedup is deterministic key-matching only (§7).
- Broad authoring of `wayback_target`/authority data for all ~150 countries — that
  is the parallel country-expansion data effort (stage-1 spec §7).
- A standalone dedup worker, a review UI, or promotion of `dedup_status` beyond the
  two states this worker sets (`unreviewed`, `soft_linked`).
- The other acquisition workers (foreign-search, regional, manufacturer, MSN).

---

## 2. Design Decisions (locked in brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| OCR / LLM seam | **Two injectable HTTP interfaces** (`OCRClient`, `LLMClient`), parallel to stage-1's `Fetcher` | Both stages are fragile network calls; isolating them behind interfaces keeps the package offline-testable with fixtures. |
| Intermediate OCR text | **Persist as an artifact** (`<store>/<iso2>/<digest>.txt` + status) | OCR is minutes of CPU and one-time-heavy; persisting lets LLM re-run without re-OCR, is debuggable, and makes restart idempotent — same discipline as stage-1's staged downloads. |
| Driver model | **`extraction_status` column on `staged_wayback_documents`** (not a new `crawl_jobs` type) | The unit of work is the document, which is already a persisted row with a status. `crawl_jobs` stays a clean country-level discovery queue (it has no document FK). |
| Spec scope | **One spec for the full PDF→events pipeline** | Linear pipeline (PDF → text → fields → rows); one cohesive deliverable. No orphan intermediate (OCR'd text with nothing consuming it). |
| Dedup depth (v1) | **Deterministic key-match** (date+registration, fallback date+operator+fatalities) | Cheaply kills obvious duplicates; leaves ambiguous cases as `unreviewed` for a later pass. The `events.dedup_status` enum already anticipates this. |
| Confidence | **Deterministic from field completeness + official bonus** | LLM self-reported confidence is poorly calibrated; a completeness-driven score is reproducible and explainable. |
| Source granularity | **Per-regulator, lookup-or-create** (fallback to a per-country `wayback` source) | Correct provenance — the source is the regulator that authored the report; the Wayback access path is captured by `reports.archived_url`. |
| OCR transport (real impl) | **Thin HTTP OCR service on hetzner** wrapping the existing `ocrmypdf` | No OCR HTTP endpoint exists today; standing up a small FastAPI route preserves the clean HTTP seam the Go code wants. It is an out-of-band deployment dependency (§9), not part of the Go TDD. |

---

## 3. Architecture

The control-plane Go binary stays the coordinator. New code extends the existing
`internal/worker/wayback/` package (which already holds `cdx.go`, `download.go`,
`runner.go`, etc.). Two new network seams, mirroring stage-1's `Fetcher`:

```go
// OCRClient turns a PDF's bytes into plain text.
type OCRClient interface {
    OCR(ctx context.Context, pdf []byte) (text string, err error)
}

// LLMClient extracts structured event fields from report text.
type LLMClient interface {
    Extract(ctx context.Context, text string) (ExtractedEvent, error)
}
```

- **Real impls:**
  - `httpOCRClient` — POSTs the PDF bytes to a configurable `--ocr-endpoint`
    (the hetzner service of §9) and reads back the extracted text.
  - `httpLLMClient` — POSTs to an Ollama-compatible `/api/generate` at
    `--llm-endpoint` with a `format` JSON schema (grammar-constrained → valid JSON
    guaranteed, no ```` ``` ```` fences) and `think:false`, model `--llm-model`
    (default `qwen3.6-rw`). Input is truncated to `--max-input-chars` (default
    24000; head-truncation) so large boilerplate PDFs do not overflow `num_ctx`.
- **Test impls:** `fixtureOCRClient` and `fixtureLLMClient`, backed by files under
  `internal/worker/wayback/testdata/`. The whole pipeline is offline-testable.
- **Public-repo hygiene:** the repo is public. No private endpoint or token is
  hardcoded; all of `--ocr-endpoint`, `--llm-endpoint`, `--llm-model` come from
  flags/env and default to innocuous local values.

### Code units (one clear purpose each)

| File | Responsibility |
|---|---|
| `ocr.go` | `OCRClient` interface + `httpOCRClient`; persist text to `<store>/<iso2>/<digest>.txt`. |
| `llm.go` | `LLMClient` interface + `httpLLMClient`; `ExtractedEvent` struct + the JSON `format` schema + embedded prompt. |
| `extract.go` | Pure field mapping (`ExtractedEvent` → an `events` row) + the deterministic confidence formula. No I/O. |
| `promote.go` | Source lookup-or-create, deterministic dedup, and the `events`/`reports` inserts, in one transaction per document. |
| `extractrunner.go` | The per-document state machine + job selection + retry cap + aggregate stats. |

### Data flow (per document)

```
staged_wayback_documents row (download_status='downloaded')
  ┌─ extraction_status = 'pending'
  │    read local_file_path → OCRClient.OCR(pdf)
  │    write <store>/<iso2>/<digest>.txt ; set ocr_text_path ; status → 'ocr_done'
  └─ extraction_status = 'ocr_done'
       read ocr_text_path → LLMClient.Extract(text) → ExtractedEvent
       if !is_aviation_accident OR missing critical fields → status → 'skipped'
       else (one tx):
         confidence = deterministic(completeness, official)
         source     = lookup-or-create (per-regulator ▸ wayback fallback)
         match      = dedup(date+registration ▸ date+operator+fatalities)
         match ? link report to existing event (event.dedup_status='soft_linked')
               : INSERT events (dedup_status='unreviewed')
         INSERT reports (event_id, source_id, report_type, archived_url, …)
         set staged doc event_id ; status → 'extracted'
```

---

## 4. Schema — migration `006_wayback_extract.sql`

New file; 001–005 are immutable under the checksum guard. All columns are added to
the existing `staged_wayback_documents` table (no new tables):

```sql
ALTER TABLE staged_wayback_documents ADD COLUMN extraction_status TEXT NOT NULL
  DEFAULT 'pending' CHECK(extraction_status IN (
    'pending', 'ocr_done', 'extracted', 'failed', 'skipped'
  ));
ALTER TABLE staged_wayback_documents ADD COLUMN ocr_text_path TEXT;
ALTER TABLE staged_wayback_documents ADD COLUMN extraction_error TEXT;
ALTER TABLE staged_wayback_documents ADD COLUMN extraction_attempts INTEGER NOT NULL
  DEFAULT 0;
ALTER TABLE staged_wayback_documents ADD COLUMN event_id INTEGER
  REFERENCES events(id);
```

- `extraction_status` drives selection and the state machine.
- `ocr_text_path` is the persisted-text artifact pointer (Q2 decision) **and** the
  resume discriminator: a re-selected document with `ocr_text_path IS NULL` restarts
  at OCR, otherwise it restarts at the LLM step.
- `extraction_error` stores the last failure message; `extraction_attempts` caps
  retries (poison-document guard, §6).
- `event_id` records which event a document was promoted into (nullable until
  `extracted`; remains NULL for `skipped`/`failed`).

> **Crash idempotency without a `running` status:** promotion (insert
> `events`+`reports` *and* set `extraction_status='extracted'`) is one transaction,
> so a crash either commits everything (the row is `extracted`, never re-selected) or
> nothing (the row stays selectable and re-runs only uncommitted work). No timed
> stale-reclaim or `running` state is needed — the status set plus the transaction
> are sufficient.

> **SQLite note:** `ALTER TABLE … ADD COLUMN` with a non-constant default is illegal,
> so `extraction_status`/`extraction_attempts` use constant defaults (`'pending'`,
> `0`), which is what we want. The migration is column-additive only — no data
> backfill needed (existing rows default to `pending`).

---

## 5. Worker behavior — `process-wayback-extract`

```
aviation-coverage process-wayback-extract --db coverage.db [--limit N] \
  [--store-dir DIR] --ocr-endpoint URL --llm-endpoint URL \
  [--llm-model qwen3.6-rw] [--max-input-chars 24000]
```

- **Job selection:** `SELECT d.* FROM staged_wayback_documents d
  JOIN countries c ON c.id = d.country_id
  WHERE d.download_status='downloaded'
    AND d.extraction_status IN ('pending','ocr_done','failed')
    AND d.extraction_attempts < 3
  ORDER BY c.priority_score DESC, d.id ASC` capped by `--limit` (0 = no cap).
  Including `failed` (with `attempts < 3`) is what makes the retry cap meaningful;
  the resume point is decided by `ocr_text_path`, not by the status label.
- **Per document**, advance the state machine, choosing the entry step by
  `ocr_text_path`:
  1. **OCR step** (`ocr_text_path IS NULL`): read `local_file_path`;
     `OCRClient.OCR(pdf)`; write the text to `<store-dir>/<iso2>/<digest>.txt`; set
     `ocr_text_path`; `extraction_status='ocr_done'`. An OCR error sets
     `extraction_status='failed'`, `extraction_error`, `extraction_attempts++`, and a
     `crawl_errors` row (`error_type='unknown'`, message+url), then continues to the
     next document. (After OCR succeeds, the same run proceeds to the extract step.)
  2. **Extract step** (`ocr_text_path` set): read `ocr_text_path`;
     `LLMClient.Extract(text)`.
     - If `is_aviation_accident` is false, or no critical field
       (§5.1) is present, set `extraction_status='skipped'` (a clean negative, not a
       failure) and continue.
     - Otherwise, in **one transaction**: compute confidence (§5.2), resolve the
       source (§7), run dedup (§7), insert/link `events` + insert `reports` (§5.3),
       set the document's `event_id` and `extraction_status='extracted'`.
     - An LLM transport/parse error sets `failed` + `extraction_error` +
       `attempts++` + a `crawl_errors` row, then continues.
- A document at `extraction_attempts >= 3` is left `failed` and never re-picked.
- The batch never panics on a single document — every failure degrades that one
  document and is counted.
- **Aggregate output:** print `{"ocr_done":O,"extracted":X,"skipped":K,"failed":F}`
  to stdout (the durable per-document record is the status columns themselves; no
  separate job ledger).

### 5.1 Critical fields (the accident gate)

A document promotes only if the LLM marks it `is_aviation_accident=true` **and** at
least these are present: a usable `date` (precision `exact` or `month`) **and** one
of {`aircraft_registration`, `aircraft_type`}. Otherwise it is `skipped`. This keeps
index pages, cover sheets, and unrelated archived PDFs out of `events`.

### 5.2 Confidence formula (deterministic)

```
critical = [ date∈{exact,month}, location present,
             (aircraft_type or aircraft_registration) present,
             fatalities known ]
base  = round( (count(true in critical) / 4) * 80 )
score = min(100, base + (20 if source is official_aai else 0))
```

`events.confidence_score` is `CHECK BETWEEN 0 AND 100`; the formula stays in range.

### 5.3 Report row

One `reports` row per promoted document:

| Column | Value |
|---|---|
| `event_id` | matched or newly-inserted event |
| `source_id` | resolved source (§7) |
| `report_type` | from the LLM (`final`/`preliminary`/`interim`/`factual`), default `final` |
| `title` | LLM `title` (fallback: `"<country> accident report"`) |
| `language` | LLM-detected language code (default `en`) |
| `original_url` | the captured live URL (`staged.original_url`) |
| `archived_url` | the Wayback URL (`staged.archived_url`) — provenance of recovery |
| `pdf_url` | `staged.original_url` (the archived asset is a PDF) |
| `published_date` | LLM `published_date` (nullable) |
| `accessed_at` | now (ms) |
| `checksum` | `staged.checksum` (SHA-256 of the PDF) |
| `local_file_path` | `staged.local_file_path` |
| `source_tier` | 1 for `official_aai`, 2 for the `wayback` fallback |
| `extraction_status` | `'extracted'` |
| `copyright_status` | `'official_public'` for `official_aai`; `'unknown'` for fallback |
| `notes` | nullable |

---

## 6. Error handling

- OCR and LLM transport/parse failures map to `extraction_status='failed'` with the
  message in `extraction_error`, `extraction_attempts++`, and a `crawl_errors` row
  (using the document's `crawl_job_id`) for stage-1-consistent observability.
- Non-accident / unparseable documents are `skipped`, never `failed` — a clean
  negative is distinct from an error.
- `extraction_attempts >= 3` is the poison-document guard: such a row is excluded
  from selection and stays `failed`.
- A single bad document never aborts the batch.

---

## 7. Source resolution + dedup

### Source (per-regulator, lookup-or-create)

For a document's country, look up the authority to credit, preferring
`authorities.type='national_aai'`, else `'caa'`:

- **Found:** `INSERT … ON CONFLICT(canonical_url, source_type) DO NOTHING` then
  select, with `name=authority.name`, `url=authority.website_url`,
  `canonical_url=authority.archive_url ?? authority.website_url`,
  `source_type='official_aai'`, `source_tier=1`. Report `copyright_status` is
  `'official_public'`.
- **Fallback (no authority or no URL):** a per-country source built from
  `countries.wayback_target` with `source_type='wayback'`, `source_tier=2`. Report
  `copyright_status` is `'unknown'`. A schedulable country always has a
  `wayback_target` (it is how stage-1 found the document), so a source always
  resolves.

### Dedup (deterministic key-match, against existing `events`)

Stage-1 already collapses identical Wayback captures by `digest`, so this guards
against the *same accident* appearing across captures or from another worker:

1. **Key 1:** an existing event with the same exact `date` **and** the same
   normalized (upper, trimmed) `aircraft_registration`.
2. **Key 2 (fallback, only if registration is absent):** same exact `date`
   **and** same `operator_name` **and** same `fatalities`.

- **Match:** do not insert a new event; set the new `reports.event_id` to the
  matched event; if the matched event's `dedup_status` is `'unreviewed'`, set it to
  `'soft_linked'`.
- **No match:** insert a new `events` row with `dedup_status='unreviewed'`.
- Because each document re-queries before inserting, two captures of one accident
  processed in the same run still produce a single event (the second matches the
  first's just-inserted row).

---

## 8. LLM extraction contract

`ExtractedEvent` mirrors the `events`/`reports` columns the worker writes, plus the
accident gate:

```go
type ExtractedEvent struct {
    IsAviationAccident bool    `json:"is_aviation_accident"`
    Date               string  `json:"date"`            // ISO-8601 or partial
    DatePrecision      string  `json:"date_precision"`  // exact|month|year|unknown
    Location           string  `json:"location"`
    Latitude           *float64`json:"latitude"`
    Longitude          *float64`json:"longitude"`
    AircraftRegistration string`json:"aircraft_registration"`
    AircraftType       string  `json:"aircraft_type"`
    Manufacturer       string  `json:"manufacturer"`
    OperatorName       string  `json:"operator_name"`
    FlightNumber       string  `json:"flight_number"`
    Fatalities         *int    `json:"fatalities"`
    Injuries           *int    `json:"injuries"`
    EventType          string  `json:"event_type"`           // accident|serious_incident|incident|hijacking|unknown
    InvestigationStatus string `json:"investigation_status"` // events enum
    ReportType         string  `json:"report_type"`          // final|preliminary|interim|factual
    Title              string  `json:"title"`
    Language           string  `json:"language"`
    PublishedDate      string  `json:"published_date"`
}
```

- The Ollama `format` field carries the JSON-Schema for this struct, so the model is
  grammar-constrained to emit exactly these keys with valid types.
- Enum-valued fields are clamped to the schema's allowed values on the Go side
  before any DB write; an out-of-enum value falls back to the column default
  (`'unknown'`) rather than violating a `CHECK`.
- The prompt is authored in-repo and embedded via `go:embed`.

---

## 9. OCR HTTP service (prerequisite, out-of-band)

A thin FastAPI route on hetzner wrapping the existing `ocrmypdf` invocation
(`--force-ocr --sidecar … --output-type none`, run under `nice -n 19 ionice -c3`,
languages `eng+spa+fra+rus`, 600 s timeout, graceful empty-text on failure). It
accepts a PDF (multipart or raw body) and returns the extracted text.

This is **not** part of the Go TDD (which uses `fixtureOCRClient`) and is **not** in
the public repo. It is a small deployment dependency required only for the live
smoke run, tracked separately. Its URL reaches the Go worker via `--ocr-endpoint`.

---

## 10. Testing (TDD — all offline on fixtures)

1. **OCR persist:** `fixtureOCRClient` returns known text → file written to
   `<store>/<iso2>/<digest>.txt`, `ocr_text_path` set, status `ocr_done`.
2. **LLM parse + gate:** a fixture accident JSON parses into `ExtractedEvent`; a
   non-accident fixture (`is_aviation_accident=false`) → status `skipped`, no event.
3. **Confidence formula:** unit tests over the four completeness combinations ±
   official bonus, asserting exact scores and the 100 cap.
4. **Dedup:** key-1 match → `soft_linked` + no new event; key-2 fallback match;
   no match → new `unreviewed`; two fixture docs of one accident in one run → one
   event.
5. **Source resolution:** authority present → `official_aai` tier-1 + `official_public`;
   authority absent → `wayback` tier-2 fallback + `unknown`; `ON CONFLICT` reuse
   does not duplicate the source.
6. **Promote:** `events` and `reports` rows have the expected columns, including
   `archived_url`, `checksum`, `copyright_status`, and the document's `event_id`.
7. **State machine:** `pending`→`ocr_done`→`extracted`; OCR failure path; LLM
   failure path; a `failed` row with `attempts<3` is re-selected and resumes from
   `ocr_text_path` (OCR not repeated once text exists); `attempts>=3` excluded; a
   document already `extracted` is never re-selected (no double event/report).
8. **Migration `006`:** migrate→seed round-trips; the new columns exist with correct
   defaults/checks; existing migration checksum/name guards stay green.
9. **CLI `process-wayback-extract`:** against a migrated+seeded DB with fixture OCR
   and LLM clients and a `download_status='downloaded'` document, the run ends with
   that document `extracted`, one `events` row, one `reports` row, and a correct
   aggregate stats line.

---

## 11. Files touched

- `internal/migrations/sql/006_wayback_extract.sql` — new (six additive columns on
  `staged_wayback_documents`).
- `internal/worker/wayback/ocr.go`, `llm.go`, `extract.go`, `promote.go`,
  `extractrunner.go` — new; plus `httpOCRClient`/`httpLLMClient`,
  `fixtureOCRClient`/`fixtureLLMClient`, an embedded extract prompt, and
  `testdata/` fixtures.
- `internal/app/app.go` — wire the `process-wayback-extract` subcommand
  (`--limit`, `--store-dir`, `--ocr-endpoint`, `--llm-endpoint`, `--llm-model`,
  `--max-input-chars`).
- `README.md` — document `process-wayback-extract`.
- (Out-of-repo) the hetzner OCR HTTP service of §9 — separate deployment task.

---

## 12. Roadmap position

Sub-project 1, **stage 2 of 2 — completes the Wayback extraction pipeline.** After
this, `events`/`reports` are populated from recovered defunct-regulator archives.
Next sub-projects: the per-body/authority `crawl_jobs` target-ref follow-up, then
the other acquisition workers (foreign-search, regional, manufacturer, MSN), and the
broad country-expansion data effort that feeds all of them.
