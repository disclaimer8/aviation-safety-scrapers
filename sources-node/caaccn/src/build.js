#!/usr/bin/env node
// Build caaccn.db (table caaccn_accidents) — the source DB for the FlightFinder
// narrative pipeline (build-source-narratives.js --source caaccn). Mainland-China
// CAAC regional + MEM special-major + CAAC-central MU5735 reports. All 官方文件
// (PRC Copyright Law Art. 5 → not ©). Text already extracted to docs/*.txt.
const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');
const { DOCS_META, composeNarrative } = require('./meta');

// Resolved from this file, not from $HOME. The host arrangement had the
// builder in ~/china-ingest and the sync script in ~/caaccn-ingest expecting
// ~/caaccn-ingest/caaccn.db — two directories that only worked because
// somebody remembered the trick. Neither half ran on its own.
const ROOT = path.resolve(__dirname, '..');
const DOCS = process.env.CAACCN_DOCS || path.join(ROOT, 'docs');
const OUT = process.env.CAACCN_DB || path.join(ROOT, 'caaccn.db');

// reg + date drive the occurrences dedup ladder (reg:norm:date attaches to
// existing baaa/ASN coverage of MU5735 / Yichun / Baotou).


if (fs.existsSync(OUT)) fs.unlinkSync(OUT);
const db = new Database(OUT);
// Schema must match build-source-narratives sync contract (caaccn_accidents).
db.exec(`CREATE TABLE caaccn_accidents (
  case_id TEXT PRIMARY KEY, event_date TEXT, aircraft TEXT, registration TEXT, operator TEXT,
  location TEXT, country TEXT, lang TEXT, narrative_text TEXT, probable_cause TEXT,
  source_url TEXT, report_type TEXT, site_slug TEXT, built_at TEXT, fatalities_total INTEGER
)`);
const ins = db.prepare(`INSERT OR REPLACE INTO caaccn_accidents
  (case_id, event_date, aircraft, registration, operator, location, country, lang,
   narrative_text, probable_cause, source_url, report_type, site_slug, built_at, fatalities_total)
  VALUES (@case_id,@event_date,@aircraft,@registration,@operator,@location,'CN','zh',
   @narrative_text,@probable_cause,@source_url,@report_type,@site_slug,@built_at,@fatalities_total)`);

const now = new Date().toISOString();
let n = 0;
for (const m of DOCS_META) {
  const narr = composeNarrative(DOCS, m);
  if (narr === null) { console.error('MISSING', path.join(DOCS, `${m.f}.txt`)); continue; }
  ins.run({
    case_id: m.case_id, event_date: m.event_date, aircraft: m.aircraft, registration: m.reg,
    operator: m.operator, location: m.location, narrative_text: narr, probable_cause: null,
    source_url: m.url, report_type: m.report_type, site_slug: null, built_at: now, fatalities_total: m.fatal,
  });
  n++;
}
const rows = db.prepare('SELECT case_id, registration, event_date, length(narrative_text) len FROM caaccn_accidents ORDER BY case_id').all();
console.error(JSON.stringify({ inserted: n, table: 'caaccn_accidents', rows }, null, 2));
db.close();
