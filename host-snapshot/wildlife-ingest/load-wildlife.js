#!/usr/bin/env node
// Load FAA Wildlife Strike Database CSV (mdb-export of Public.accdb STRIKE_REPORTS)
// into a normalized SQLite wildlife.db. Node + better-sqlite3 (no python, no bun).
// Source: wildlife.faa.gov/assets/database.zip -> Public.accdb (US-gov public domain).

const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');
const { parse } = require('csv-parse');
const { parse: parseSync } = require('csv-parse/sync');

const CSV = process.argv[2] || path.join(__dirname, 'strike_reports.csv');
const OUT = process.argv[3] || path.join(__dirname, 'wildlife.db');

// Columns we promote to typed/indexed; everything else stored TEXT verbatim.
const INT_COLS = new Set(['INDEX_NR', 'INCIDENT_MONTH', 'INCIDENT_YEAR', 'NR_INJURIES', 'NR_FATALITIES', 'NUM_ENGS', 'NUM_SEEN', 'NUM_STRUCK', 'HEIGHT', 'SPEED', 'AOS', 'DISTANCE']);
const REAL_COLS = new Set(['AIRPORT_LATITUDE', 'AIRPORT_LONGITUDE', 'INCIDENT_LATITUDE', 'INCIDENT_LONGITUDE', 'COST_REPAIRS', 'COST_OTHER', 'COST_REPAIRS_INFL_ADJ', 'COST_OTHER_INFL_ADJ']);

function normDate(rawDate, yr4, mon) {
  // INCIDENT_DATE looks like "06/22/96 00:00:00"; INCIDENT_YEAR is authoritative 4-digit.
  if (!yr4) return null;
  let mm = mon, dd = null;
  const m = /^(\d{2})\/(\d{2})\/(\d{2})/.exec(rawDate || '');
  if (m) { mm = mm || parseInt(m[1], 10); dd = parseInt(m[2], 10); }
  if (!mm) return null;
  const p = (n) => String(n).padStart(2, '0');
  return dd ? `${yr4}-${p(mm)}-${p(dd)}` : `${yr4}-${p(mm)}-01`;
}

(async () => {
  if (!fs.existsSync(CSV)) { console.error('CSV not found:', CSV); process.exit(1); }
  if (fs.existsSync(OUT)) fs.unlinkSync(OUT);

  // read header first
  const header = await new Promise((res, rej) => {
    const rl = require('readline').createInterface({ input: fs.createReadStream(CSV) });
    rl.on('line', (l) => { rl.close(); res(l); });
    rl.on('error', rej);
  });
  const colsSync = parseSync(header, { columns: false })[0];

  const db = new Database(OUT);
  db.pragma('journal_mode = WAL');
  db.pragma('synchronous = OFF');

  const colDefs = colsSync.map((c) => {
    if (c === 'INDEX_NR') return `"${c}" INTEGER PRIMARY KEY`;
    if (INT_COLS.has(c)) return `"${c}" INTEGER`;
    if (REAL_COLS.has(c)) return `"${c}" REAL`;
    return `"${c}" TEXT`;
  });
  colDefs.push('normalized_date TEXT');
  db.exec(`CREATE TABLE strike_reports (${colDefs.join(', ')})`);

  const allCols = [...colsSync, 'normalized_date'];
  const insert = db.prepare(
    `INSERT OR REPLACE INTO strike_reports (${allCols.map((c) => `"${c}"`).join(',')}) VALUES (${allCols.map(() => '?').join(',')})`
  );

  const yrIdx = colsSync.indexOf('INCIDENT_YEAR');
  const monIdx = colsSync.indexOf('INCIDENT_MONTH');
  const dateIdx = colsSync.indexOf('INCIDENT_DATE');

  let n = 0;
  const tx = db.transaction((rows) => { for (const r of rows) insert.run(r); });
  let batch = [];

  const parser = fs.createReadStream(CSV).pipe(parse({ columns: false, from_line: 2, relax_quotes: true, skip_records_with_error: false }));
  for await (const rec of parser) {
    const vals = colsSync.map((c, i) => {
      let v = rec[i];
      if (v === '' || v === undefined) return null;
      if (c === 'INDEX_NR' || INT_COLS.has(c)) { const x = parseInt(v, 10); return Number.isNaN(x) ? null : x; }
      if (REAL_COLS.has(c)) { const x = parseFloat(v); return Number.isNaN(x) ? null : x; }
      return v;
    });
    const nd = normDate(rec[dateIdx], parseInt(rec[yrIdx], 10), parseInt(rec[monIdx], 10) || null);
    vals.push(nd);
    batch.push(vals);
    if (batch.length >= 5000) { tx(batch); n += batch.length; batch = []; if (n % 50000 === 0) console.log('  inserted', n); }
  }
  if (batch.length) { tx(batch); n += batch.length; }

  // facet indexes
  for (const c of ['INCIDENT_YEAR', 'AIRPORT_ID', 'SPECIES_ID', 'OPID', 'STATE', 'DAMAGE_LEVEL', 'AMA', 'normalized_date']) {
    db.exec(`CREATE INDEX idx_sr_${c.toLowerCase()} ON strike_reports("${c}")`);
  }
  const total = db.prepare('SELECT count(*) n FROM strike_reports').get().n;
  const yrs = db.prepare("SELECT min(INCIDENT_YEAR) a, max(INCIDENT_YEAR) b FROM strike_reports").get();
  const dmg = db.prepare("SELECT count(*) n FROM strike_reports WHERE DAMAGE_LEVEL IS NOT NULL AND DAMAGE_LEVEL NOT IN ('N','')").get().n;
  const fat = db.prepare("SELECT sum(NR_FATALITIES) f FROM strike_reports").get().f;
  console.log(JSON.stringify({ inserted: n, total, years: yrs, withDamage: dmg, totalFatalities: fat }, null, 2));
  db.close();
})();
