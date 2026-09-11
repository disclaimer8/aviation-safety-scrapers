#!/usr/bin/env node
/*
 * uas-aggregate.js — fold the FAA UAS (drone) sighting dataset (uas.db, built
 * from the quarterly XLSX at faa.gov/uas/resources/public_records, US-gov public
 * domain) into an aggregate seed JSON for the drone-sightings vertical. Mirrors
 * the laser/wildlife aggregate pattern.
 */
const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');

const DB = process.argv[2] || path.join(process.env.HOME, 'uas-ingest', 'uas.db');
const OUT = process.argv[3] || path.join(__dirname, '..', 'data', 'uas-sightings.json');

const CITY_MIN = 30;
const NOTABLE_CAP = 400;
const RECENT_PER_CITY = 8;
const RECENT_SUMMARY_MAX = 360; // keep per-city narratives short; hub notable keeps 1000
const TOPN = 15;
const YEARS = 'incident_year BETWEEN 2015 AND 2030';

function slugify(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80); }
function dedupeBySlug(rows) { const seen = new Set(); return rows.filter((r) => (seen.has(r.slug) ? false : (seen.add(r.slug), true))); }

const db = new Database(DB, { readonly: true });
const all = (sql) => db.prepare(sql).all();
const one = (sql) => db.prepare(sql).get();

// Every query below is scoped to YEARS, so a row outside it leaves the seed
// without a trace. load-uas.js already refuses to build a db with more than a
// handful of those; this makes the exclusion visible on the aggregate side too,
// for a db built before that check existed or by hand.
const excluded = all(`SELECT incident_year year, count(*) c FROM uas_sightings
  WHERE incident_year IS NULL OR NOT (${YEARS}) GROUP BY incident_year ORDER BY c DESC`);
if (excluded.length) {
  const shown = excluded.map((r) => `${r.year === null ? 'no date' : r.year}: ${r.c}`).join(', ');
  const n = excluded.reduce((a, r) => a + r.c, 0);
  throw new Error(`${n} rows fall outside "${YEARS}" and would be dropped silently (${shown}) — fix the dates or widen the window deliberately`);
}
// near-miss / evasive markers (the newsworthy subset)

const totals = one(`SELECT count(*) total,
  min(incident_year) firstYear, max(incident_year) lastYear FROM uas_sightings WHERE ${YEARS}`);
const yearly = all(`SELECT incident_year year, count(*) total
  FROM uas_sightings WHERE ${YEARS} GROUP BY incident_year ORDER BY incident_year`);
const topStates = all(`SELECT state name, count(*) c FROM uas_sightings WHERE ${YEARS} AND state IS NOT NULL GROUP BY state ORDER BY c DESC LIMIT ${TOPN}`);
const topCities = all(`SELECT city||', '||state name, count(*) c FROM uas_sightings WHERE ${YEARS} AND city IS NOT NULL AND state IS NOT NULL GROUP BY city, state ORDER BY c DESC LIMIT ${TOPN}`);

const stateRows = all(`SELECT state name, count(*) total,
  min(incident_year) firstYear, max(incident_year) lastYear FROM uas_sightings
  WHERE ${YEARS} AND state IS NOT NULL GROUP BY state ORDER BY total DESC`);
const stateTopCities = db.prepare(`SELECT city name, count(*) c FROM uas_sightings WHERE state = ? AND city IS NOT NULL GROUP BY city ORDER BY c DESC LIMIT 8`);
const states = dedupeBySlug(stateRows.map((s) => ({ ...s, slug: slugify(s.name), topCities: stateTopCities.all(s.name) })));

// Each city carries its own yearly trend + newest reports so the city page
// never depends on the global newest-400 `notable` slice (13 of 80 cities
// never appeared there → thin single-sentence pages).
const cityRows = all(`SELECT city name, state, count(*) total
  FROM uas_sightings WHERE ${YEARS} AND city IS NOT NULL AND state IS NOT NULL
  GROUP BY city, state HAVING total >= ${CITY_MIN} ORDER BY total DESC`);
const cityYearly = db.prepare(`SELECT incident_year year, count(*) total
  FROM uas_sightings WHERE ${YEARS} AND city = ? AND state = ?
  GROUP BY incident_year ORDER BY incident_year`);
const cityRecent = db.prepare(`SELECT id, incident_date date,
  CASE WHEN length(summary) > ${RECENT_SUMMARY_MAX}
    THEN substr(summary,1,${RECENT_SUMMARY_MAX}) || '…' ELSE summary END summary
  FROM uas_sightings WHERE ${YEARS} AND city = ? AND state = ?
  ORDER BY incident_date DESC, id DESC LIMIT ${RECENT_PER_CITY}`);
const cities = dedupeBySlug(cityRows.map((c) => ({
  ...c, slug: slugify(`${c.name}-${c.state}`),
  yearly: cityYearly.all(c.name, c.state), recent: cityRecent.all(c.name, c.state),
})));

const yearRows = all(`SELECT incident_year year, count(*) total
  FROM uas_sightings WHERE ${YEARS} GROUP BY incident_year ORDER BY incident_year DESC`);
const yearTopStates = db.prepare(`SELECT state name, count(*) c FROM uas_sightings WHERE incident_year = ? AND state IS NOT NULL GROUP BY state ORDER BY c DESC LIMIT 8`);
const years = yearRows.map((y) => ({ ...y, topStates: yearTopStates.all(y.year) }));

const notable = all(`SELECT id, incident_date date, state, city, substr(summary,1,1000) summary
  FROM uas_sightings WHERE ${YEARS} ORDER BY incident_date DESC LIMIT ${NOTABLE_CAP}`);

const seed = {
  generatedAt: new Date().toISOString().slice(0, 10),
  source: 'FAA UAS (Drone) Sighting Reports',
  sourceUrl: 'https://www.faa.gov/uas/resources/public_records/uas_sightings_report',
  license: 'US Government Public Domain',
  gates: { CITY_MIN },
  hub: { ...totals, yearly, topStates, topCities },
  states, cities, years, notable,
};
fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, JSON.stringify(seed));
db.close();
console.error(JSON.stringify({ out: OUT, bytes: fs.statSync(OUT).size,
  counts: { states: states.length, cities: cities.length, years: years.length, notable: notable.length },
  hub: { total: totals.total, years: `${totals.firstYear}-${totals.lastYear}` } }, null, 2));
