# Wayback Worker — Discovery + Acquisition — Design Spec

**Date:** 2026-06-23
**Repo:** `aviation-safety-scrapers/control-plane`
**Sub-project:** 1 of the gap-driven coverage roadmap (the first acquisition worker
that drains the `crawl_jobs` queue the gap scheduler — sub-project 0 — produces).

---

## 1. Context & Goal

The gap scheduler (sub-project 0, merged `origin/main@288b55f`) populates `crawl_jobs`
with `wayback_cdx` rows for countries whose `coverage_status` implies a missing or
unstable archive (`unknown`, `source_exists_unstable`, `no_public_archive`,
`regional_raio`). Nothing yet executes those jobs.

**End goal (user-chosen): full extraction** — recover accident reports from defunct
regulator websites preserved in the Internet Archive, OCR them, extract structured
event fields, and produce `events`/`reports`. That end goal is a multi-stage
pipeline whose network/OCR/LLM stages are fragile and must be isolated. This spec
covers **only the first two stages — discovery and acquisition** — which are pure
Go with an injectable fetch layer and are fully offline-testable. OCR, LLM field
extraction, and promotion into `events`/`reports` are **Spec 2** (a separate
sub-project).

**This spec's deliverable:** given pending `wayback_cdx` jobs, resolve each
country's defunct-archive target, query the Internet Archive CDX index for archived
PDFs, stage the discovered snapshots, download the archived PDFs to a local store
with checksums, and record per-job status/stats — all idempotently.

### Non-goals (out of scope here)

- OCR of downloaded PDFs (Spec 2 — calls the existing hetzner `ocr_extract`).
- LLM extraction of event fields from PDF text (Spec 2 — calls the home-pc Ollama
  service).
- Creating `events`/`reports` rows or any dedup/promotion (Spec 2).
- The other acquisition workers (foreign-search, regional, PDF-discovery,
  manufacturer, MSN) — later sub-projects.
- Broad authoring of `wayback_target`/overlays for all ~150 C/D countries — that is
  the parallel **country-expansion** data effort (§7), which this spec defines the
  methodology for and seeds a small pilot batch of, but does not complete.

---

## 2. Design Decisions (locked in brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| v1 depth | discovery + acquisition only (full extraction is the multi-spec goal) | Network/OCR/LLM isolated; this stage is pure-Go + injectable fetch, offline-testable. |
| Target-domain resolution | **hybrid**: overlay `wayback_target` → fallback `authorities.archive_url` → else skip + warning | Works on a fresh seed (authored targets) without requiring an `import-aia` run, but still uses authority data when present. |
| Country expansion authoring | **agent-assisted research in regional batches**, every row cited + verified before commit | Scales to ~150 countries; mirrors the verified RAIO-overlay-batch pattern. |
| Network discipline | live fetch behind an injectable `Fetcher` interface; real impl hits `web.archive.org` directly from the control-plane host | Wayback is **not** TLS-fingerprint-blocked like ICAO, so no mini-PC needed; tests use fixtures. |

---

## 3. Architecture

The control-plane Go binary stays the **coordinator**. New code:

- `internal/worker/wayback/` — the worker package: CDX-response parsing, target
  resolution, snapshot staging, download orchestration. Pure logic + an injectable
  `Fetcher`.
- A `Fetcher` interface — the only network seam:

  ```go
  type Fetcher interface {
      // CDX returns the raw Internet Archive CDX JSON for a domain query.
      CDX(ctx context.Context, domain string) ([]byte, error)
      // Get returns the bytes of an archived resource URL.
      Get(ctx context.Context, archivedURL string) ([]byte, error)
  }
  ```

  Real impl (`httpFetcher`) uses `net/http` against `web.archive.org`. Tests use a
  `fixtureFetcher` backed by files under `internal/worker/wayback/testdata/`.
- A new CLI subcommand `process-wayback` in `internal/app/app.go`, following the
  existing command pattern.

### Data flow (per job)

```
pending wayback_cdx job
  → resolve target domain (overlay wayback_target ▸ authority archive_url ▸ skip+warn)
  → Fetcher.CDX(domain)                → raw CDX JSON
  → parse + filter (PDF, collapse by digest)   → []Snapshot
  → stage each Snapshot into staged_wayback_documents (digest-dedup, idempotent)
  → for each staged doc: Fetcher.Get(archived raw URL) → write <store>/<iso2>/<digest>.pdf
      → record local_file_path + SHA-256 checksum + download_status
  → update crawl_jobs: status (success|partial|failed) + stats_json{found,staged,downloaded,errors}
  → crawl_errors row per failed fetch/download
```

---

## 4. Schema — migration `005_wayback.sql`

(New file; 001–004 are immutable under the checksum guard.)

1. **`countries.wayback_target TEXT`** — nullable. The defunct archive domain or
   URL-prefix to query in the CDX index (e.g. `caa.gov.xx` or
   `caa.gov.xx/accidents`). Authored via overlays.

2. **`staged_wayback_documents`** — mirrors the `staged_authorities` style:

   ```sql
   CREATE TABLE staged_wayback_documents (
     id INTEGER PRIMARY KEY,
     crawl_job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
     country_id INTEGER NOT NULL REFERENCES countries(id),
     original_url TEXT NOT NULL,        -- the live URL as captured (CDX 'original')
     archived_url TEXT NOT NULL,        -- web.archive.org/web/<ts>id_/<original>
     timestamp TEXT NOT NULL,           -- CDX 14-digit capture timestamp
     mimetype TEXT NOT NULL,
     digest TEXT NOT NULL,              -- CDX content digest (dedup key)
     length INTEGER,
     local_file_path TEXT,              -- set after download
     checksum TEXT,                     -- SHA-256 of downloaded bytes
     download_status TEXT NOT NULL DEFAULT 'pending' CHECK(download_status IN (
       'pending', 'downloaded', 'failed', 'skipped'
     )),
     created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
     UNIQUE(country_id, digest)
   ) STRICT;
   ```

   The `UNIQUE(country_id, digest)` constraint is the idempotency key: re-running a
   job for the same country never double-stages the same captured document.

3. **`seed.go` + `country_overlays.json`** — read/write the optional
   `wayback_target` overlay field (NULL when absent), same nullable-string pattern
   as `delegate_iso2`.

---

## 5. Worker behavior — `process-wayback`

```
aviation-coverage process-wayback --db coverage.db [--limit N] [--store-dir DIR]
```

- **Job selection:** `SELECT cj.* FROM crawl_jobs cj JOIN countries c ON
  c.id = cj.country_id WHERE cj.job_type='wayback_cdx' AND cj.status='pending'
  ORDER BY c.priority_score DESC, c.iso2 ASC` capped by `--limit` (0 = no cap).
- **Per job, transactionally mark `running`**, then:
  1. **Resolve target** (§2 hybrid). No target → mark job `failed` with a
     `crawl_errors` row (`error_type='unknown'`, message names the country) and
     continue. (A schedulable country with no resolvable Wayback target is a data
     gap worth surfacing, not a silent skip.)
  2. **CDX query** via `Fetcher.CDX`. Transport error → job `failed` +
     `crawl_errors` (`error_type` mapped from the failure: timeout/dns/tls/http_*).
  3. **Parse + filter:** drop the CDX header row; keep `statuscode` 200 captures
     with a PDF mimetype; collapse by `digest`. Malformed rows are skipped and
     counted as warnings (job becomes `partial`).
  4. **Stage** each surviving snapshot into `staged_wayback_documents`
     (`INSERT … ON CONFLICT(country_id, digest) DO NOTHING`).
  5. **Download** each newly-staged doc via `Fetcher.Get` of the raw archived URL
     (`web.archive.org/web/<timestamp>id_/<original>`), write to
     `<store-dir>/<iso2>/<digest>.pdf`, set `local_file_path`, compute and store the
     SHA-256 `checksum`, set `download_status='downloaded'`. A failed download sets
     `download_status='failed'` + a `crawl_errors` row; the job is `partial`.
  6. **Finalize job:** `success` if all staged docs downloaded with no warnings;
     `partial` if some failed/warned but at least one document was staged or
     downloaded; `failed` if step 1 or 2 failed. Always write
     `stats_json = {"found":F,"staged":S,"downloaded":D,"errors":E}` and set
     `finished_at`.
- `--store-dir` defaults to `./wayback-store`. Downloaded PDFs and the store dir are
  runtime artifacts (gitignored), like the SQLite DB.

**Idempotency:** the scheduler already won't re-enqueue a `pending`/`running` pair;
within a job, `ON CONFLICT(country_id, digest) DO NOTHING` means a re-run after a
crash re-stages nothing and only retries undownloaded docs.

---

## 6. Error handling

- Every network failure maps to a `crawl_errors` row with the right `error_type`
  (`timeout`/`dns_error`/`tls_error`/`http_403`/`http_404`/`http_500`/`unknown`) and
  the offending URL.
- A job never panics on a single bad document — bad rows/downloads degrade the job
  to `partial` and are counted, never aborting the batch.
- A job whose target cannot be resolved is `failed` with a clear message, never a
  silent success.

---

## 7. Country expansion (parallel data effort — methodology + pilot)

This spec defines the methodology and seeds a **pilot batch**; the full ~150-country
authoring runs separately (agent-assisted) and feeds both the scheduler and this
worker.

- **Method:** dispatch research agents in **regional batches** (Africa, LATAM, Asia,
  Oceania, …). For each country, the agent determines: is there a live official
  archive? is the regulator's old domain captured in the Wayback CDX index? who is
  the accredited delegate? — and proposes `country_group` (A–D), `coverage_status`,
  the four scores, and `wayback_target`. **Every proposed row carries a cited basis
  and is human/Fable-verified before commit**, exactly like the RAIO-overlay batch.
- **Pilot batch (in this spec):** ~6 countries with a known defunct regulator domain
  captured in Wayback, authored with `coverage_status` (`no_public_archive` or
  `source_exists_unstable`), scores, and a real `wayback_target`. These drive the
  worker's end-to-end fixture tests and a first live smoke run.
- The broad expansion is tracked in the roadmap memory, not blocked on this spec.

---

## 8. Testing (TDD)

1. **CDX parse:** a fixture CDX JSON → expected `[]Snapshot` (header dropped,
   non-PDF/non-200 filtered, digest-collapsed); a malformed-row fixture → counted
   warning, not a crash.
2. **Target resolution:** overlay `wayback_target` present → used; absent but
   `authorities.archive_url` present → used; neither → skip + warning. (Three cases.)
3. **Staging + dedup:** staging the same digest twice inserts one row
   (`ON CONFLICT` no-op); different digests insert distinct rows.
4. **Download + checksum:** `fixtureFetcher` returns known bytes → file written to
   `<store>/<iso2>/<digest>.pdf`, `checksum` equals the SHA-256 of those bytes,
   `download_status='downloaded'`.
5. **Job finalize:** all-ok → `success` + correct `stats_json`; one download failure
   → `partial` + a `crawl_errors` row; CDX transport error → `failed`.
6. **Migration `005_wayback.sql`:** migrate→seed→round-trips `wayback_target`;
   `staged_wayback_documents` accepts a row and enforces `UNIQUE(country_id,digest)`;
   existing migration checksum/name guards stay green.
7. **Pilot overlays:** the ~6 pilot countries seed with a non-NULL `wayback_target`
   and a Wayback-appropriate `coverage_status`.
8. **CLI `process-wayback`:** against a migrated+seeded DB with a fixtureFetcher
   wired in, a pending `wayback_cdx` job for a pilot country ends `success`/`partial`
   with staged+downloaded rows and a populated `stats_json`.

---

## 9. Files touched

- `internal/migrations/sql/005_wayback.sql` — new (`wayback_target` column +
  `staged_wayback_documents` table).
- `internal/seed/seed.go` + `internal/seed/data/country_overlays.json` — read/write
  `wayback_target`; +~6 pilot overlay rows.
- `internal/worker/wayback/` — new package: `Fetcher` interface, `httpFetcher`,
  CDX parsing, target resolution, staging, download, job runner. Plus
  `testdata/` fixtures and `fixtureFetcher`.
- `internal/app/app.go` — wire the `process-wayback` subcommand (`--limit`,
  `--store-dir`).
- `README.md` — document `process-wayback`.
- `.gitignore` — ignore the default `wayback-store/` directory.

---

## 10. Roadmap position

Sub-project 1, stage 1 of 2. **Spec 2** (next) consumes
`staged_wayback_documents` rows: OCR (hetzner) → LLM field extraction (home-pc
Ollama) → `events`/`reports` with provenance + dedup. The per-body/per-authority
`crawl_jobs` target-ref follow-up (from the sub-project-0 final review) is needed
before the foreign-search/regional workers, **not** for this Wayback worker (whose
target resolves from the country itself).
