# Gap Scheduler (`plan` command) — Design Spec

**Date:** 2026-06-23
**Repo:** `aviation-safety-scrapers/control-plane`
**Sub-project:** 0 of the gap-driven coverage roadmap (the scheduler that drives all
acquisition workers built later).

---

## 1. Context & Goal

The control-plane already models *where* aviation-accident data is missing: 249
ISO-3166 countries, each with a `coverage_status`, a curated `priority_score`
(`expected_records × expected_source_quality ÷ effort_score`, already computed at
seed time in `model.PriorityScore`), policy flags, regional-body memberships, and
aircraft-origin routes. It also already defines the *pipeline* schema —
`crawl_jobs`, `crawl_errors`, `events`, `reports` — and a `CrawlJobType` enum that
enumerates every acquisition path on the roadmap (`wayback_cdx`,
`ntsb_foreign_search`, `bea_foreign_search`, `atsb_search`, `pdf_discovery`,
`icao_elibrary_search`, `archive_crawl`, `authority_health_check`,
`direct_request_needed`).

**What is missing:** nothing turns the coverage map into work. `crawl_jobs` is
never populated by Go code; the table is empty. Without a scheduler, "gap-driven"
has no driver and the acquisition workers (sub-projects 1–6) have no queue to
drain.

**Goal of this sub-project:** add a `plan` command that ranks coverage gaps by ROI
and emits `crawl_jobs` of the applicable type(s) per country — plus the minimal
data-model and seed additions needed for that ranking to be meaningful.

### Non-goals (YAGNI — explicitly out of scope)

- The acquisition workers themselves (Wayback, foreign-search, regional, PDF,
  manufacturer, MSN-enrichment) — separate sub-projects 1–6.
- The ingest bridge that flows `events`/`reports` into the FlightFinder app DB.
- Escalation / waterfall logic (cancel cheaper jobs once a higher-tier source
  succeeds). The scheduler emits *all* applicable job types up front; result-driven
  escalation is a later refinement.
- Broad overlay authoring for all ~150 group C/D countries — a later data
  sub-project. This spec authors only a bounded first batch (the 20 RAIO members).

---

## 2. Design Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Framing | **Gap-driven** | Control-plane is the brain; sources are tools it dispatches. |
| Ranking | **ROI** = `expected_records × expected_source_quality ÷ effort_score` | Already implemented as `countries.priority_score`. Scheduler just reads it. |
| Jobs per country | **All applicable job types at once**, each its own pending row | Simple v1 (no result feedback needed); the queue shows the full backlog; workers drain by type/priority. |
| Data scope | **0a mechanism + 0b data in one spec** | First overlay batch + `delegate_iso2` ship together so the scheduler ranks real data, not a 5-country stub. |

---

## 3. Architecture

Two parts, one spec.

### 3a. Mechanism — `plan` subcommand

A new subcommand in `internal/app/app.go`, in the style of the existing commands
(`export` prints; `import-*` writes). No new runtime dependencies.

```
aviation-coverage plan --db coverage.db                 # dry-run: prints ranked JSON plan
aviation-coverage plan --db coverage.db --enqueue       # writes pending crawl_jobs
aviation-coverage plan --db coverage.db --limit 50      # cap to top-N countries
```

**Algorithm:**

1. **Select candidate countries:** `SELECT … FROM countries WHERE policy_status !=
   'excluded' ORDER BY priority_score DESC, iso2 ASC`. The `iso2` tiebreak keeps
   output deterministic (matches the project's deterministic-export convention).
2. **For each country**, resolve the applicable `job_type`s from its
   `coverage_status` via the mapping table (§4).
3. **For each (country, job_type)**, resolve a `source_id` (§5).
4. **Idempotency & cadence gate (§6):** skip if a non-terminal job already exists;
   skip a re-emit if the cadence window has not elapsed.
5. **Output:** print a deterministic ranked JSON plan. With `--enqueue`, insert the
   surviving (country, job_type, source_id) rows into `crawl_jobs` with
   `status='pending'` inside a single transaction.

**Dry-run is the default** — `plan` never writes unless `--enqueue` is passed. This
mirrors how `export` is read-only and makes the command safe to run for inspection.

### 3b. Data — `delegate_iso2` + first overlay batch

1. **Migration `004_delegate.sql`:** add a nullable
   `delegate_iso2 TEXT REFERENCES countries(iso2)` column to `countries`. (New file;
   existing 001–003 are immutable under the checksum-drift guard in
   `migrations.go`.)
2. **Overlay field:** add optional `delegate_iso2` to the `overlayEntry` struct and
   `country_overlays.json`; `seed.go` writes it (NULL when absent). For a
   `delegated_to_foreign_authority` country this names the foreign authority's
   state (e.g. `delegate_iso2: "FR"` → BEA), which drives foreign-search job
   selection (§4).
3. **First overlay batch — the 20 RAIO member states** (structurally anchored, so
   their routing is already known):
   - ECCAA (Caribbean): DM, GD, KN, LC, VC
   - BAGAIA (West Africa): CV, GM, GH, GN, LR, NG, SL
   - IAC (CIS): RU, AM, AZ, BY, KZ, KG, TJ, TM

   Each gets `country_group` (C/D), `coverage_status = regional_raio`,
   `coverage_score`, `effort_score`, `expected_records`,
   `expected_source_quality`, `refresh_cadence`. Values authored during
   implementation from the category-C/D source research; **every authored row is
   verified against a cited basis** (regional body membership + known archive
   state) before commit. May be agent-assisted, but values are human/Fable-reviewed,
   not invented.

---

## 4. `coverage_status → job_type` mapping

The single source of truth for "what work does this gap imply". Lives as a Go map
in a new `internal/planner` package, table-driven and unit-tested per row.

| coverage_status | emitted job types |
|---|---|
| `direct_public_archive` | `authority_health_check`, `archive_crawl` |
| `source_exists_unstable` | `archive_crawl`, `wayback_cdx` |
| `delegated_to_foreign_authority` | foreign-search by `delegate_iso2`* + `icao_elibrary_search` |
| `regional_raio` | `archive_crawl` (regional body), `wayback_cdx` |
| `official_contact_only` | `direct_request_needed`, `icao_elibrary_search` |
| `no_public_archive` | `wayback_cdx`, `pdf_discovery`, `direct_request_needed` |
| `unknown` | `icao_elibrary_search`, `wayback_cdx` |
| `policy_excluded` | *(none — country filtered out before mapping)* |

\* **Foreign-search resolution:** map `delegate_iso2 → job_type`:
`US → ntsb_foreign_search`, `FR → bea_foreign_search`, `AU → atsb_search`. If
`delegate_iso2` is NULL or unmapped, fall back to `icao_elibrary_search` +
`wayback_cdx` only (no foreign-search guess). The three foreign-search job types in
the enum correspond exactly to these three accredited-rep states; other delegates
get the safe fallback until the enum is extended in a later sub-project.

---

## 5. Source resolution

`crawl_jobs.source_id` is `NOT NULL`. Each (country, job_type) must attach a
`sources` row. Resolution order:

1. **Regional jobs** (`regional_raio` coverage): attach the source linked to the
   country's regional body.
2. **Foreign-search jobs**: attach the foreign authority's source (NTSB / BEA /
   ATSB rows already in `sources.json`).
3. **Wayback / PDF / ICAO-eLibrary / direct-request**: attach a per-type
   "method" source. `sources.json` already has `source_type` values `wayback`,
   `trusted_index`, `icao_elibrary`; add named method-source rows for any job type
   that lacks one so resolution never returns NULL.
4. If no source resolves, **skip the job and record a warning in the plan output**
   (never emit a job with a dangling/invalid source).

The exact method-source rows to add are enumerated in the implementation plan.

---

## 6. Idempotency & cadence

- **No duplicate live jobs:** before emitting (country_id, job_type), skip if a row
  already exists for that pair with `status IN ('pending','running')`. Re-running
  `plan --enqueue` is a no-op when nothing has changed.
- **Cadence gate for completed jobs:** a (country, job_type) whose latest job is
  `success`/`failed`/`partial` is re-emitted only when
  `now − finished_at ≥ refresh_cadence`. Cadence strings (`weekly`, `quarterly`,
  …) map to durations in the planner; NULL cadence ⇒ default (quarterly).
- The dry-run plan annotates each candidate with one of: `would_enqueue`,
  `skipped_active`, `skipped_cadence`, `skipped_no_source` — so an operator sees
  exactly why each job is or isn't created.

---

## 7. Output format (dry-run plan)

Deterministic JSON to stdout (matches the `export` convention):

```json
{
  "generated_at": "2026-06-23T12:00:00Z",
  "candidate_countries": 246,
  "jobs_planned": 612,
  "jobs": [
    {
      "iso2": "NG",
      "priority_score": 240.0,
      "coverage_status": "regional_raio",
      "job_type": "archive_crawl",
      "source_id": 41,
      "decision": "would_enqueue"
    }
  ],
  "warnings": ["LR/atsb_foreign_search: no source resolved"]
}
```

With `--enqueue`, additionally print a one-line summary (`enqueued N, skipped M`)
and exit 0; exit 1 only on DB/transaction error.

---

## 8. Testing (TDD)

Written test-first, table-driven where possible:

1. **Ranking order:** countries returned in `priority_score DESC, iso2 ASC` order;
   deterministic across runs.
2. **Mapping coverage:** one case per `coverage_status` row in §4, asserting the
   exact emitted job-type set — including `delegate_iso2 → correct foreign-search`
   and the NULL/unmapped fallback.
3. **Policy filter:** `policy_excluded` / `policy_status='excluded'` countries never
   appear in the plan or `crawl_jobs`.
4. **Idempotency:** second `plan --enqueue` with no state change inserts zero rows;
   an existing `pending` job suppresses re-emit of that pair.
5. **Cadence gate:** a `success` job inside its cadence window is `skipped_cadence`;
   the same job past its window is `would_enqueue`.
6. **Source resolution:** regional/foreign/method sources resolve; an unresolvable
   pair becomes `skipped_no_source` with a warning, never a dangling FK.
7. **Migration `004_delegate.sql`:** migrate→seed→export round-trips
   `delegate_iso2`; existing migration checksum/name guards stay green.
8. **First-batch seed:** the 20 RAIO members load with the authored overlay fields
   and non-zero `priority_score`.

---

## 9. Files touched

- `internal/migrations/sql/004_delegate.sql` — new (nullable `delegate_iso2`).
- `internal/seed/data/country_overlays.json` — +20 RAIO-member rows, +`delegate_iso2`.
- `internal/seed/seed.go` — read/write `delegate_iso2`.
- `internal/seed/data/sources.json` — method-source rows for job types lacking one.
- `internal/planner/` — new package: mapping table, ranking query, idempotency +
  cadence gate, plan builder. Unit-tested.
- `internal/app/app.go` — wire the `plan` subcommand (flags: `--enqueue`,
  `--limit`, `--generated-at`).
- `README.md` — document the `plan` command.

---

## 10. Roadmap position

This is sub-project 0. Once the queue exists, sub-projects 1–6 each build one
acquisition worker that drains its `job_type`: 1) Wayback (`wayback_cdx`),
2) foreign-search, 3) regional, 4) PDF-discovery, 5) manufacturer, 6) MSN
enrichment — followed by the ingest bridge into FlightFinder occurrences. Each is
its own spec → plan → implementation cycle.
