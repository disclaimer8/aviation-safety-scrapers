'use strict';

const Database = require('better-sqlite3');

// Standalone SQLite store for Wikidata aviation-accident records.
// One row per Wikidata entity (q_id). No coupling to any external schema.
function openDb(dbPath) {
  const db = new Database(dbPath);
  db.pragma('journal_mode = WAL');
  db.exec(`
    CREATE TABLE IF NOT EXISTS accidents (
      q_id            TEXT PRIMARY KEY,
      source_url      TEXT,
      label           TEXT,
      slug            TEXT,
      event_date      TEXT,
      narrative_text  TEXT,
      probable_cause  TEXT,
      factors_json    TEXT,
      fetched_at      INTEGER,
      updated_at      INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_accidents_date ON accidents(event_date);
  `);
  return db;
}

// `row.narrative_from_fetch` (0/1) tells the upsert whether narrative_text
// was composed from an actual Wikipedia fetch (a `build --enrich` run) or is
// the boilerplate-padded fallback composeNarrative() produces when there's
// no (or too-short) Wikipedia text (see compose.js CONTEXT_BOILERPLATE). A
// plain `build` re-run (no --enrich) would otherwise overwrite a previously
// enriched, real Wikipedia narrative with that short boilerplate filler —
// so a non-fetch narrative only replaces the stored one when it is actually
// longer; a real fetch always wins (mirrors the mak upsertFacts-vs-
// setNarrative split: facts always refresh, narrative provenance matters).
function upsert(db, row) {
  const params = { narrative_from_fetch: 0, ...row };
  db.prepare(`
    INSERT INTO accidents
      (q_id, source_url, label, slug, event_date, narrative_text,
       probable_cause, factors_json, fetched_at, updated_at)
    VALUES
      (@q_id, @source_url, @label, @slug, @event_date, @narrative_text,
       @probable_cause, @factors_json, @fetched_at, @updated_at)
    ON CONFLICT(q_id) DO UPDATE SET
      source_url     = excluded.source_url,
      label          = excluded.label,
      slug           = excluded.slug,
      event_date     = excluded.event_date,
      narrative_text = CASE
                          WHEN @narrative_from_fetch = 1 THEN excluded.narrative_text
                          WHEN LENGTH(excluded.narrative_text) > LENGTH(COALESCE(narrative_text, '')) THEN excluded.narrative_text
                          ELSE narrative_text
                        END,
      probable_cause = excluded.probable_cause,
      factors_json   = excluded.factors_json,
      updated_at     = excluded.updated_at
  `).run(params);
}

module.exports = { openDb, upsert };
