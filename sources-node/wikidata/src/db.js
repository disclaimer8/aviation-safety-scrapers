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

function upsert(db, row) {
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
      narrative_text = excluded.narrative_text,
      probable_cause = excluded.probable_cause,
      factors_json   = excluded.factors_json,
      updated_at     = excluded.updated_at
  `).run(row);
}

module.exports = { openDb, upsert };
