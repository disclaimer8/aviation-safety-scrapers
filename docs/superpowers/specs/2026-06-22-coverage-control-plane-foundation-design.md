# Aviation Coverage Control Plane Foundation Design

**Date:** 2026-06-22

**Status:** Approved

## 1. Purpose

Build a production-ready control plane for aviation accident and serious-incident source coverage, with an initial release focused on:

- a canonical data model;
- complete ISO 3166 country seeds;
- curated country policy and coverage mappings;
- ICAO Accident Investigation Authorities (AIA) contact import;
- ICAO RAIO/ICM regional-body import;
- deterministic validation and export.

The control plane will live in this repository and remain separate from both the independent source scrapers and the `FLIGHT` application database.

## 2. Scope

### Included in the foundation release

- All schema and models required by the complete pipeline.
- Seed data for countries, status vocabularies, source tiers, regional bodies, problematic-country groups, policy exclusions, and initial aircraft-origin routes.
- ICAO AIA parser and importer.
- ICAO RAIO/ICM parser and importer.
- Immutable source snapshots, import runs, provenance, curated overrides, and conflict review.
- CLI commands for migration, seeding, import, validation, and export.
- Stable JSON export for downstream consumers such as `FLIGHT`.
- Offline fixture-based tests and an opt-in live smoke procedure.

### Deferred

- ICAO e-Library report import.
- NTSB, BEA, ATSB, TAIC, and other foreign-accredited-representative importers.
- Authority and source health checks.
- PDF discovery.
- Wayback CDX discovery.
- Event ingestion and automatic deduplication execution.
- Direct-request sending.
- Dashboard UI and reporting API.
- SQLite projection into `FLIGHT`.

The deferred workflows receive schema support where required but no production job implementation in this release.

## 3. Repository and technology

Create a self-contained Go module under:

```text
control-plane/
```

Technology:

- Go 1.24 or newer.
- SQLite with foreign-key enforcement and WAL mode.
- Versioned SQL migrations embedded with `go:embed`.
- Standard-library HTTP client with explicit timeouts and bounded response bodies.
- HTML parsing through a focused Go HTML parser dependency.
- A single CLI binary.

The existing Python, Node.js, and Go source scrapers remain independent. The control plane coordinates source metadata and coverage policy; it does not become a shared runtime library for those scrapers.

## 4. Component boundaries

```text
control-plane/
  cmd/aviation-coverage/       CLI entry point
  internal/config/             runtime configuration
  internal/database/           connection and transaction handling
  internal/migrations/         embedded SQL migrations
  internal/model/              domain types and enum validation
  internal/seed/               ISO and curated seed application
  internal/importer/aia/       ICAO AIA fetch, parse, stage, apply
  internal/importer/raio/      ICAO RAIO/ICM fetch, parse, stage, apply
  internal/provenance/         snapshots, runs, overrides, conflicts
  internal/validation/         cross-table invariant checks
  internal/export/             deterministic downstream export
  fixtures/                    offline ICAO source samples
  seeds/                       reviewable curated data files
```

Each importer follows the same boundary:

```text
fetch -> snapshot -> parse -> stage -> validate -> diff -> apply
```

Fetch and parse are independently testable. Applying staged records is transactional and does not depend on live network access.

## 5. CLI

The first release provides:

```text
aviation-coverage migrate
aviation-coverage seed
aviation-coverage import-aia
aviation-coverage import-raio
aviation-coverage validate
aviation-coverage export --format json --output PATH
```

Common flags:

- `--db PATH`
- `--source-file PATH` for offline or manually captured imports
- `--source-url URL` where a command supports live fetching
- `--user-agent VALUE`
- `--timeout DURATION`

`import-aia` and `import-raio` default to official ICAO URLs but accept a local source file for deterministic operation and incident recovery.

## 6. Canonical data model

SQLite enum fields use `CHECK` constraints. Foreign keys are enabled for every connection. Timestamps use UTC integer milliseconds. URLs remain textual and preserve the original value where provenance requires it.

### 6.1 Core requested tables

#### `countries`

- `id`
- `iso2`, unique
- `iso3`, unique
- `name`
- `region`
- `policy_status`: `allowed`, `indirect_public_only`, `excluded`
- `coverage_status`: `direct_public_archive`, `delegated_to_foreign_authority`, `regional_raio`, `official_contact_only`, `source_exists_unstable`, `no_public_archive`, `policy_excluded`, `unknown`
- `coverage_score`, 0-5
- `effort_score`, 1-5
- `expected_records`, non-negative
- `expected_source_quality`, 1-5
- `priority_score`
- `last_checked_at`
- `notes`

`priority_score` is derived as:

```text
expected_records * expected_source_quality / effort_score
```

The value is recalculated by seed and import logic; it is not independently curated.

#### `authorities`

- requested identity, country, type, contact, source, archive, status, dates, and notes fields;
- effective fields used by consumers;
- no parser writes directly over a curated effective value.

Authority types:

- `national_aai`
- `caa`
- `ministry`
- `regional_raio`
- `foreign_aai`
- `manufacturer_state_aai`
- `operator_state_aai`
- `registry_state_aai`

Authority statuses:

- `ok`
- `empty_archive`
- `tls_error`
- `nx_domain`
- `suspended`
- `forbidden`
- `changed_structure`
- `manual_review_needed`
- `unknown`

Uniqueness is based on country, normalized name, and authority type.

#### `regional_bodies`

- `id`
- unique `code`
- `name`
- `website_url`
- `source_url`
- `notes`

#### `regional_body_members`

- `regional_body_id`
- `country_id`
- `role`
- `source_url`

The composite key is regional body, country, and role.

#### `sources`

- requested identity, URL, type, tier, policy, active, and check fields;
- `health_status` using the authority health vocabulary.

Source types and tiers follow the requested six-tier policy. A canonical URL plus source type is unique.

#### `events`

All requested occurrence and aircraft fields, plus:

- `dedup_status`: `unreviewed`, `auto_merged`, `soft_linked`, `manual_review`, `distinct`
- `needs_official_confirmation`

Every event requires `confidence_score` and `dedup_status`.

#### `reports`

All requested report metadata, source tier, extraction status, copyright status, original URL, archived URL, accessed time, checksum, and optional local path.

Every report requires `source_tier` and `copyright_status`.

#### `event_source_links`

All requested source identity, URL, matching, reason, confidence, and creation fields.

#### `investigation_participants`

All requested state, authority, role, source, and notes fields.

Occurrence country remains on `events`; participant authorities never replace it.

#### `aircraft_origin_routes`

All requested manufacturer, design/manufacture state, expected authority/source, and priority fields.

The normalized aircraft pattern plus expected authority or source is unique.

#### `crawl_jobs`

All requested source, country, job type, status, timing, error, statistics, and creation fields.

#### `crawl_errors`

All requested job, URL, error type, message, and creation fields.

### 6.2 Foundation provenance tables

#### `import_runs`

Tracks importer, source URL, snapshot, start/end time, status, counts, and error summary.

Statuses:

- `running`
- `success`
- `partial`
- `failed`
- `unchanged`

#### `source_snapshots`

Stores:

- source identity;
- source URL;
- fetch time;
- HTTP metadata;
- SHA-256 checksum;
- immutable raw body or repository-relative artifact path;
- content type and byte size.

Source plus checksum is unique. Re-importing an identical checksum is idempotent.

#### `staged_authorities`

Stores parsed AIA records associated with an import run, including:

- ICAO country label;
- resolved country ID;
- authority name;
- raw contact block;
- parsed website, archive, email, and phone;
- ICAO update date;
- parse warnings;
- record checksum.

#### `staged_regional_bodies`

Stores parsed organization code, description, region, website, member labels, organization class, warnings, and record checksum.

#### `field_overrides`

Stores curated overrides by entity type, entity ID, field name, typed value, reason, author, and timestamps.

There is at most one active override for an entity field.

#### `import_conflicts`

Stores:

- import run and staged record;
- target entity and field;
- current effective value;
- incoming value;
- active override;
- conflict reason;
- review status and resolution.

Review statuses:

- `open`
- `accepted_incoming`
- `kept_curated`
- `resolved_manually`

#### `authority_requests`

Provides future direct-request state:

- authority;
- request status: `not_sent`, `sent`, `replied`, `bounced`, `no_response`;
- generated subject and body;
- sent/replied timestamps;
- response notes.

No email is sent in the foundation release.

## 7. Effective-value policy

Field precedence is:

```text
active curated override
-> latest valid ICAO imported value
-> curated seed/default value
-> null
```

Imported source facts, curated values, and effective values are not conflated.

Rules:

- An importer never overwrites an active curated override.
- A differing imported value creates an `import_conflicts` row.
- Non-conflicting imported changes may update effective state transactionally.
- Removing a value from an upstream page does not automatically erase the last known value; it produces a reviewable change.
- Every effective authority value can be traced to its seed, snapshot, or override.

## 8. Seed design

Seed files are reviewable, deterministic, and idempotent.

### 8.1 Countries

Seed all ISO 3166 countries, not only problematic countries. Overlay:

- policy status;
- coverage status;
- coverage and effort scores;
- expected records and expected source quality;
- A/B/C1/C2/C3/D grouping;
- notes and refresh guidance.

Policy-excluded countries are seeded according to project policy. Afghanistan, North Korea, and Syria must never be scheduled for direct acquisition when excluded; public non-sanctioned official sources remain eligible in `indirect_public_only` mode.

### 8.2 Regional bodies

Seed at minimum:

- ECCAA and OECS mappings;
- BAGAIA;
- IAC/MAK;
- ICAO-listed investigation cooperation mechanisms useful to the country model.

ICAO currently lists BAGAIA members as Cabo Verde, Gambia, Ghana, Guinea, Liberia, Nigeria, and Sierra Leone, and IAC members as Armenia, Azerbaijan, Belarus, Kazakhstan, Kyrgyzstan, Tajikistan, Turkmenistan, and the Russian Federation.

Curated seed mappings supplement ICAO when the official page is incomplete, ambiguous, or represents a cooperation mechanism rather than a delegated investigator.

### 8.3 Sources and tiers

Seed official source classes and the six requested tiers. Trusted indexes are marked discovery-only and private narrative/full-text storage is prohibited unless rights explicitly permit it.

### 8.4 Aircraft-origin routes

Seed the initial manufacturer routing families:

- United States -> NTSB;
- France -> BEA;
- Canada -> TSB;
- United Kingdom -> AAIB;
- Switzerland -> SUST;
- Italy -> ANSV;
- Brazil -> CENIPA;
- Ukraine -> NBAAI/legacy official routing.

These records support future routing but do not enqueue searches in this release.

## 9. ICAO AIA importer

Primary source:

```text
https://www.icao.int/safety/AIG/AIA
```

Process:

1. Fetch with a bounded body, timeout, retry/backoff, identifiable User-Agent, and redirect limit.
2. Store an immutable snapshot before parsing.
3. Split the contact directory into country records.
4. Preserve the complete raw contact block.
5. Parse authority name, email addresses, phone numbers, websites, references/delegations, and ICAO update date.
6. Resolve country labels to ISO countries through exact aliases.
7. Stage all records and warnings.
8. Validate the complete staging set.
9. Compute changes against current source-derived and effective values.
10. Apply valid, non-conflicting changes transactionally.
11. Record unresolved or malformed records without silently dropping them.

Special forms such as dependent territories, non-contracting states, `Refer to`, and `See` relationships are preserved and mapped explicitly where possible.

The parser must tolerate formatting errors in the source page, including inconsistent labels and obfuscated email forms. It must not invent corrected contact values when the page is internally inconsistent.

## 10. ICAO RAIO/ICM importer

Primary source:

```text
https://www.icao.int/safety/regional-safety-cooperation/List-of-RAIOs-and-ICMs
```

Process:

1. Fetch and snapshot using the same transport guarantees as AIA.
2. Parse RAIO and ICM sections separately.
3. Parse body code, name, region, member labels, website, and organization class.
4. Resolve each member to a country or record a validation warning.
5. Stage the complete import.
6. Diff against source-derived body and membership state.
7. Apply transactionally, respecting curated mappings.

Membership does not automatically imply delegated investigation authority. Coverage changes are rule-based:

- confirmed regional investigator membership may set `regional_raio` where no direct archive exists;
- cooperation-mechanism membership alone is informational unless curated policy assigns stronger meaning;
- curated delegations remain authoritative.

## 11. Import status and error behavior

An importer returns:

- `success` when all valid records apply;
- `partial` when usable records apply but unresolved records or conflicts remain;
- `failed` when fetch, snapshot, schema, or transaction failure prevents a valid apply;
- `unchanged` when the checksum already exists and no replay is requested.

A malformed country record does not discard other valid countries. A failed apply transaction changes no canonical records. Staging, warnings, and run diagnostics remain available for review.

## 12. Validation

`validate` checks:

- ISO2/ISO3 uniqueness and completeness;
- all enum and score ranges;
- foreign-key integrity;
- duplicate normalized authorities;
- unknown AIA/RAIO country labels;
- missing required A/B/C/D country mappings;
- required ECCAA, BAGAIA, and IAC/MAK mappings;
- policy-excluded countries cannot have direct crawl jobs;
- source tier and source type consistency;
- reports always have copyright status;
- events always have confidence and dedup status;
- effective authority values have provenance;
- open import conflicts are reported;
- priority scores equal the defined formula within numeric tolerance.

Invariant failures produce a non-zero exit code. Open review conflicts are reported separately and may be configured as warnings or failures in CI.

## 13. Export contract

The foundation release writes deterministic JSON:

```json
{
  "schema_version": 1,
  "generated_at": "2026-06-22T00:00:00Z",
  "countries": [],
  "authorities": [],
  "regional_bodies": [],
  "regional_body_members": [],
  "sources": [],
  "aircraft_origin_routes": []
}
```

Rules:

- stable ordering by ISO code and stable entity keys;
- no raw snapshots or private operational notes;
- effective values plus explicit provenance metadata;
- policy and coverage statuses included;
- timestamps use RFC 3339 UTC;
- output is written atomically.

`FLIGHT` consumes this export later; it does not share or directly mutate the control-plane database.

## 14. Testing

All automated tests are offline.

Required coverage:

- clean migration and repeat migration;
- schema constraints, indexes, and foreign keys;
- complete and idempotent ISO seed;
- country group and policy overlays;
- source tiers and aircraft-origin seed mappings;
- AIA fixture parsing;
- delegation/reference parsing;
- RAIO and ICM fixture parsing;
- ECCAA, BAGAIA, and IAC/MAK mappings;
- identical snapshot/import idempotency;
- curated override preservation;
- import conflict creation;
- partial imports;
- transactional rollback;
- policy-excluded behavior;
- deterministic JSON export;
- invalid enum and duplicate rejection.

Live ICAO smoke tests are explicit operator commands, not unit tests or default CI jobs.

## 15. Operational safeguards

- Respect ICAO terms and robots policy.
- Use low request rates and an identifiable contact-bearing User-Agent.
- Apply strict timeouts, retry ceilings, redirect ceilings, and response-size limits.
- Do not crawl policy-excluded government sources.
- Never treat absence of a national archive as absence of occurrence data.
- Never merge occurrence country with investigation authority.
- Never use trusted private indexes as a primary narrative source.
- Preserve source URL, access time, checksum, and archived URL fields throughout the schema.

## 16. Definition of done

The foundation release is complete when:

- the Go CLI builds as a single binary;
- migrations create the complete schema on an empty SQLite database;
- seeds load all ISO countries and approved mappings without duplicates;
- AIA and RAIO imports pass offline fixture tests;
- repeated imports are idempotent;
- curated fields survive conflicting imports and conflicts are reviewable;
- `validate` enforces the documented invariants;
- deterministic JSON export succeeds;
- operator documentation covers migration, seed, import, validation, export, and live smoke commands;
- the existing independent scrapers remain unaffected.
