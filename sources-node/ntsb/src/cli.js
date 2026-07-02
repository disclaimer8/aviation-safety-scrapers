'use strict';
/**
 * cli.js — builds a standalone `accidents` SQLite from the NTSB public bulk
 * aviation dump (avall.zip).
 *
 * Pipeline: download avall.zip from NTSB → unzip → mdb-export the
 * events / narratives / aircraft / occurrences tables → join → write a
 * self-contained `accidents` table with factual / analysis / cause narratives
 * plus metadata. The output is a single SQLite file you can read directly.
 *
 * Requires `mdbtools` on PATH (provides `mdb-export`) and `unzip`.
 *
 * Usage:
 *   node src/cli.js build [outPath]   # default ./ntsb-accidents.sqlite
 *   node src/cli.js --selftest        # join/mapping logic check, no download
 *
 * Refresh: NTSB updates the dump ~monthly; re-run to regenerate.
 */
const fs   = require('node:fs');
const os   = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { pipeline } = require('node:stream/promises');
const { Readable } = require('node:stream');
const Database = require('better-sqlite3');
const { buildWeatherSummary } = require('./parse');

const NTSB_BASE = 'https://data.ntsb.gov/avdata';
const FULL_DUMP = 'avall.zip';

// Incremental CSV parser: the same quoted-field state machine as the
// original single-shot parseCsv(), but fed in chunks so a multi-hundred-MB
// CSV (narratives.csv in particular) never has to exist as one giant JS
// string in memory. Handles quoted fields with embedded commas/newlines,
// doubled-quote escapes ("" inside a quoted field), and CRLF — including
// when any of those constructs straddle a chunk boundary.
//
// The one construct that needs special handling across chunks is the
// closing-vs-escaped-quote lookahead (a `"` while inside a quoted field is
// either the start of an escaped `""` or the end of the field, decided by
// peeking at the next character). If that lookahead character isn't
// available yet (it's the last char of the current chunk), the decision is
// deferred to the start of the next feed() call via `_pendingQuoteAtBoundary`.
class CsvStreamParser {
  constructor() {
    this.headers = null;
    this.rows = [];
    this._cur = '';
    this._row = [];
    this._inQ = false;
    this._pendingQuoteAtBoundary = false;
  }

  feed(chunk) {
    if (!chunk) return;
    let i = 0;

    if (this._pendingQuoteAtBoundary) {
      this._pendingQuoteAtBoundary = false;
      if (chunk[0] === '"') {
        // Escaped quote pair split across chunks: "" → literal ".
        this._cur += '"';
        i = 1;
      } else {
        // The deferred `"` was a closing quote; re-process chunk[0] fresh
        // under the (now) not-in-quotes state, same as the original loop
        // would on its next iteration.
        this._inQ = false;
        i = 0;
      }
    }

    for (; i < chunk.length; i++) {
      const c = chunk[i];
      if (this._inQ) {
        if (c === '"') {
          if (i + 1 < chunk.length) {
            if (chunk[i + 1] === '"') { this._cur += '"'; i++; continue; }
            this._inQ = false;
          } else {
            this._pendingQuoteAtBoundary = true;
            return;
          }
        } else {
          this._cur += c;
        }
      } else if (c === '"') {
        this._inQ = true;
      } else if (c === ',') {
        this._row.push(this._cur); this._cur = '';
      } else if (c === '\r') {
        /* skip */
      } else if (c === '\n') {
        this._row.push(this._cur); this._cur = '';
        this._commitRow();
      } else {
        this._cur += c;
      }
    }
  }

  _commitRow() {
    const row = this._row;
    this._row = [];
    if (!row.some(v => v !== '')) return; // skip blank rows
    if (!this.headers) {
      this.headers = row.map(h => String(h).toLowerCase());
      return;
    }
    const obj = {};
    for (let j = 0; j < this.headers.length; j++) obj[this.headers[j]] = row[j] ?? '';
    this.rows.push(obj);
  }

  // Flush a trailing row that wasn't newline-terminated (end of input).
  end() {
    if (this._pendingQuoteAtBoundary) {
      // No more input: the original text[i+1]===undefined case treats a
      // trailing quote-in-quotes as a closing quote.
      this._inQ = false;
      this._pendingQuoteAtBoundary = false;
    }
    if (this._cur || this._row.length) {
      this._row.push(this._cur);
      this._cur = '';
      this._commitRow();
    }
  }
}

// Whole-string CSV parse — kept for callers/tests that already have the
// full text in memory (and as the reference semantics CsvStreamParser must
// match exactly). exportTables() below uses the streaming variant instead.
function parseCsv(text) {
  const parser = new CsvStreamParser();
  parser.feed(text);
  parser.end();
  return parser.rows;
}

// Streaming file parse: reads in chunks via fs.createReadStream instead of
// fs.readFileSync(...,'utf8'), so the raw CSV text is never held as one
// giant string. A TextDecoder with {stream:true} absorbs any multi-byte
// UTF-8 sequence that a chunk boundary happens to split.
async function parseCsvFile(filePath) {
  const parser = new CsvStreamParser();
  const decoder = new TextDecoder('utf-8');
  for await (const chunk of fs.createReadStream(filePath)) {
    parser.feed(decoder.decode(chunk, { stream: true }));
  }
  parser.feed(decoder.decode()); // flush any pending decoder bytes
  parser.end();
  return parser.rows;
}

function toInt(v) { const n = parseInt(v, 10); return Number.isFinite(n) ? n : 0; }

// NTSB ev_date arrives as "MM/DD/YY HH:MM:SS" (2-digit year) or "MM/DD/YYYY".
// Normalize to ISO "YYYY-MM-DD" so the prompt reads cleanly and the worker's
// date validation has a stable format to match. 2-digit year window: 80-99 →
// 19xx, 00-29 → 20xx (NTSB data spans 1980s→present).
function normalizeDate(raw) {
  if (!raw) return '';
  const datePart = String(raw).trim().split(/\s+/)[0];
  const m = datePart.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2}|\d{4})$/);
  if (!m) return datePart;                 // already ISO or unknown — pass through
  let [, mm, dd, yy] = m;
  let year = yy.length === 4 ? Number(yy) : (Number(yy) >= 30 ? 1900 + Number(yy) : 2000 + Number(yy));
  return `${year}-${mm.padStart(2, '0')}-${dd.padStart(2, '0')}`;
}

// Pure mapping: joined NTSB tables → rows in the worker's `accidents` schema.
// Only events that have a non-empty factual narrative (narr_accp) are emitted —
// the worker's MIN_NARRATIVE_CHARS gate would drop the rest anyway.
function mapToAccidents(tables) {
  const narrByEv = new Map();
  for (const r of tables.narratives || []) narrByEv.set(r.ev_id, r);
  const acftByEv = new Map();
  for (const r of tables.aircraft || []) if (!acftByEv.has(r.ev_id)) acftByEv.set(r.ev_id, r);
  const occByEv = new Map();
  for (const r of tables.occurrences || []) if (!occByEv.has(r.ev_id)) occByEv.set(r.ev_id, r);

  const out = [];
  for (const ev of tables.events || []) {
    const narr = narrByEv.get(ev.ev_id);
    const factual = (narr?.narr_accp || '').trim();
    if (!factual) continue;                        // skip narrative-less events
    const acft = acftByEv.get(ev.ev_id) || {};
    const occ  = occByEv.get(ev.ev_id) || {};
    out.push({
      case_id:            ev.ev_id,
      ntsb_no:            ev.ntsb_no || ev.ev_id,
      event_date:         normalizeDate(ev.ev_date),
      city:               ev.ev_city || '',
      state_country:      [ev.ev_state, ev.ev_country].filter(Boolean).join(', '),
      aircraft:           [acft.acft_make, acft.acft_model].filter(Boolean).join(' ').trim(),
      registration:       acft.regis_no || '',
      operator:           acft.oper_name || acft.oper_dba || '',
      fatal:              toInt(ev.inj_tot_f),
      serious:            toInt(ev.inj_tot_s),
      minor:              toInt(ev.inj_tot_m),
      phase:              occ.phase_flt_spec || occ.phase_of_flight || occ.occurrence_code || '',
      weather:            buildWeatherSummary(ev) || '',
      factual_narrative:  factual,
      analysis_narrative: (narr?.narr_accf || '').trim(),
      probable_cause:     (narr?.narr_cause || '').trim(),
      docket_url:         `https://carol.ntsb.gov/event/${ev.ev_id}`,
    });
  }
  return out;
}

const COLUMNS = [
  'case_id', 'ntsb_no', 'event_date', 'city', 'state_country', 'aircraft',
  'registration', 'operator', 'fatal', 'serious', 'minor', 'phase', 'weather',
  'factual_narrative', 'analysis_narrative', 'probable_cause', 'docket_url',
];

function writeSqlite(rows, outPath) {
  if (fs.existsSync(outPath)) fs.rmSync(outPath);
  const db = new Database(outPath);
  db.pragma('journal_mode = WAL');
  db.exec(`
    CREATE TABLE accidents (
      case_id            TEXT PRIMARY KEY,
      ntsb_no            TEXT,
      event_date         TEXT,
      city               TEXT,
      state_country      TEXT,
      aircraft           TEXT,
      registration       TEXT,
      operator           TEXT,
      fatal              INTEGER,
      serious            INTEGER,
      minor              INTEGER,
      phase              TEXT,
      weather            TEXT,
      factual_narrative  TEXT,
      analysis_narrative TEXT,
      probable_cause     TEXT,
      docket_url         TEXT
    );
  `);
  const ins = db.prepare(
    `INSERT OR REPLACE INTO accidents (${COLUMNS.join(', ')}) VALUES (${COLUMNS.map(c => '@' + c).join(', ')})`
  );
  const tx = db.transaction((batch) => { for (const r of batch) ins.run(r); });
  tx(rows);
  db.close();
}

async function exportTables(mdbPath, csvDir) {
  const DEFS = [
    { csv: 'events',      mdb: 'events' },
    { csv: 'narratives',  mdb: 'narratives' },
    { csv: 'aircraft',    mdb: 'aircraft' },
    { csv: 'occurrences', mdb: 'Occurrences' },
  ];
  const tables = {};
  for (const d of DEFS) {
    const outFile = path.join(csvDir, `${d.csv}.csv`);
    execFileSync('mdb-export', [mdbPath, d.mdb], { stdio: ['ignore', fs.openSync(outFile, 'w'), 'inherit'] });
    // narratives.csv in particular can be huge; stream it instead of
    // fs.readFileSync(...,'utf8') + parseCsv(), which held the entire raw
    // text (plus the parsed rows) in memory at once.
    tables[d.csv] = await parseCsvFile(outFile);
    console.log(`  ${d.csv}: ${tables[d.csv].length} rows`);
  }
  return tables;
}

// Streams the download straight to disk instead of buffering the whole
// multi-hundred-MB avall.zip in memory via res.arrayBuffer() +
// fs.writeFileSync(). res.body is a WHATWG ReadableStream; Readable.fromWeb
// bridges it into a Node stream for stream/promises.pipeline.
async function downloadDump(dest) {
  const fileId = `C:\\avdata\\${FULL_DUMP}`;
  const url = `${NTSB_BASE}/FileDirectory/DownloadFile?fileID=${encodeURIComponent(fileId)}`;
  console.log(`Downloading ${url}`);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`NTSB download → HTTP ${res.status}`);
  if (!res.body) throw new Error('NTSB download → empty response body');
  await pipeline(Readable.fromWeb(res.body), fs.createWriteStream(dest));
  const sz = fs.statSync(dest).size;
  if (sz < 1_000_000) throw new Error(`download too small (${sz}b) — likely an error page`);
  console.log(`  ${(sz / 1e6).toFixed(1)} MB`);
}

async function main() {
  const args = process.argv.slice(2);

  if (args.includes('--selftest')) {
    const rows = mapToAccidents({
      events: [
        { ev_id: 'ERA24LA101', ev_date: '08/12/24 00:00:00', ev_city: 'Talkeetna', ev_state: 'AK', ev_country: 'USA',
          inj_tot_f: '2', inj_tot_s: '1', inj_tot_m: '0', wx_cond_basic: 'VMC' },
        { ev_id: 'NONARR1', ev_date: '2024-01-01', ev_city: 'Nowhere' },  // no narrative → dropped
      ],
      narratives: [
        { ev_id: 'ERA24LA101', narr_accp: 'The pilot reported a loss of engine power on final approach.', narr_cause: 'Fuel starvation.' },
        { ev_id: 'NONARR1', narr_accp: '' },
      ],
      aircraft: [{ ev_id: 'ERA24LA101', acft_make: 'CESSNA', acft_model: '172', regis_no: 'N12345', oper_name: 'PRIVATE' }],
      occurrences: [{ ev_id: 'ERA24LA101', phase_flt_spec: 'Approach' }],
    });
    console.log(JSON.stringify(rows, null, 2));
    const out = path.join(os.tmpdir(), `ntsb-selftest-${Date.now()}.sqlite`);
    writeSqlite(rows, out);
    const db = new Database(out, { readonly: true });
    const n = db.prepare('SELECT COUNT(*) c FROM accidents').get().c;
    const sample = db.prepare('SELECT case_id, aircraft, fatal, phase, docket_url FROM accidents').get();
    db.close(); fs.rmSync(out); fs.rmSync(out + '-shm', { force: true }); fs.rmSync(out + '-wal', { force: true });
    console.log(`\nselftest: wrote ${n} row(s); sample=${JSON.stringify(sample)}`);
    if (n !== 1) { console.error('FAIL: expected exactly 1 row (narrative-less dropped)'); process.exit(1); }
    console.log('selftest OK');
    return;
  }

  const positional = args.filter(a => a !== 'build');   // `build` is an optional verb
  const outPath = path.resolve(positional[0] || 'ntsb-accidents.sqlite');
  const tmpRoot = process.env.NTSB_TMPDIR || os.tmpdir();
  const tmpDir = fs.mkdtempSync(path.join(tmpRoot, 'ntsb-build-'));
  try {
    const zip = path.join(tmpDir, FULL_DUMP);
    await downloadDump(zip);
    execFileSync('unzip', ['-o', '-q', zip, '-d', tmpDir], { stdio: 'inherit' });
    const mdb = fs.readdirSync(tmpDir).find(f => f.toLowerCase().endsWith('.mdb'));
    if (!mdb) throw new Error('no .mdb in dump');
    console.log('Exporting tables via mdb-export…');
    const tables = await exportTables(path.join(tmpDir, mdb), tmpDir);
    const rows = mapToAccidents(tables);
    console.log(`Mapped ${rows.length} accidents with narratives.`);
    writeSqlite(rows, outPath);
    console.log(`Wrote ${outPath}`);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

module.exports = { mapToAccidents, parseCsv, parseCsvFile, CsvStreamParser, downloadDump, exportTables };

if (require.main === module) {
  main().catch((err) => { console.error('FAILED:', err.message); process.exit(1); });
}
