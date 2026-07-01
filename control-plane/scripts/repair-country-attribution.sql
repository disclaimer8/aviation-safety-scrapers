-- repair-country-attribution.sql
--
-- One-time repair for GO-CP-1 (body-wide regional/foreign sources — ECCAA,
-- BAGAIA, IAC, BEA — misattributing every record they discover to whichever
-- country's crawl job happened to stage it first). This script does NOT
-- re-discover the correct country; the code fix (this branch) makes new
-- extractions resolve occurrence_country_id from the LLM's reading of the
-- report content instead. Rows extracted BEFORE the fix landed were promoted
-- with the wrong country baked in and the code fix does not touch them (their
-- staged row is already extraction_status='extracted', so they never re-enter
-- the extract queue). This script clears the wrong stamp on those rows; a
-- later re-run of the LLM-based resolution (out of scope here — would need a
-- separate re-extraction pass) can backfill the correct country.
--
-- Confirmed corrupted on minipc at authoring time: 21 staged_regional_documents
-- rows and 18 events, all via IAC (e.g. EW-307SL — a Belarus accident — and
-- UP-MI872 — a Kazakh Mi-8 — both recorded as RU).
--
-- Scope: every regional body currently wired (ECCAA/BAGAIA/IAC) and the BEA
-- authority in staged_foreign_documents are body-wide (see the doc comments in
-- control-plane/internal/worker/regional/{eccaa,bagaia,iac}.go and
-- control-plane/internal/worker/foreignsearch/bea.go). NTSB and ATSB are
-- excluded — they are genuinely filtered per country and were never affected.
--
-- Idempotent: every statement only touches rows that still carry a non-NULL
-- value, so re-running this script after it has already been applied is a
-- no-op. Safe to run inside a single transaction.
--
-- USAGE (operator-run only — NOT executed by this branch or by CI):
--   sqlite3 /var/lib/flightfinder-coverage/coverage.db < control-plane/scripts/repair-country-attribution.sql
-- (adjust the path to wherever minipc's control-plane DB lives)

BEGIN;

-- 1. Clear the wrong country claim on staged regional-body rows (ECCAA/BAGAIA/IAC).
--    Any row still 'pending'/'failed' will re-stage cleanly next run since the
--    code fix (regional/stage.go) now always passes country_id=NULL for these
--    bodies; this just brings already-staged rows in line with that.
UPDATE staged_regional_documents
   SET country_id = NULL
 WHERE country_id IS NOT NULL;

-- 2. Same for the BEA authority in staged_foreign_documents (NTSB/ATSB rows
--    are untouched — they were never body-wide).
UPDATE staged_foreign_documents
   SET country_id = NULL
 WHERE authority = 'bea'
   AND country_id IS NOT NULL;

-- 3. Clear the wrong occurrence_country_id on events that were promoted from
--    one of these body-wide staged docs (event_id is set once a staged row is
--    extracted). NULL is strictly safer than a confirmed-wrong stamp; a
--    follow-up re-extraction pass (out of scope for this script) can backfill
--    the true country via the LLM `country` field added by this fix.
UPDATE events
   SET occurrence_country_id = NULL
 WHERE occurrence_country_id IS NOT NULL
   AND (
     id IN (SELECT event_id FROM staged_regional_documents WHERE event_id IS NOT NULL)
     OR
     id IN (SELECT event_id FROM staged_foreign_documents WHERE authority = 'bea' AND event_id IS NOT NULL)
   );

COMMIT;

-- Verification (read-only; run after the repair to confirm the counts match
-- what you expect before/after):
--   SELECT body_code, COUNT(*) FROM staged_regional_documents WHERE country_id IS NOT NULL GROUP BY body_code;
--   SELECT COUNT(*) FROM staged_foreign_documents WHERE authority='bea' AND country_id IS NOT NULL;
--   SELECT COUNT(*) FROM events e
--     JOIN staged_regional_documents d ON d.event_id = e.id
--    WHERE e.occurrence_country_id IS NOT NULL;
-- All three should return 0 rows / zero counts after this script runs.
