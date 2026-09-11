#!/usr/bin/env node
// Load FAA per-year laser-incident XLSX files into a normalized SQLite laser.db.
// Source: faa.gov/about/initiatives/reported-laser-incidents-* (US-gov public
// domain). Schema varies by year (sheet name, header-row offset, FlightID
// present-or-not, Excel-serial dates) so we map by header NAME, not position.
const fs = require('fs');
const path = require('path');
const X = require('xlsx');
const Database = require('better-sqlite3');

const DIR = process.argv[2] || __dirname;
const OUT = process.argv[3] || path.join(DIR, 'laser.db');

// Per-file ceiling on rows whose parsed date falls outside the workbook's year.
// Above it we assume a layout change rather than source typos and stop.
const REPAIR_MAX = 25;

// header text (lowercased, contains) -> normalized column
const MAP = [
  [/incident date|^date/, 'incident_date'],
  [/incident time|^time/, 'incident_time'],
  [/flight\s*id|^flight/, 'flight_id'],
  [/aircraft/, 'aircraft'],
  [/altitude/, 'altitude'],
  [/airport/, 'airport'],
  [/laser color|color/, 'laser_color'],
  [/injury/, 'injury'],
  [/city/, 'city'],
  [/state/, 'state'],
];
function mapHeader(h) {
  const s = String(h || '').trim().toLowerCase();
  for (const [re, col] of MAP) if (re.test(s)) return col;
  return null;
}
function toISO(v) {
  if (v == null || v === '') return null;
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  if (typeof v === 'number') { // Excel serial
    const d = X.SSF ? X.SSF.parse_date_code(v) : null;
    if (d && d.y) return `${d.y}-${String(d.m).padStart(2, '0')}-${String(d.d).padStart(2, '0')}`;
  }
  const m = /^(\d{1,2})\/(\d{1,2})\/(\d{2,4})/.exec(String(v));
  if (m) { let yr = +m[3]; if (yr < 100) yr += 2000; return `${yr}-${String(+m[1]).padStart(2, '0')}-${String(+m[2]).padStart(2, '0')}`; }
  return null;
}

if (fs.existsSync(OUT)) fs.unlinkSync(OUT);
const db = new Database(OUT);
db.pragma('journal_mode = WAL');
db.exec(`CREATE TABLE laser_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_date TEXT, incident_year INTEGER, incident_time TEXT,
  flight_id TEXT, aircraft TEXT, altitude TEXT, airport TEXT,
  laser_color TEXT, injury TEXT, city TEXT, state TEXT
)`);
const ins = db.prepare(`INSERT INTO laser_reports
  (incident_date, incident_year, incident_time, flight_id, aircraft, altitude, airport, laser_color, injury, city, state)
  VALUES (@incident_date,@incident_year,@incident_time,@flight_id,@aircraft,@altitude,@airport,@laser_color,@injury,@city,@state)`);

const files = fs.readdirSync(DIR).filter((f) => /^laser-\d{4}\.xlsx$/.test(f)).sort();
let total = 0;
for (const file of files) {
  const fileYear = parseInt(file.match(/(\d{4})/)[1], 10);
  const wb = X.readFile(path.join(DIR, file), { cellDates: true });
  // pick the comprehensive sheet: not "Dec ...", most rows
  let best = null, bestRows = -1;
  for (const name of wb.SheetNames) {
    if (/^\s*dec\b/i.test(name)) continue;
    const aoa = X.utils.sheet_to_json(wb.Sheets[name], { header: 1, blankrows: false });
    if (aoa.length > bestRows) { best = { name, aoa }; bestRows = aoa.length; }
  }
  if (!best) continue;
  const { aoa } = best;
  // find header row = first row containing "incident date"
  let hi = aoa.findIndex((r) => r.some((c) => /incident date/i.test(String(c || ''))));
  if (hi < 0) hi = 0;
  const headers = aoa[hi].map(mapHeader);
  const repaired = [];
  const tx = db.transaction((rows) => {
    for (const r of rows) {
      const rec = { incident_date: null, incident_time: null, flight_id: null, aircraft: null, altitude: null, airport: null, laser_color: null, injury: null, city: null, state: null };
      headers.forEach((col, i) => { if (col && r[i] != null && r[i] !== '') rec[col] = r[i]; });
      rec.incident_date = toISO(rec.incident_date);
      rec.incident_year = rec.incident_date ? parseInt(rec.incident_date.slice(0, 4), 10) : fileYear;
      rec.incident_time = rec.incident_time == null ? null : String(rec.incident_time);
      for (const k of ['aircraft', 'altitude', 'airport', 'laser_color', 'injury', 'city', 'state', 'flight_id']) rec[k] = rec[k] == null ? null : String(rec[k]).trim();
      if (!rec.airport && !rec.city && !rec.aircraft) continue; // skip blank/spacer rows
      // A parseable date can still be wrong. The 2025 workbook carries one
      // typo'd Excel serial (3205 -> 1908-10-09) that the aggregate's
      // BETWEEN 2000 AND 2030 window then drops, silently costing a report the
      // FAA's own published total counts. The file's year is authoritative, so
      // keep the row in it and drop the bogus date instead of the report.
      if (rec.incident_year !== fileYear) {
        repaired.push(`row date ${rec.incident_date} -> ${fileYear} (${rec.airport || rec.city || rec.aircraft})`);
        rec.incident_date = null;
        rec.incident_year = fileYear;
      }
      ins.run(rec);
    }
  });
  const body = aoa.slice(hi + 1);
  tx(body);
  const n = db.prepare('SELECT count(*) c FROM laser_reports').get().c - total;
  total += n;
  // A handful of typos is normal; a flood means the sheet or header row moved
  // and we are mis-reading the date column, which must not pass silently.
  if (repaired.length > REPAIR_MAX) {
    throw new Error(`${file}: ${repaired.length} rows dated outside ${fileYear} (limit ${REPAIR_MAX}) — sheet "${best.name}" layout probably changed`);
  }
  for (const msg of repaired) console.error(`${file}: ${msg}`);
  console.error(`${file}: sheet="${best.name}" +${n} rows${repaired.length ? ` (${repaired.length} out-of-year dates repaired)` : ''}`);
}
for (const c of ['incident_year', 'state', 'airport', 'aircraft', 'laser_color', 'injury']) {
  db.exec(`CREATE INDEX idx_laser_${c} ON laser_reports(${c})`);
}
const yr = db.prepare('SELECT incident_year y, count(*) c FROM laser_reports GROUP BY incident_year ORDER BY incident_year').all();
console.error(JSON.stringify({ total, byYear: yr }, null, 2));
db.close();
