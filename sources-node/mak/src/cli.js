'use strict';
/**
 * cli.js — ingest MAK (Interstate Aviation Committee, mak-iac.org) accident
 * reports into a standalone SQLite.
 *
 * Pipeline per year (current → 2004, descending):
 *   discover : list /rassledovaniya/?YEAR=<y> → accident slugs
 *   fetch    : GET each /rassledovaniya/<slug>/ detail page
 *   parse    : parseDetail(html) → factual columns
 *   build    : upsert facts; for FINAL reports with a PDF, download it and
 *              extractNarrative() → narrative_text + probable_cause
 *
 * MAK attaches a final-report PDF reliably only from ~2014 onward; older years
 * are mostly metadata-only. The descending year order surfaces PDF-bearing
 * reports first.
 *
 * Usage:
 *   node src/cli.js build [--db ./mak-accidents.sqlite] [--year 2018]
 *   node src/cli.js build --from 2014 --to 2026 --db ./mak.sqlite
 */
const path = require('node:path');
const makParse = require('./parse');
const { fetchWithRetry, listYear, sleep, PER_REQUEST_DELAY_MS, BASE } = require('./scrape');
const { openDb, upsertFacts, setNarrative, getBySlug } = require('./db');

const FIRST_YEAR = 2004;

function yearList({ year, from, to }) {
  if (year) return [year];
  const end = to || new Date().getUTCFullYear();
  const start = from || FIRST_YEAR;
  const out = [];
  for (let y = end; y >= start; y--) out.push(y);   // descending: PDFs first
  return out;
}

async function processSlug(db, slug) {
  const url = `${BASE}/rassledovaniya/${slug}/`;
  const now = Math.floor(Date.now() / 1000);
  const html = await fetchWithRetry(url);
  const factRow = makParse.parseDetail(html, slug);
  upsertFacts(db, factRow, now);

  // Narrative only for final reports that carry a PDF.
  if (factRow.status_flag !== 'final' || !factRow.report_pdf_final) {
    return { slug, narrativeWritten: false, reason: 'no-final-pdf' };
  }
  // Idempotency: skip the (large) PDF if we already have a real probable_cause.
  const existing = getBySlug(db, slug);
  if (existing && existing.probable_cause && existing.probable_cause.length >= 100) {
    return { slug, narrativeWritten: false, reason: 'already-extracted' };
  }

  await sleep(PER_REQUEST_DELAY_MS);
  let pdfBuf;
  try {
    pdfBuf = await fetchWithRetry(factRow.report_pdf_final, { binary: true, referer: url });
  } catch (e) {
    return { slug, narrativeWritten: false, reason: `pdf-unavailable: ${e.message}` };
  }
  let narrative;
  try {
    narrative = await makParse.extractNarrative(pdfBuf);
  } catch (e) {
    return { slug, narrativeWritten: false, reason: `pdf-parse: ${e.message}` };
  }
  setNarrative(db, slug, narrative, now);
  return { slug, narrativeWritten: true, pages: narrative.page_count };
}

async function runIngest({ dbPath, year, from, to }) {
  const db = openDb(dbPath);
  const years = yearList({ year, from, to });
  const summary = { yearsScanned: 0, slugsSeen: 0, narrativesWritten: 0, errors: [] };

  for (const y of years) {
    let slugs;
    try {
      slugs = await listYear(y);
      summary.yearsScanned++;
    } catch (e) {
      summary.errors.push({ year: y, error: e.message });
      console.error(`[mak] year ${y} listing failed: ${e.message}`);
      continue;
    }
    for (const slug of slugs) {
      summary.slugsSeen++;
      try {
        const r = await processSlug(db, slug);
        if (r.narrativeWritten) summary.narrativesWritten++;
      } catch (e) {
        summary.errors.push({ slug, error: e.message });
        console.error(`[mak] ${slug} failed: ${e.message}`);
      }
      await sleep(PER_REQUEST_DELAY_MS);
    }
    console.log(`[mak] year ${y} done — ${slugs.length} slugs`);
  }
  db.close();
  return summary;
}

async function main() {
  const args = process.argv.slice(2);
  const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : null; };
  const dbPath = path.resolve(opt('--db') || 'mak-accidents.sqlite');
  const year = opt('--year') ? Number(opt('--year')) : null;
  const from = opt('--from') ? Number(opt('--from')) : null;
  const to = opt('--to') ? Number(opt('--to')) : null;
  const r = await runIngest({ dbPath, year, from, to });
  console.log(`[mak] years=${r.yearsScanned} slugs=${r.slugsSeen} narratives=${r.narrativesWritten} errors=${r.errors.length} -> ${dbPath}`);
}

module.exports = { runIngest, processSlug, yearList };

if (require.main === module) {
  main().catch((e) => { console.error(e); process.exit(1); });
}
