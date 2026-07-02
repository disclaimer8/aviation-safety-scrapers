'use strict';
/**
 * cli.js — ingest Wikidata aviation-accident entities into a standalone SQLite.
 *
 * Pipeline: query the Wikidata SPARQL endpoint for everything that is (a
 * subclass of) an aviation accident (wd:Q744913) → parse the result rows →
 * optionally enrich each with the lead text of its English Wikipedia article →
 * compose a narrative + probable-cause summary → write one row per entity.
 *
 * Usage:
 *   node src/cli.js build [--db ./wikidata-accidents.sqlite] [--enrich]
 *   node src/cli.js --selftest        # offline: parse → compose → db on a sample, no network
 *
 *   --enrich   also fetch English Wikipedia lead text per entity (slower, paced)
 */
const path = require('node:path');
const { parseWikidataResponse } = require('./parse');
const { fetchArticleText } = require('./enrich');
const { composeNarrative, composeProbableCause } = require('./compose');
const { openDb, upsert } = require('./db');

const SPARQL_URL = 'https://query.wikidata.org/sparql';
const UA = 'wikidata-ingest/1.0 (+https://github.com/disclaimer8/aviation-safety-scrapers)';
const FETCH_DELAY_MS = 50;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Page size mirrors the old single-shot LIMIT. Live event count (~3,273 as
// of writing) is well under one page, but multi-valued ?causeLabel/?date
// rows used to inflate the GROUP BY toward the cap even for a few thousand
// distinct events — hence also aggregating causes below (mirrors factors).
// MAX_PAGES is a sane ceiling (50k rows) so a runaway loop can't hang forever
// if Wikidata's data model changes again.
const PAGE_SIZE = 5000;
const MAX_PAGES = 10;

// GROUP_CONCAT both ?causeLabel and ?factorsLabels so neither multi-valued
// property (P1196 cause, P828 factor) inflates the per-event row count in
// the GROUP BY — a single event now always yields exactly one binding row.
function buildSparql(offset) {
  return `
SELECT ?event ?eventLabel ?description ?date
       (GROUP_CONCAT(DISTINCT ?causeLabelRaw; SEPARATOR=";;") AS ?causeLabel)
       (GROUP_CONCAT(DISTINCT ?factorLabel; SEPARATOR=";;") AS ?factorsLabels)
WHERE {
  ?event wdt:P31/wdt:P279* wd:Q744913 .
  OPTIONAL { ?event schema:description ?description FILTER (LANG(?description) = "en") }
  OPTIONAL { ?event wdt:P585 ?date }
  OPTIONAL {
    ?event wdt:P1196 ?cause .
    ?cause rdfs:label ?causeLabelRaw .
    FILTER (LANG(?causeLabelRaw) = "en")
  }
  OPTIONAL {
    ?event wdt:P828 ?factor .
    ?factor rdfs:label ?factorLabel .
    FILTER (LANG(?factorLabel) = "en")
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?event ?eventLabel ?description ?date
ORDER BY ?event
LIMIT ${PAGE_SIZE}
OFFSET ${offset}
`.trim();
}

// Backwards-compatible export: the first page of the query, as before.
const SPARQL = buildSparql(0);

async function fetchSparqlPage(offset) {
  const res = await fetch(`${SPARQL_URL}?query=${encodeURIComponent(buildSparql(offset))}`, {
    headers: { Accept: 'application/sparql-results+json', 'User-Agent': UA },
  });
  if (!res.ok) throw new Error(`Wikidata SPARQL ${res.status}`);
  return res.json();
}

// Paginate with OFFSET until a short (< PAGE_SIZE) page comes back, i.e. we
// found the end. A full-size final page is indistinguishable from "there's
// more", so if we hit MAX_PAGES while still getting full pages, that's a
// truncation: emit a loud, grep-able warning rather than silently dropping
// the remainder (see SILENT_FAIL_SUSPECT convention used by other workers).
async function fetchSparql() {
  const allBindings = [];
  let offset = 0;
  let page = 0;
  for (; page < MAX_PAGES; page++) {
    const json = await fetchSparqlPage(offset);
    const bindings = json?.results?.bindings || [];
    allBindings.push(...bindings);
    if (bindings.length < PAGE_SIZE) {
      return { results: { bindings: allBindings } };
    }
    offset += PAGE_SIZE;
    if (page + 1 < MAX_PAGES) await sleep(FETCH_DELAY_MS);
  }
  console.warn(
    `SILENT_FAIL_SUSPECT source=wikidata truncated pages=${MAX_PAGES} ` +
    `rows=${allBindings.length} — hit MAX_PAGES with a still-full page; ` +
    'results are likely incomplete. Raise MAX_PAGES or investigate row inflation.'
  );
  return { results: { bindings: allBindings } };
}

function slugify(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80);
}

// Turn one parsed Wikidata record into a standalone DB row. No app facts.
function rowFromRecord(rec, wikipediaText, now) {
  const narrative_text = composeNarrative({
    wikipediaText: wikipediaText || rec.narrative_text || '',
    facts: {},
  });
  const probable_cause = composeProbableCause({ rawCause: rec.probable_cause, facts: {} });
  const factors_json = rec.factors && rec.factors.length
    ? JSON.stringify(rec.factors.map((label) => ({ label, role: 'cause' })))
    : null;
  const event_date = rec.date ? rec.date.slice(0, 10) : null;
  const slug = slugify(`${event_date || 'unknown'}-${rec.label || rec.q_id}`);
  return {
    q_id: rec.q_id,
    source_url: `https://www.wikidata.org/wiki/${rec.q_id}`,
    label: rec.label || null,
    slug,
    event_date,
    narrative_text,
    // Provenance flag for db.upsert(): only true when composeNarrative()
    // actually had real Wikipedia lead text to work with (a `--enrich` run
    // that got a hit), as opposed to the fact-sentence + boilerplate filler
    // it falls back to otherwise. Lets a plain re-run avoid clobbering a
    // previously enriched narrative — see db.js.
    narrative_from_fetch: (wikipediaText || '').trim().length > 0 ? 1 : 0,
    probable_cause,
    factors_json,
    fetched_at: now,
    updated_at: now,
  };
}

async function runIngest({ dbPath, enrich }) {
  const db = openDb(dbPath);
  const json = await fetchSparql();
  const records = parseWikidataResponse(json);
  const now = Math.floor(Date.now() / 1000);
  let ingested = 0;
  let enriched = 0;

  for (const rec of records) {
    let wikipediaText = null;
    const title = rec.label && !/^Q\d+$/.test(rec.label) ? rec.label : null;
    if (enrich && title) {
      try {
        wikipediaText = await fetchArticleText(title);
        if (wikipediaText) enriched++;
      } catch (e) {
        console.warn(`[wikidata] wikipedia fetch failed for ${title}: ${e.message}`);
      }
      await sleep(FETCH_DELAY_MS);
    }
    upsert(db, rowFromRecord(rec, wikipediaText, now));
    ingested++;
  }
  db.close();
  return { ingested, enriched, total: records.length };
}

function selftest() {
  // A trimmed SPARQL-results payload — exercises parse → compose → db offline.
  const sample = {
    results: {
      bindings: [
        {
          event: { value: 'http://www.wikidata.org/entity/Q7727806' },
          eventLabel: { value: 'TWA Flight 800' },
          date: { value: '1996-07-17T00:00:00Z' },
          causeLabel: { value: 'fuel tank explosion' },
          factorsLabels: { value: 'electrical fault;;fuel vapor' },
        },
      ],
    },
  };
  const records = parseWikidataResponse(sample);
  if (records.length !== 1) throw new Error(`expected 1 record, got ${records.length}`);
  const os = require('node:os');
  const out = path.join(os.tmpdir(), `wikidata-selftest-${Date.now()}.sqlite`);
  const db = openDb(out);
  upsert(db, rowFromRecord(records[0], null, Math.floor(Date.now() / 1000)));
  const got = db.prepare('SELECT q_id, label, event_date, slug FROM accidents').get();
  const n = db.prepare('SELECT COUNT(*) c FROM accidents').get().c;
  db.close();
  const fs = require('node:fs');
  for (const f of [out, out + '-wal', out + '-shm']) fs.rmSync(f, { force: true });
  console.log(`selftest: wrote ${n} row(s); sample=${JSON.stringify(got)}`);
  if (n !== 1) throw new Error('expected exactly 1 row');
  if (got.q_id !== 'Q7727806') throw new Error(`bad q_id: ${got.q_id}`);
  console.log('selftest OK');
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes('--selftest')) { selftest(); return; }
  const dbIdx = args.indexOf('--db');
  const dbPath = path.resolve(dbIdx >= 0 ? args[dbIdx + 1] : 'wikidata-accidents.sqlite');
  const enrich = args.includes('--enrich');
  const r = await runIngest({ dbPath, enrich });
  console.log(`[wikidata] ingested=${r.ingested}/${r.total} wikipedia=${r.enriched} -> ${dbPath}`);
}

module.exports = {
  runIngest,
  rowFromRecord,
  fetchSparql,
  fetchSparqlPage,
  buildSparql,
  SPARQL,
  PAGE_SIZE,
  MAX_PAGES,
};

if (require.main === module) {
  main().catch((e) => { console.error(e); process.exit(1); });
}
