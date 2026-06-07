'use strict';

const Database = require('better-sqlite3');

// Standalone SQLite store for MAK (Interstate Aviation Committee) reports.
// One row per accident slug. Columns mirror makParse.parseDetail() plus the
// extracted narrative fields. No coupling to any external schema.
const FACT_COLUMNS = [
  'slug', 'source_url', 'event_date', 'normalized_date', 'aircraft_model',
  'registration', 'serial_number', 'operator', 'owner', 'departure_city',
  'departure_airport', 'destination_city', 'destination_airport',
  'location_text', 'lat', 'lon', 'fatalities', 'damage_level', 'hull_loss',
  'aviation_kind', 'work_type', 'remark', 'data_accuracy', 'status_flag',
  'investigation_status', 'investigation_closed_date', 'report_pdf_final',
  'report_pdf_interim', 'report_pdf_en',
];

function openDb(dbPath) {
  const db = new Database(dbPath);
  db.pragma('journal_mode = WAL');
  db.exec(`
    CREATE TABLE IF NOT EXISTS accidents (
      slug                      TEXT PRIMARY KEY,
      source_url                TEXT,
      event_date                TEXT,
      normalized_date           TEXT,
      aircraft_model            TEXT,
      registration              TEXT,
      serial_number             TEXT,
      operator                  TEXT,
      owner                     TEXT,
      departure_city            TEXT,
      departure_airport         TEXT,
      destination_city          TEXT,
      destination_airport       TEXT,
      location_text             TEXT,
      lat                       REAL,
      lon                       REAL,
      fatalities                INTEGER,
      damage_level              TEXT,
      hull_loss                 INTEGER,
      aviation_kind             TEXT,
      work_type                 TEXT,
      remark                    TEXT,
      data_accuracy             TEXT,
      status_flag               TEXT,
      investigation_status      TEXT,
      investigation_closed_date TEXT,
      report_pdf_final          TEXT,
      report_pdf_interim        TEXT,
      report_pdf_en             TEXT,
      narrative_text            TEXT,
      probable_cause            TEXT,
      page_count                INTEGER,
      fetched_at                INTEGER,
      updated_at                INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_mak_date ON accidents(normalized_date);
  `);
  return db;
}

// Insert/update the factual columns from parseDetail(). Leaves any existing
// narrative untouched (COALESCE keeps a previously-extracted narrative).
function upsertFacts(db, factRow, now) {
  const row = {};
  for (const c of FACT_COLUMNS) {
    let v = factRow[c];
    if (typeof v === 'boolean') v = v ? 1 : 0;   // hull_loss
    row[c] = v === undefined ? null : v;
  }
  row.fetched_at = now;
  row.updated_at = now;
  const cols = [...FACT_COLUMNS, 'fetched_at', 'updated_at'];
  const setClause = cols.filter((c) => c !== 'slug')
    .map((c) => `${c} = excluded.${c}`).join(',\n      ');
  db.prepare(`
    INSERT INTO accidents (${cols.join(', ')})
    VALUES (${cols.map((c) => '@' + c).join(', ')})
    ON CONFLICT(slug) DO UPDATE SET
      ${setClause}
  `).run(row);
}

function setNarrative(db, slug, { narrative_text, probable_cause, page_count }, now) {
  db.prepare(`
    UPDATE accidents
       SET narrative_text = @narrative_text,
           probable_cause = @probable_cause,
           page_count     = @page_count,
           updated_at     = @updated_at
     WHERE slug = @slug
  `).run({ slug, narrative_text, probable_cause, page_count, updated_at: now });
}

function getBySlug(db, slug) {
  return db.prepare('SELECT * FROM accidents WHERE slug = ?').get(slug);
}

module.exports = { openDb, upsertFacts, setNarrative, getBySlug, FACT_COLUMNS };
