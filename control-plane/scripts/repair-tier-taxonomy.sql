-- repair-tier-taxonomy.sql
--
-- One-time repair for GO-CP-7: rows written before PR #16 (which fixed
-- ResolveSource's hardcoded source_tier literals — see
-- internal/worker/extract/tiertaxonomy_test.go's doc comment: "the bug that
-- had regional_body and wayback at tier 2 and manufacturer at tier 3, all
-- disallowed") still carry the pre-fix tier on disk. Two independent
-- staleness paths:
--
--   1. `reports.source_tier` is a point-in-time SNAPSHOT copied from
--      ResolveSource's return value at report-insert time (see
--      extract/promote.go's INSERT INTO reports). Reports promoted before
--      PR #16 landed still carry whatever tier the code returned back then;
--      the code fix does not retroactively touch already-inserted rows.
--
--   2. `sources.source_tier` is looked-up-or-created via upsertSource's
--      `INSERT ... ON CONFLICT(canonical_url, source_type) DO NOTHING`
--      (extract/promote.go). Once a sources row exists for a given
--      (canonical_url, source_type), a later call passing the corrected tier
--      is silently a no-op — the stale tier persists in the `sources` table
--      itself, not just in reports.source_tier snapshots.
--
--      For 'wayback' specifically, this is NOT just a historical artifact:
--      engine.sh (the minipc ingest orchestrator, NOT in this repo) directly
--      creates synthetic wayback source rows at tier 2 on every run, so this
--      half of the repair may need periodic re-application until engine.sh
--      itself is fixed (tracked separately — out of scope for this repo).
--
-- Correct tiers, cross-checked against the CURRENT code (not just the
-- finding's prose) in internal/model/types.go's SourceTierAllowsType and
-- confirmed by the literals ResolveSource actually returns today:
--   - source_type='regional_body' (ECCAA/BAGAIA/IAC) -> tier 4
--     (extract/regional_source.go ResolveSource returns tier=4 hardcoded)
--   - source_type='wayback'                          -> tier 5
--     (extract/wayback_source.go ResolveSource returns tier=5 hardcoded)
-- Both match the finding's proposed numbers exactly — no discrepancy between
-- the finding and the code was found, so this script follows both.
--
-- Scope note (judgment call): the finding's reports.source_tier fix names
-- IAC specifically (the body confirmed corrupted on minipc), but the same
-- pre-#16 code path applied uniformly to ALL regional_body sources
-- (ECCAA/BAGAIA too) and to 'wayback' reports (the same commit fixed both
-- literals together per the tiertaxonomy_test.go comment). This script
-- therefore repairs reports.source_tier for every regional_body- and
-- wayback-credited report with a mismatched tier, not just IAC, since
-- narrowing to IAC alone would leave the identical bug live for the other
-- three source_types with no separate finding to catch it later.
--
-- Idempotent: every statement only touches rows whose source_tier does not
-- already match the target value, so re-running this script (including
-- against a DB where engine.sh has re-poisoned wayback sources since the
-- last run) is safe and a no-op on already-correct rows.
--
-- Do NOT run this from CI or from this branch — operator-run only:
--   sqlite3 /var/lib/flightfinder-coverage/coverage.db < control-plane/scripts/repair-tier-taxonomy.sql
-- (adjust the path to wherever minipc's control-plane DB lives)

BEGIN;

-- 1. sources rows for regional bodies (ECCAA/BAGAIA/IAC) stuck at a stale
--    tier from before PR #16 (upsertSource's ON CONFLICT DO NOTHING never
--    updated them once created).
UPDATE sources
   SET source_tier = 4
 WHERE source_type = 'regional_body'
   AND source_tier != 4;

-- 2. sources rows for wayback stuck at tier 2 — both the pre-#16 historical
--    case and engine.sh's ongoing synthetic-row creation (see header).
UPDATE sources
   SET source_tier = 5
 WHERE source_type = 'wayback'
   AND source_tier = 2;

-- 3. reports.source_tier snapshots that were copied from a regional_body
--    source before PR #16 fixed the tier the code credits. Confirmed live
--    example: IAC-credited reports at source_tier=2 (should be 4).
UPDATE reports
   SET source_tier = 4
 WHERE source_tier != 4
   AND source_id IN (SELECT id FROM sources WHERE source_type = 'regional_body');

-- 4. reports.source_tier snapshots copied from a wayback source before
--    PR #16 (same commit, same bug class as #3 — see header's scope note).
UPDATE reports
   SET source_tier = 5
 WHERE source_tier != 5
   AND source_id IN (SELECT id FROM sources WHERE source_type = 'wayback');

COMMIT;
