'use strict';
//
// cli.js — memory-safety regression tests.
//
// Before this fix: downloadDump() buffered the whole avall.zip via
// res.arrayBuffer() + fs.writeFileSync(), and exportTables() read each
// exported CSV fully into one string via fs.readFileSync(...,'utf8') before
// parseCsv() re-parsed it into row objects — a double/triple memory hit for
// narratives.csv, which can be multi-hundred-MB. Both now stream instead.
//
// These tests cover:
//  1. parseCsv() (whole-string) keeps its exact original semantics —
//     quoted fields, embedded commas/newlines, doubled-quote escapes, CRLF,
//     blank-row skipping.
//  2. CsvStreamParser/parseCsvFile produce byte-for-byte identical row
//     objects to parseCsv() when the same CSV text is fed in arbitrary
//     chunk sizes — including splits that land exactly inside a quoted
//     field's embedded newline and exactly on the ""-escape boundary.
//  3. downloadDump() streams the response body to disk (via
//     stream/promises.pipeline) instead of buffering it, and still
//     enforces the non-ok / too-small-file guards.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { parseCsv, CsvStreamParser, parseCsvFile, downloadDump } = require('../src/cli');

describe('parseCsv (whole-string, reference semantics)', () => {
  it('parses a simple CSV into lowercase-keyed row objects', () => {
    const csv = 'Ev_Id,Ev_City\nE1,Minneapolis\nE2,Talkeetna\n';
    expect(parseCsv(csv)).toEqual([
      { ev_id: 'E1', ev_city: 'Minneapolis' },
      { ev_id: 'E2', ev_city: 'Talkeetna' },
    ]);
  });

  it('handles quoted fields with embedded commas and newlines', () => {
    const csv = 'ev_id,narr_accp\nE1,"The pilot, on approach,\nreported a loss of power."\n';
    const out = parseCsv(csv);
    expect(out).toEqual([
      { ev_id: 'E1', narr_accp: 'The pilot, on approach,\nreported a loss of power.' },
    ]);
  });

  it('handles doubled-quote escapes inside quoted fields', () => {
    const csv = 'ev_id,note\nE1,"He said ""mayday"" twice."\n';
    expect(parseCsv(csv)).toEqual([{ ev_id: 'E1', note: 'He said "mayday" twice.' }]);
  });

  it('skips CR in CRLF line endings', () => {
    const csv = 'ev_id,city\r\nE1,Nowhere\r\n';
    expect(parseCsv(csv)).toEqual([{ ev_id: 'E1', city: 'Nowhere' }]);
  });

  it('skips fully-blank rows', () => {
    const csv = 'ev_id,city\nE1,Nowhere\n\nE2,Elsewhere\n';
    expect(parseCsv(csv)).toEqual([
      { ev_id: 'E1', city: 'Nowhere' },
      { ev_id: 'E2', city: 'Elsewhere' },
    ]);
  });

  it('returns [] for empty input', () => {
    expect(parseCsv('')).toEqual([]);
  });

  it('flushes a trailing row with no terminating newline', () => {
    const csv = 'ev_id,city\nE1,Nowhere';
    expect(parseCsv(csv)).toEqual([{ ev_id: 'E1', city: 'Nowhere' }]);
  });
});

// Feeds `text` to a CsvStreamParser in chunks of the given sizes (or, for
// explicit split points, exact substrings) and returns the resulting rows.
function parseInChunks(text, chunkSize) {
  const parser = new CsvStreamParser();
  for (let i = 0; i < text.length; i += chunkSize) {
    parser.feed(text.slice(i, i + chunkSize));
  }
  parser.end();
  return parser.rows;
}

function parseAtSplitPoints(text, splitPoints) {
  const parser = new CsvStreamParser();
  let prev = 0;
  for (const p of splitPoints) {
    parser.feed(text.slice(prev, p));
    prev = p;
  }
  parser.feed(text.slice(prev));
  parser.end();
  return parser.rows;
}

describe('CsvStreamParser chunk-boundary parity with parseCsv', () => {
  const BIG_CSV_HEADER = 'ev_id,narr_accp,narr_cause\n';
  const QUOTED_MULTILINE_ROW =
    'E1,"The pilot reported engine roughness.\nOn final approach, power was lost.","Fuel starvation, ""confirmed"" by teardown."\n';
  const PLAIN_ROW = 'E2,Simple narrative with no quotes,Simple cause\n';
  const CSV = BIG_CSV_HEADER + QUOTED_MULTILINE_ROW + PLAIN_ROW;

  const EXPECTED = parseCsv(CSV);

  it('sanity: the fixture actually exercises embedded newline + escaped quotes', () => {
    expect(EXPECTED).toHaveLength(2);
    expect(EXPECTED[0].narr_accp).toContain('\n');
    expect(EXPECTED[0].narr_cause).toContain('"confirmed"');
  });

  it.each([1, 2, 3, 5, 7, 11, 17, 31, 64, 4096])(
    'matches parseCsv() when fed in %i-char chunks',
    (chunkSize) => {
      expect(parseInChunks(CSV, chunkSize)).toEqual(EXPECTED);
    }
  );

  it('matches parseCsv() when a chunk boundary lands exactly on the embedded newline inside a quoted field', () => {
    const idx = CSV.indexOf('\nOn final approach'); // the \n itself
    expect(parseAtSplitPoints(CSV, [idx + 1])).toEqual(EXPECTED); // split right after the \n
    expect(parseAtSplitPoints(CSV, [idx])).toEqual(EXPECTED);     // split right before the \n
  });

  it('matches parseCsv() when a chunk boundary lands exactly between the two quotes of a "" escape', () => {
    const idx = CSV.indexOf('""confirmed""');
    // Split between the first and second quote of the opening "" pair.
    expect(parseAtSplitPoints(CSV, [idx + 1])).toEqual(EXPECTED);
    // Split between the two closing quotes.
    const closing = CSV.indexOf('""', idx + 3);
    expect(parseAtSplitPoints(CSV, [closing + 1])).toEqual(EXPECTED);
  });

  it('matches parseCsv() when a chunk boundary lands exactly on the closing quote of a quoted field', () => {
    const closeIdx = CSV.indexOf('confirmed""') + 'confirmed"'.length; // right after the real closing structure begins
    expect(parseAtSplitPoints(CSV, [closeIdx])).toEqual(EXPECTED);
  });

  it('handles a single-character-at-a-time feed across the whole file', () => {
    expect(parseInChunks(CSV, 1)).toEqual(EXPECTED);
  });
});

describe('parseCsvFile (streaming from disk)', () => {
  let tmpDir;
  beforeEach(() => { tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ntsb-csv-test-')); });
  afterEach(() => { fs.rmSync(tmpDir, { recursive: true, force: true }); });

  it('produces the same rows as parseCsv() for a file with quoted embedded newlines', async () => {
    const csv =
      'ev_id,narr_accp\n' +
      'E1,"Line one.\nLine two, with a comma.\nLine three ""quoted""."\n' +
      'E2,Plain text\n';
    const file = path.join(tmpDir, 'narratives.csv');
    fs.writeFileSync(file, csv, 'utf8');

    const expected = parseCsv(csv);
    const got = await parseCsvFile(file);
    expect(got).toEqual(expected);
    expect(got).toHaveLength(2);
  });

  it('handles a file larger than one read-stream chunk (forces real chunk boundaries)', async () => {
    // Default highWaterMark for fs.createReadStream is 64KB; build a file
    // comfortably larger than that with quoted rows straddling chunk edges.
    const header = 'ev_id,narr_accp\n';
    let body = '';
    for (let i = 0; i < 2000; i++) {
      body += `E${i},"Narrative row ${i} with, a comma and ""quoted"" text spanning\nmultiple lines."\n`;
    }
    const csv = header + body;
    const file = path.join(tmpDir, 'big.csv');
    fs.writeFileSync(file, csv, 'utf8');
    expect(fs.statSync(file).size).toBeGreaterThan(64 * 1024);

    const expected = parseCsv(csv);
    const got = await parseCsvFile(file);
    expect(got).toHaveLength(2000);
    expect(got).toEqual(expected);
  });

  it('returns [] for an empty file', async () => {
    const file = path.join(tmpDir, 'empty.csv');
    fs.writeFileSync(file, '');
    expect(await parseCsvFile(file)).toEqual([]);
  });
});

describe('downloadDump (streamed to disk)', () => {
  let tmpDir;
  beforeEach(() => { tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ntsb-dl-test-')); });
  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
    delete global.fetch;
  });

  it('streams a large-enough response body to disk without buffering it whole', async () => {
    const payload = Buffer.alloc(1_500_000, 'a'); // > 1MB size guard
    global.fetch = jest.fn(async () => new Response(payload, { status: 200 }));

    const dest = path.join(tmpDir, 'avall.zip');
    await downloadDump(dest);

    expect(fs.statSync(dest).size).toBe(payload.length);
    expect(fs.readFileSync(dest).equals(payload)).toBe(true);
  });

  it('throws on a non-ok HTTP response', async () => {
    global.fetch = jest.fn(async () => new Response('nope', { status: 500 }));
    const dest = path.join(tmpDir, 'avall.zip');
    await expect(downloadDump(dest)).rejects.toThrow('HTTP 500');
  });

  it('throws when the downloaded file is too small (likely an error page)', async () => {
    global.fetch = jest.fn(async () => new Response('tiny error page', { status: 200 }));
    const dest = path.join(tmpDir, 'avall.zip');
    await expect(downloadDump(dest)).rejects.toThrow(/too small/);
  });
});
