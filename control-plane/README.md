# aviation-coverage — Coverage Control Plane

A single Go binary that manages **coverage metadata** for the aviation safety
data pipeline: which countries exist, which authorities investigate accidents
there, how healthy each source is, and which ones are policy-excluded.  It does
not replace the independent source scrapers in `sources/`, `sources-node/`, or
`sources-go/` — it coordinates the routing and policy layer that sits above
them.

Requires Go 1.24.0 or later.

## Build

```bash
cd control-plane
go build -o aviation-coverage ./cmd/aviation-coverage
```

The binary has no runtime dependencies beyond the SQLite file you point it at.

## Commands

### migrate

Creates (or upgrades) the 19 canonical and provenance tables in a SQLite file.
Idempotent — safe to run on an already-migrated database.

```bash
./aviation-coverage migrate --db coverage.db
```

### seed

Populates the database with the canonical ISO 3166-1 country list (249 rows),
curated policy/coverage overlays, regional body definitions, source records, and
aircraft origin routes.  All data is embedded at compile time; no network access
is required.  Idempotent — repeated calls produce the same rows.

```bash
./aviation-coverage seed --db coverage.db
```

### import-aia

Fetches (or reads) the ICAO Accident Investigation Authorities (AIA) page and
stages each parsed authority record into the database.  Prints a JSON result
object to stdout:

```json
{"RunID":1,"Status":"partial","Parsed":10,"Applied":7,"Warnings":4,"Conflicts":0,"Unchanged":false}
```

**Live (default):** fetches `https://www.icao.int/safety/airnavigation/AIG/Pages/AIA-States.aspx`

```bash
./aviation-coverage import-aia --db coverage.db
```

**Offline / recovery:** reads a locally saved copy of the page instead:

```bash
./aviation-coverage import-aia --db coverage.db --source-file fixtures/icao/aia.html
```

The `--source-url` flag overrides the URL recorded in the snapshot metadata
without triggering a live fetch when `--source-file` is also present.

Additional tuning flags: `--user-agent`, `--timeout`.

### import-raio

Same as `import-aia` but for the ICAO Regional Accident and Incident
Investigation Organizations (RAIO) page.  Distinguishes RAIO-class bodies from
Investigative Coordination Mechanism (ICM) bodies and preserves existing
curated authority mappings.

```bash
./aviation-coverage import-raio --db coverage.db
# or offline:
./aviation-coverage import-raio --db coverage.db --source-file fixtures/icao/raio.html
```

### validate

Runs integrity checks across all provenance, authority, and conflict tables and
prints a JSON report to stdout.  Exits 0 when there are no errors; exits 1 when
errors are found.

```bash
./aviation-coverage validate --db coverage.db
# treat open import conflicts as errors too:
./aviation-coverage validate --db coverage.db --strict-conflicts
```

### export

Writes a deterministic JSON snapshot of all 249 countries with their effective
authority contact fields, provenance labels, and regional body memberships.
Raw snapshot blobs, private operator notes, and internal database IDs are
excluded.

```bash
./aviation-coverage export \
  --db coverage.db \
  --format json \
  --output coverage.json \
  --generated-at 2026-06-22T12:00:00Z   # optional; defaults to now
```

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

## Override precedence

For every mutable authority field (website URL, archive URL, contact email,
contact phone) the effective value is resolved in this order:

1. **Curated override** (`field_overrides` table) — always wins; a differing
   incoming ICAO value records an open conflict but never overwrites.
2. **Incoming ICAO snapshot value** — applied when non-empty and no override
   exists.
3. **Existing stored value** — preserved when the incoming value is empty but a
   stored value exists (upstream removal), which also records a removal
   conflict.

The `validate` command surfaces open conflicts; the `--strict-conflicts` flag
promotes them to errors.

## Import run statuses

| Status | Meaning |
|--------|---------|
| `success` | All records parsed and applied without warnings. |
| `partial` | At least one record was applied but warnings or parse errors were present (e.g. a malformed fixture row). Exits 0. |
| `failed` | The import could not complete (fetch error, DB error). Exits 1. |
| `unchanged` | The incoming snapshot matches the previously stored one exactly; no rows were written. Exits 0. |

A `partial` result from the offline fixture (`fixtures/icao/aia.html`) is
expected — the fixture contains one intentionally malformed record for
parser-resilience testing.

## Policy-excluded countries

Three countries are seeded with `policy_status = "excluded"` and
`coverage_status = "policy_excluded"`: **AF** (Afghanistan), **KP** (North
Korea), and **SY** (Syria).  These countries will never be scheduled for direct
acquisition jobs.  The exclusion is applied at seed time; ICAO import data for
these countries is still staged and stored but no crawl jobs are generated.

## Live ICAO smoke (manual only)

The live `import-aia` and `import-raio` commands hit ICAO's public web servers
and are **not run in CI** — they are manual operator actions run against a
persistent database:

```bash
./aviation-coverage import-aia  --db /var/lib/coverage/coverage.db
./aviation-coverage import-raio --db /var/lib/coverage/coverage.db
./aviation-coverage validate    --db /var/lib/coverage/coverage.db
./aviation-coverage export \
  --db /var/lib/coverage/coverage.db \
  --format json \
  --output /var/lib/coverage/coverage.json
```

## What is NOT committed

The following are generated at runtime and must not be committed to the
repository:

- The compiled binary (`aviation-coverage`)
- SQLite database files (`*.db`, `*.db-wal`, `*.db-shm`)
- Exported JSON snapshots (`coverage.json`)

Committed seed data under `internal/seed/data/` (JSON files embedded at compile
time) is not affected by the `.gitignore` rules.

## Fetching live ICAO pages (Cloudflare)

The default `import-aia`/`import-raio` live fetch **fails** against the real
ICAO website.  ICAO sits behind Cloudflare, which returns a non-retriable 404
to Go's stdlib HTTP client based on TLS/HTTP2 fingerprint — this is **not** a
User-Agent issue: `curl` with any `--user-agent` string gets a 200 from
a residential or desktop connection, while Go's net/http client (data-centre
TLS fingerprint) is rejected.  The Go fetcher is correct; this is environmental
bot-protection.

**Supported operational path:** fetch the page out-of-band where a real
browser / residential egress is available (the project's mini-PC), save the
HTML, and import with `--source-file`:

```bash
# on the mini-PC (residential egress; a real-browser TLS fingerprint passes Cloudflare):
control-plane/scripts/fetch-icao.sh aia  > /tmp/aia.html
control-plane/scripts/fetch-icao.sh raio > /tmp/raio.html

# then on the control-plane host:
aviation-coverage import-aia  --db coverage.db --source-file aia.html  --source-url https://www.icao.int/safety/AIG/AIA
aviation-coverage import-raio --db coverage.db --source-file raio.html
```

**Expected import statuses:**

- `partial` — some records applied but unresolved territories or observers
  remain.  This is **expected**, not a failure:
  - AIA: dependent territories / non-sovereign entities (e.g. DT/NCS) have no
    ISO country match.
  - RAIO: non-country observers such as BEA or NTSB appear in observer lists
    and cannot be resolved to a seeded ISO country.
- `failed` — a 0-record result (e.g. a Cloudflare block page or structural
  change to the ICAO page) is a **hard failure**.  Check the page with
  `fetch-icao.sh` and compare against the committed fixtures.

## Running tests

```bash
cd control-plane
go test ./...
```

All tests are offline — they use in-process SQLite databases and the committed
fixture files under `fixtures/`.
