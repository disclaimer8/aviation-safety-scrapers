'use strict';

const Database = require('better-sqlite3');

// Standalone SQLite store for ATSB (Australian Transport Safety Bureau)
// aviation investigations. One row per investigation_id. Columns mirror
// atsbParse.parseDetail() plus the composed narrative fields.
const INT_COLUMNS = new Set(['fatalities_parsed', 'fatalities_estimated']);
const FACT_COLUMNS = [
  'investigation_id', 'source_url', 'title', 'occurrence_date', 'normalized_date',
  'release_date', 'normalized_release_date', 'location_text', 'state', 'operator',
  'aircraft_manufacturer', 'aircraft_model', 'aircraft_registration', 'aircraft_serial',
  'occurrence_category', 'occurrence_class', 'highest_injury_level', 'fatalities_parsed',
  'fatalities_estimated', 'sector', 'damage', 'operation_type', 'departure_point',
  'destination', 'investigation_status', 'investigation_level', 'investigation_type',
  'report_status', 'summary_text', 'contributing_factors', 'other_factors',
  'other_findings', 'findings_text', 'safety_analysis_text', 'report_pdf_url',
];
const EXTRA_COLUMNS = ['narrative_text', 'probable_cause', 'fetched_at', 'updated_at'];

function colType(c) {
  if (INT_COLUMNS.has(c) || c === 'fetched_at' || c === 'updated_at') return 'INTEGER';
  return 'TEXT';
}

function openDb(dbPath) {
  const db = new Database(dbPath);
  db.pragma('journal_mode = WAL');
  const cols = [...FACT_COLUMNS, ...EXTRA_COLUMNS]
    .map((c) => `${c} ${colType(c)}${c === 'investigation_id' ? ' PRIMARY KEY' : ''}`)
    .join(',\n      ');
  db.exec(`
    CREATE TABLE IF NOT EXISTS accidents (
      ${cols}
    );
    CREATE INDEX IF NOT EXISTS idx_atsb_date ON accidents(normalized_date);
  `);
  return db;
}

// Upsert one investigation: factual columns from parseDetail + composed
// narrative_text / probable_cause (may be null for non-final reports).
function upsert(db, factRow, { narrative_text, probable_cause }, now) {
  const row = {};
  for (const c of FACT_COLUMNS) {
    let v = factRow[c];
    if (v === undefined) v = null;
    row[c] = v;
  }
  row.narrative_text = narrative_text ?? null;
  row.probable_cause = probable_cause ?? null;
  row.fetched_at = now;
  row.updated_at = now;
  const allCols = [...FACT_COLUMNS, ...EXTRA_COLUMNS];
  const setClause = allCols.filter((c) => c !== 'investigation_id')
    .map((c) => `${c} = excluded.${c}`).join(',\n      ');
  db.prepare(`
    INSERT INTO accidents (${allCols.join(', ')})
    VALUES (${allCols.map((c) => '@' + c).join(', ')})
    ON CONFLICT(investigation_id) DO UPDATE SET
      ${setClause}
  `).run(row);
}

function getById(db, id) {
  return db.prepare('SELECT * FROM accidents WHERE investigation_id = ?').get(id);
}

module.exports = { openDb, upsert, getById, FACT_COLUMNS };
