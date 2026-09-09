-- 013_extract_claim.sql
-- process-extract had no document claim. Crawl jobs have had an atomic one
-- since 012 (claimJob: a compare-and-set on status, checked via RowsAffected),
-- but the extract path selected pending documents and processed them with no
-- guard at all. MaxOpenConns(1) serialises writes WITHIN a process; it says
-- nothing about two processes, and the deploy runs process-extract from a
-- timer that can overlap a manual run or a slow previous tick.
--
-- The race is not theoretical: FindDuplicateEvent reads events and
-- PromoteDocument inserts into it, in separate transactions per document. Two
-- passes holding the same document both see an empty snapshot and both insert,
-- producing exactly the duplicate events the dedup keys exist to prevent.
--
-- A claim timestamp is used rather than a new extraction_status value:
-- extraction_status carries a CHECK constraint on four different tables, and
-- widening those in SQLite means rebuilding each one. NULL means unclaimed;
-- a claim older than the staleness window is reclaimable, so a process that
-- dies mid-document does not strand it (same shape as claimJob's stale-running
-- rule in 012).
ALTER TABLE staged_wayback_documents      ADD COLUMN extraction_claimed_at INTEGER;
ALTER TABLE staged_regional_documents     ADD COLUMN extraction_claimed_at INTEGER;
ALTER TABLE staged_foreign_documents      ADD COLUMN extraction_claimed_at INTEGER;
ALTER TABLE staged_manufacturer_documents ADD COLUMN extraction_claimed_at INTEGER;
