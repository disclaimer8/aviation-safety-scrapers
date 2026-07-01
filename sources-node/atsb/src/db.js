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
//
// Two guards against a parse-empty detail page clobbering previously-good
// data. A detail fetch that returns a challenge/interstitial page, or a page
// the parser can't read, yields a factRow with almost nothing set:
//
//   1. Refuse the upsert outright when the parsed row has neither an
//      occurrence_date nor a title — those are the two fields every real
//      ATSB investigation page carries; their absence means parseDetail was
//      fed something that isn't a real record.
//   2. For rows that DO pass guard 1, fact/narrative columns use
//      `COALESCE(excluded.col, col)` instead of a blind `col = excluded.col`.
//      A field that's genuinely null in an otherwise-valid fresh parse (e.g.
//      an optional field the redesign dropped) leaves the existing stored
//      value in place rather than nulling it — only a real, non-null new
//      value overwrites what's stored. fetched_at/updated_at always advance.
function upsert(db, factRow, { narrative_text, probable_cause }, now) {
  if (!factRow.occurrence_date && !factRow.title) {
    throw new Error(
      `atsb upsert refused for ${factRow.investigation_id || '(unknown id)'}: ` +
      'parseDetail produced neither occurrence_date nor title — treating as a ' +
      'parse failure (challenge page / unrecognised layout), not a real record.'
    );
  }

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

  const coalesceCols = [...FACT_COLUMNS, 'narrative_text', 'probable_cause']
    .filter((c) => c !== 'investigation_id');
  const setClause = [
    ...coalesceCols.map((c) => `${c} = COALESCE(excluded.${c}, ${c})`),
    'fetched_at = excluded.fetched_at',
    'updated_at = excluded.updated_at',
  ].join(',\n      ');

  const allCols = [...FACT_COLUMNS, ...EXTRA_COLUMNS];
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
