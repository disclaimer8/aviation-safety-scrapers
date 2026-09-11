#!/usr/bin/env node
// Load FAA UAS (drone) sighting quarterly XLSX into a normalized SQLite uas.db.
// Source: faa.gov/uas/resources/public_records/uas_sightings_report (US-gov
// public domain). Columns are stable: Date, State, City, Summary (+ junk
// Column1..N in some quarters, ignored). Map by header NAME.
const fs = require('fs');
const path = require('path');
const X = require('xlsx');
const Database = require('better-sqlite3');

const DIR = process.argv[2] || __dirname;
const OUT = process.argv[3] || path.join(DIR, 'uas.db');

// Must match the YEARS window in uas-aggregate.js. A row dated outside it is
// loaded but never reaches the seed, so it has to be counted here rather than
// disappearing between the two scripts.
const AGG_MIN_YEAR = 2015;
const AGG_MAX_YEAR = 2030;
// Per-file ceilings. A couple of oddities are source noise; a pile of them
// means the date column moved and we are reading the wrong values.
const UNDATED_MAX = 5;
const OFF_WINDOW_MAX = 5;
// FAA files are fiscal-year quarters, so the calendar window is derived from
// the name, not the name's year: fy24_q1 is Oct-Dec 2023. Sightings spill a day
// or two across a boundary in the real files (69 rows across all 27 quarters,
// none more than 2 days out), so only a date a month clear of its quarter is
// treated as a defect.
const OFF_QUARTER_DAYS = 31;
const OFF_QUARTER_MAX = 5;

// "fy24_q1.xlsx", "fy2020_q3_uas_sightings.xlsx" -> the quarter's calendar span.
function quarterWindow(file) {
  const m = /fy(\d{2,4})[_-]?q([1-4])/i.exec(file);
  if (!m) return null;
  let fy = parseInt(m[1], 10);
  if (fy < 100) fy += 2000;
  const q = parseInt(m[2], 10);
  const [y, mo] = { 1: [fy - 1, 10], 2: [fy, 1], 3: [fy, 4], 4: [fy, 7] }[q];
  return { from: Date.UTC(y, mo - 1, 1), to: Date.UTC(y, mo + 2, 0) };
}
const daysOutside = (iso, w) => {
  const t = Date.parse(`${iso}T00:00:00Z`);
  if (Number.isNaN(t)) return 0;
  if (t < w.from) return Math.round((w.from - t) / 86400000);
  if (t > w.to) return Math.round((t - w.to) / 86400000);
  return 0;
};

const MAP = [
  [/sigh|^date$/i, "incident_date"],
  [/^state$/i, 'state'],
  [/^city$/i, 'city'],
  [/^summary$/i, 'summary'],
];
function mapHeader(h) {
  const s = String(h || '').trim();
  for (const [re, col] of MAP) if (re.test(s)) return col;
  return null;
}
function toISO(v) {
  if (v == null || v === '') return null;
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  if (typeof v === 'number') { const d = X.SSF && X.SSF.parse_date_code(v); if (d && d.y) return `${d.y}-${String(d.m).padStart(2, '0')}-${String(d.d).padStart(2, '0')}`; }
  const m = /^(\d{1,2})\/(\d{1,2})\/(\d{2,4})/.exec(String(v));
  if (m) { let y = +m[3]; if (y < 100) y += 2000; return `${y}-${String(+m[1]).padStart(2, '0')}-${String(+m[2]).padStart(2, '0')}`; }
  const iso = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(v)); if (iso) return iso[0];
  return null;
}
const titleCase = (s) => String(s || '').toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase()).trim();

if (fs.existsSync(OUT)) fs.unlinkSync(OUT);
const db = new Database(OUT);
db.pragma('journal_mode = WAL');
db.exec(`CREATE TABLE uas_sightings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_date TEXT, incident_year INTEGER,
  state TEXT, city TEXT, summary TEXT )`);
const ins = db.prepare('INSERT INTO uas_sightings (incident_date, incident_year, state, city, summary) VALUES (@incident_date,@incident_year,@state,@city,@summary)');

const files = fs.readdirSync(DIR).filter((f) => /\.xlsx$/i.test(f) && /sighting|fy\d/i.test(f)).sort();
let total = 0;
for (const file of files) {
  const wb = X.readFile(path.join(DIR, file), { cellDates: true });
  // pick the sheet with the most rows
  let best = null, bestN = -1;
  for (const name of wb.SheetNames) {
    const aoa = X.utils.sheet_to_json(wb.Sheets[name], { header: 1, blankrows: false });
    if (aoa.length > bestN) { best = aoa; bestN = aoa.length; }
  }
  if (!best) continue;
  let hi = best.findIndex((r) => r.some((c) => /sigh|^date$/i.test(String(c || ''))) && r.some((c) => /^state$/i.test(String(c || ''))));
  if (hi < 0) hi = 0;
  const headers = best[hi].map(mapHeader);
  const win = quarterWindow(file);
  const undated = [];
  const offWindow = [];
  const offQuarter = [];
  const tx = db.transaction((rows) => {
    for (const r of rows) {
      const rec = { incident_date: null, state: null, city: null, summary: null };
      headers.forEach((col, i) => { if (col && r[i] != null && r[i] !== '') rec[col] = r[i]; });
      rec.incident_date = toISO(rec.incident_date);
      if (!rec.summary && !rec.city && !rec.state) continue;
      rec.incident_year = rec.incident_date ? parseInt(rec.incident_date.slice(0, 4), 10) : null;
      rec.state = rec.state ? titleCase(rec.state) : null;
      rec.city = rec.city ? titleCase(rec.city) : null;
      rec.summary = rec.summary ? String(rec.summary).replace(/\s+/g, ' ').trim() : null;
      const where = rec.city || rec.state || 'unknown place';
      if (rec.incident_year == null) undated.push(where);
      else if (rec.incident_year < AGG_MIN_YEAR || rec.incident_year > AGG_MAX_YEAR) offWindow.push(`${rec.incident_date} (${where})`);
      else if (win && daysOutside(rec.incident_date, win) > OFF_QUARTER_DAYS) offQuarter.push(`${rec.incident_date} (${where})`);
      ins.run(rec);
    }
  });
  tx(best.slice(hi + 1));
  const n = db.prepare('SELECT count(*) c FROM uas_sightings').get().c - total;
  total += n;
  // Anything reported here is a row the seed will not contain. Both laser data
  // failures were exactly this: an undercount with nothing anywhere to see it.
  for (const [label, list, cap] of [
    ['no parseable date', undated, UNDATED_MAX],
    [`dated outside ${AGG_MIN_YEAR}-${AGG_MAX_YEAR}, dropped by the aggregate`, offWindow, OFF_WINDOW_MAX],
    [`more than ${OFF_QUARTER_DAYS} days outside this quarter`, offQuarter, OFF_QUARTER_MAX],
  ]) {
    if (!list.length) continue;
    for (const item of list.slice(0, cap)) console.error(`${file}: ${label}: ${item}`);
    if (list.length > cap) {
      throw new Error(`${file}: ${list.length} rows ${label} (limit ${cap}) — the date column or sheet layout probably changed`);
    }
  }
  const flagged = undated.length + offWindow.length + offQuarter.length;
  console.error(`${file}: +${n}${flagged ? ` (${flagged} flagged)` : ''}`);
}
// A quarter missing from DIR costs roughly 400 sightings and looks like nothing
// at all in the output. This has already happened once from the other side: the
// FAA withdrew fy22_q2 through fy23_q3 from its site, and those quarters survive
// only in our copies. Loading a directory with a hole in it must not be quiet.
const loaded = files.map(quarterWindow).filter(Boolean).map((w) => {
  const d = new Date(w.from);
  return d.getUTCFullYear() * 4 + Math.floor(d.getUTCMonth() / 3);
}).sort((a, b) => a - b);
const missing = [];
for (let i = 1; i < loaded.length; i += 1) {
  for (let k = loaded[i - 1] + 1; k < loaded[i]; k += 1) {
    const y = Math.floor(k / 4);
    const mo = (k % 4) * 3 + 1;
    missing.push(`${y}-${String(mo).padStart(2, '0')}`);
  }
}
if (missing.length) {
  throw new Error(`gap in the quarterly files: nothing covers ${missing.join(', ')} — add the missing workbook(s) to ${DIR} or the seed will be short and look fine`);
}

for (const c of ['incident_year', 'state', 'city']) db.exec(`CREATE INDEX idx_uas_${c} ON uas_sightings(${c})`);
const yr = db.prepare('SELECT incident_year y, count(*) c FROM uas_sightings WHERE incident_year IS NOT NULL GROUP BY incident_year ORDER BY incident_year').all();
console.error(JSON.stringify({ total, byYear: yr }, null, 2));
db.close();
