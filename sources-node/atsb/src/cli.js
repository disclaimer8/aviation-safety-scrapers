'use strict';
/**
 * cli.js — ingest ATSB (Australian Transport Safety Bureau) aviation
 * investigations into a standalone SQLite.
 *
 * ATSB sits behind Akamai and serves a JS-hydrated investigations listing, so
 * egress is a HEADED Chromium (Playwright). See src/scrape.js for the why.
 *
 * Pipeline:
 *   discover : walk /investigations?...transport_mode=607 listing pages
 *   fetch    : load each /investigations/<id> detail page in the browser
 *   parse    : parseDetail(html) → factual columns
 *   build    : compose narrative (summary + safety analysis) + probable_cause
 *              (contributing factors) for Final reports; upsert
 *
 * Requires Playwright + Chromium:  npm i playwright && npx playwright install chromium
 * On a display-less host run under Xvfb:  xvfb-run -a node src/cli.js build
 *
 * Usage:
 *   node src/cli.js build [--db ./atsb-accidents.sqlite] [--max-pages N] [--start-page N]
 */
const path = require('node:path');
const atsbParse = require('./parse');
const { createScraper } = require('./scrape');
const { openDb, upsert, getById } = require('./db');

// Mirror the production field mapping: narrative from summary + safety
// analysis, probable cause from contributing factors (ATSB's equivalent of
// NTSB's probable cause). Only Final reports with findings get a narrative.
function composeNarrativeFields(factRow) {
  const status = (factRow.report_status || '').toLowerCase();
  if (status !== 'final' || !factRow.findings_text) {
    return { narrative_text: null, probable_cause: null };
  }
  const narrative_text = [factRow.summary_text, factRow.safety_analysis_text]
    .filter(Boolean).join('\n\n').slice(0, 8000) || null;
  const probable_cause = factRow.contributing_factors || factRow.findings_text || null;
  return { narrative_text, probable_cause };
}

async function runIngest({ dbPath, maxPages = Infinity, startPage = 0 }) {
  const db = openDb(dbPath);
  const scraper = createScraper();
  const summary = { listed: 0, upserted: 0, narrativesWritten: 0, errors: [] };
  try {
    const rows = await scraper.listSlugs({
      maxPages,
      startPage,
      onPage: (pg, n) => console.log(`[atsb] listing page ${pg}: +${n}`),
    });
    summary.listed = rows.length;

    for (const { investigation_id, detail_url } of rows) {
      try {
        // Skip the detail fetch if we already have a real probable_cause.
        const existing = getById(db, investigation_id);
        if (existing && existing.probable_cause && existing.probable_cause.length >= 100) continue;

        const html = await scraper.fetchDetailHtml(detail_url);
        const factRow = atsbParse.parseDetail(html, investigation_id);
        const narr = composeNarrativeFields(factRow);
        upsert(db, factRow, narr, Math.floor(Date.now() / 1000));
        summary.upserted++;
        if (narr.narrative_text) summary.narrativesWritten++;
      } catch (e) {
        summary.errors.push({ investigation_id, error: e.message });
        console.error(`[atsb] ${investigation_id} failed: ${e.message}`);
      }
    }
  } finally {
    await scraper.close();
    db.close();
  }
  return summary;
}

async function main() {
  const args = process.argv.slice(2);
  const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : null; };
  const dbPath = path.resolve(opt('--db') || 'atsb-accidents.sqlite');
  const maxPages = opt('--max-pages') ? Number(opt('--max-pages')) : Infinity;
  const startPage = opt('--start-page') ? Number(opt('--start-page')) : 0;
  const r = await runIngest({ dbPath, maxPages, startPage });
  console.log(`[atsb] listed=${r.listed} upserted=${r.upserted} narratives=${r.narrativesWritten} errors=${r.errors.length} -> ${dbPath}`);
}

module.exports = { runIngest, composeNarrativeFields };

if (require.main === module) {
  main().catch((e) => { console.error(e); process.exit(1); });
}
