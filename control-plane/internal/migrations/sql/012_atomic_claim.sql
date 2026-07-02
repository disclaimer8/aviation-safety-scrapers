-- 012_atomic_claim.sql
-- GO-CP-9 fix: BuildPlan's HasActive read and Enqueue's INSERT run as separate,
-- non-serialized steps, so two concurrent planner runs can both decide
-- would_enqueue for the same (country_id, job_type) and double-enqueue it.
-- Symmetrically, worker ProcessPending loops claimed a job with a plain
-- `UPDATE crawl_jobs SET status='running' WHERE id=?` (no `AND status='pending'`
-- guard), so two overlapping ProcessPending invocations could both run the
-- same job. This migration adds the DB-level invariant that makes both races
-- impossible: at most one pending/running crawl_jobs row per (country_id,
-- job_type). Enqueue (planner.go) now targets this index with
-- `ON CONFLICT ... DO NOTHING`, and every worker's claim UPDATE now carries an
-- atomic `AND status = ...` guard checked via RowsAffected (see
-- wayback/runner.go, regional/runner.go, foreignsearch/runner.go).
--
-- Live minipc DBs predate this invariant and may already contain duplicate
-- (country_id, job_type) rows among pending/running crawl_jobs (e.g. from the
-- exact GO-CP-9 race this migration closes). Creating the unique index
-- directly would brick those DBs (migration fails outright on the first
-- duplicate). So, before creating the index, resolve pre-existing duplicates
-- deterministically: for each (country_id, job_type) group with more than one
-- pending/running row, keep the highest-id row (the most recently created,
-- since crawl_jobs.id is an autoincrement PRIMARY KEY) and retire the rest.
--
-- CrawlJobStatus (internal/model/enums.go) has no 'skipped'/'cancelled'
-- value — the legal terminal states are 'success', 'failed', 'partial',
-- 'manual_review'. Of these, 'failed' is the most truthful: these rows never
-- ran and never will run as themselves (their claim key is now owned by the
-- surviving row), which is exactly what 'failed' means for a crawl_jobs row
-- that never reached a terminal outcome under its own execution. 'error' is
-- set to an explanatory, greppable string rather than left NULL so this is
-- distinguishable from a normal crawl failure in any operational review.
WITH survivors AS (
  SELECT country_id, job_type, MAX(id) AS keep_id
    FROM crawl_jobs
   WHERE status IN ('pending', 'running')
   GROUP BY country_id, job_type
)
UPDATE crawl_jobs
   SET status = 'failed',
       finished_at = CAST(unixepoch('subsec') * 1000 AS INTEGER),
       error = 'duplicate_claim_resolved_by_migration_012_atomic_claim: ' ||
               'superseded by job id ' ||
               (SELECT s.keep_id FROM survivors s
                 WHERE s.country_id IS crawl_jobs.country_id
                   AND s.job_type = crawl_jobs.job_type) ||
               ' for the same (country_id, job_type); this row predates a ' ||
               'partial UNIQUE index on crawl_jobs(country_id, job_type) ' ||
               'WHERE status IN (''pending'',''running'') and was never ' ||
               'claimed under the old non-atomic worker UPDATE'
 WHERE status IN ('pending', 'running')
   AND id NOT IN (SELECT keep_id FROM survivors);

CREATE UNIQUE INDEX idx_crawl_jobs_active_claim
  ON crawl_jobs(country_id, job_type)
  WHERE status IN ('pending', 'running');
