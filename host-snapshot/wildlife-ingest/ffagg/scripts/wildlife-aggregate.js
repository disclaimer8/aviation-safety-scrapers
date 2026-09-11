#!/usr/bin/env node
/*
 * wildlife-aggregate.js — fold the FAA Wildlife Strike Database (wildlife.db,
 * built from wildlife.faa.gov/assets/database.zip -> Public.accdb, US-gov public
 * domain) into an aggregate seed JSON for the FlightFinder wildlife-strikes
 * vertical. Mirrors the SDR aggregate pattern (offline fold -> server/data/*.json
 * -> boot replaceAll). NO per-row rows shipped except a capped "notable" set.
 *
 * Usage: node wildlife-aggregate.js [wildlife.db] [out.json]
 * Default in:  ~/wildlife-ingest/wildlife.db   (mini-PC)
 * Default out: server/data/wildlife-strikes.json
 */
const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');

const DB = process.argv[2] || path.join(process.env.HOME, 'wildlife-ingest', 'wildlife.db');
const OUT = process.argv[3] || path.join(__dirname, '..', 'data', 'wildlife-strikes.json');

// Gates (thin-content). Tunable; the service/enumerator must use the same floors.
const AIRPORT_MIN = 50;
const SPECIES_MIN = 50;
const AIRCRAFT_MIN = 50;
const NOTABLE_CAP = 800;
const TOPN = 12;

const DAMAGE_LABELS = { N: 'None', M: 'Minor', 'M?': 'Uncertain', S: 'Substantial', D: 'Destroyed' };
const DAMAGING = "DAMAGE_LEVEL IN ('M','M?','S','D')"; // excludes None / null

function slugify(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80);
}

// slugify strips non-alphanumerics, so distinct ids can collide (e.g. FAA
// "KDEN" and the annotated "KDEN*" both → "kden"). The wildlife_* tables key on
// slug, so a collision would let a smaller row silently overwrite a larger one.
// Rows arrive ordered by total DESC, so keep the FIRST (largest) per slug.
function dedupeBySlug(rows) {
  const seen = new Set();
  return rows.filter((r) => (seen.has(r.slug) ? false : (seen.add(r.slug), true)));
}

const db = new Database(DB, { readonly: true });
const all = (sql, ...a) => db.prepare(sql).all(...a);
const one = (sql, ...a) => db.prepare(sql).get(...a);

console.error('aggregating', DB);

// ---- HUB rollups ----
const totals = one(`SELECT count(*) total,
  sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging,
  sum(COALESCE(NR_FATALITIES,0)) fatalities, sum(COALESCE(NR_INJURIES,0)) injuries,
  min(INCIDENT_YEAR) firstYear, max(INCIDENT_YEAR) lastYear FROM strike_reports`);

const yearly = all(`SELECT INCIDENT_YEAR year, count(*) total,
  sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging
  FROM strike_reports WHERE INCIDENT_YEAR IS NOT NULL GROUP BY INCIDENT_YEAR ORDER BY INCIDENT_YEAR`);

const topAirports = all(`SELECT AIRPORT_ID id, AIRPORT name, count(*) c FROM strike_reports
  WHERE AIRPORT_ID IS NOT NULL AND AIRPORT IS NOT NULL AND AIRPORT_ID <> 'UNKN' AND AIRPORT <> 'UNKNOWN'
  GROUP BY AIRPORT_ID ORDER BY c DESC LIMIT ${TOPN}`);
const topSpecies = all(`SELECT SPECIES name, count(*) c FROM strike_reports
  WHERE SPECIES IS NOT NULL AND SPECIES NOT LIKE 'Unknown%' GROUP BY SPECIES_ID ORDER BY c DESC LIMIT ${TOPN}`);
const topPhases = all(`SELECT PHASE_OF_FLIGHT name, count(*) c FROM strike_reports
  WHERE PHASE_OF_FLIGHT IS NOT NULL AND PHASE_OF_FLIGHT <> '' GROUP BY PHASE_OF_FLIGHT ORDER BY c DESC LIMIT ${TOPN}`);

// ---- per-AIRPORT ----
const airportRows = all(`SELECT AIRPORT_ID id, AIRPORT name, STATE state,
  max(AIRPORT_LATITUDE) lat, max(AIRPORT_LONGITUDE) lon,
  count(*) total, sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging,
  sum(COALESCE(NR_FATALITIES,0)) fatalities, min(INCIDENT_YEAR) firstYear, max(INCIDENT_YEAR) lastYear
  FROM strike_reports
  WHERE AIRPORT_ID IS NOT NULL AND AIRPORT IS NOT NULL AND AIRPORT_ID <> 'UNKN' AND AIRPORT <> 'UNKNOWN'
  GROUP BY AIRPORT_ID HAVING total >= ${AIRPORT_MIN} ORDER BY total DESC`);
const airportTopSpecies = db.prepare(`SELECT SPECIES name, count(*) c FROM strike_reports
  WHERE AIRPORT_ID = ? AND SPECIES IS NOT NULL AND SPECIES NOT LIKE 'Unknown%'
  GROUP BY SPECIES_ID ORDER BY c DESC LIMIT 8`);
const airports = dedupeBySlug(airportRows.map((a) => ({ ...a, slug: slugify(a.id), topSpecies: airportTopSpecies.all(a.id) })));

// ---- per-SPECIES ----
const speciesRows = all(`SELECT SPECIES_ID id, SPECIES name, count(*) total,
  sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging, max(SIZE) size
  FROM strike_reports WHERE SPECIES IS NOT NULL AND SPECIES NOT LIKE 'Unknown%'
  GROUP BY SPECIES_ID HAVING total >= ${SPECIES_MIN} ORDER BY total DESC`);
const speciesTopAirports = db.prepare(`SELECT AIRPORT name, count(*) c FROM strike_reports
  WHERE SPECIES_ID = ? AND AIRPORT <> 'UNKNOWN' GROUP BY AIRPORT_ID ORDER BY c DESC LIMIT 8`);
const species = dedupeBySlug(speciesRows.map((s) => ({ ...s, slug: slugify(s.name), topAirports: speciesTopAirports.all(s.id) })));

// ---- per-AIRCRAFT (group by AIRCRAFT text) ----
const aircraftRows = all(`SELECT AIRCRAFT name, count(*) total,
  sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging
  FROM strike_reports WHERE AIRCRAFT IS NOT NULL AND AIRCRAFT <> '' AND AIRCRAFT <> 'UNKNOWN'
  GROUP BY AIRCRAFT HAVING total >= ${AIRCRAFT_MIN} ORDER BY total DESC`);
const aircraftTopSpecies = db.prepare(`SELECT SPECIES name, count(*) c FROM strike_reports
  WHERE AIRCRAFT = ? AND SPECIES NOT LIKE 'Unknown%' GROUP BY SPECIES_ID ORDER BY c DESC LIMIT 8`);
const aircraft = dedupeBySlug(aircraftRows.map((a) => ({ ...a, slug: slugify(a.name), topSpecies: aircraftTopSpecies.all(a.name) })));

// ---- per-YEAR ----
const yearRows = all(`SELECT INCIDENT_YEAR year, count(*) total,
  sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging, sum(COALESCE(NR_FATALITIES,0)) fatalities
  FROM strike_reports WHERE INCIDENT_YEAR IS NOT NULL GROUP BY INCIDENT_YEAR ORDER BY INCIDENT_YEAR DESC`);
const yearTopSpecies = db.prepare(`SELECT SPECIES name, count(*) c FROM strike_reports
  WHERE INCIDENT_YEAR = ? AND SPECIES NOT LIKE 'Unknown%' GROUP BY SPECIES_ID ORDER BY c DESC LIMIT 8`);
const years = yearRows.map((y) => ({ ...y, topSpecies: yearTopSpecies.all(y.year) }));

// ---- NOTABLE strikes (severe + has narrative) ----
const notable = all(`SELECT INDEX_NR id, normalized_date date, AIRPORT airport, STATE state,
  OPERATOR operator, AIRCRAFT aircraft, SPECIES species, DAMAGE_LEVEL damage,
  EFFECT effect, COALESCE(NR_FATALITIES,0) fatalities, COALESCE(NR_INJURIES,0) injuries,
  substr(REMARKS,1,1200) remarks
  FROM strike_reports
  WHERE REMARKS IS NOT NULL AND length(REMARKS) > 80
    AND (DAMAGE_LEVEL IN ('S','D') OR COALESCE(NR_FATALITIES,0) > 0 OR COALESCE(NR_INJURIES,0) > 0)
  ORDER BY (CASE DAMAGE_LEVEL WHEN 'D' THEN 3 WHEN 'S' THEN 2 ELSE 1 END) DESC,
    COALESCE(NR_FATALITIES,0) DESC, COALESCE(NR_INJURIES,0) DESC, INCIDENT_YEAR DESC
  LIMIT ${NOTABLE_CAP}`).map((r) => ({ ...r, damageLabel: DAMAGE_LABELS[r.damage] || r.damage }));

// ---- per-FAMILY x segment (for the data-stories composition + danger charts) ----
// Family = FAA SPECIES_ID prefix (hierarchical): 1G=deer, 1C=bats, 1*=other
// mammals, J=waterfowl, K=raptors, R=owls, NE*=gulls/terns, N*=shorebirds,
// O=pigeons/doves, Y|Z=perching songbirds, I=herons, else Other. Identified
// strikes only (SPECIES NOT LIKE 'Unknown%'), matching the species table.
// segment 'commercial' = larger aircraft (AC_MASS 3-5: regional jets, airliners,
// heavy) vs 'all' = every civil strike. The prefix→label mapping lives in
// wildlifeFamilies.js — the SAME module storyQueries.js resolves its metric
// labels from, so the aggregate and the metric definitions cannot desync.
const { buildFamilyCase } = require('../src/services/stories/wildlifeFamilies');
const FAMILY_CASE = buildFamilyCase('SPECIES_ID');
const IDENTIFIED = `SPECIES IS NOT NULL AND SPECIES NOT LIKE 'Unknown%' AND SPECIES_ID IS NOT NULL AND SPECIES_ID <> ''`;
const familyFor = (segCond) => all(`SELECT ${FAMILY_CASE} family, count(*) strikes,
  sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging
  FROM strike_reports WHERE ${IDENTIFIED} AND ${segCond}
  GROUP BY family ORDER BY strikes DESC`);
const families = [
  ...familyFor('1=1').map((r) => ({ segment: 'all', ...r })),
  ...familyFor(`AC_MASS IN ('3','4','5')`).map((r) => ({ segment: 'commercial', ...r })),
];

// ---- GA cuts (bird-strike stories) ----
// Segment split: light aircraft (AC_MASS 1-2, up to 5,700 kg) against the
// commercial fleet (3-5). The stories contrast the two, so every dimension is
// emitted for both segments; storyQueries picks what each block needs.
const { buildHeightCase, buildSpeedCase, PART_COLUMNS } =
  require('../src/services/stories/wildlifeGaCuts');
const SEGMENTS = { ga: `AC_MASS IN ('1','2')`, commercial: `AC_MASS IN ('3','4','5')` };
const DMG_SUM = `sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END)`;

const cutRows = (dimension, expr, segCond, segment) => all(
  `SELECT ${expr} bucket, count(*) strikes, ${DMG_SUM} damaging
     FROM strike_reports WHERE ${segCond} GROUP BY bucket`
).filter((r) => r.bucket !== null && r.bucket !== '')
  .map((r) => ({
    dimension, segment, bucket: String(r.bucket),
    strikes: r.strikes | 0, damaging: r.damaging | 0,
  }));

const gaCuts = [];
for (const [segment, segCond] of Object.entries(SEGMENTS)) {
  gaCuts.push(...cutRows('month', `printf('%02d', INCIDENT_MONTH)`, `${segCond} AND INCIDENT_MONTH BETWEEN 1 AND 12`, segment));
  gaCuts.push(...cutRows('height', buildHeightCase('HEIGHT'), segCond, segment));
  gaCuts.push(...cutRows('speed', buildSpeedCase('SPEED'), segCond, segment));
  gaCuts.push(...cutRows('time_of_day', `COALESCE(NULLIF(TIME_OF_DAY,''),'unknown')`, segCond, segment));
  gaCuts.push(...cutRows('phase', 'PHASE_OF_FLIGHT', `${segCond} AND PHASE_OF_FLIGHT IS NOT NULL AND PHASE_OF_FLIGHT <> ''`, segment));
  // Birds only (SPECIES_ID prefix 1 = mammals) — the night-damage gap is
  // largely white-tailed deer, which are nocturnal and destructive, so the
  // bird-only series is what supports any claim about *birds* at night.
  gaCuts.push(...cutRows('time_of_day_birds', `COALESCE(NULLIF(TIME_OF_DAY,''),'unknown')`,
    `${segCond} AND (SPECIES_ID IS NULL OR SPECIES_ID NOT LIKE '1%')`, segment));
  // part_struck: one row per part, counted independently (a strike can hit
  // several parts, so these overlap and never sum to the segment total).
  for (const p of PART_COLUMNS) {
    const r = one(`SELECT count(*) strikes, sum(CASE WHEN ${p.damCol}='1' THEN 1 ELSE 0 END) damaging
       FROM strike_reports WHERE ${segCond} AND ${p.strCol}='1'`);
    if (r && r.strikes > 0) {
      gaCuts.push({ dimension: 'part_struck', bucket: p.bucket, segment, strikes: r.strikes, damaging: r.damaging | 0 });
    }
  }
}

// Species for the GA segment only — the commercial mix is already covered by
// the family charts on the live bird story.
const gaSpecies = all(
  `SELECT SPECIES name, count(*) strikes, ${DMG_SUM} damaging
     FROM strike_reports
    WHERE ${SEGMENTS.ga} AND SPECIES IS NOT NULL AND SPECIES <> ''
    GROUP BY SPECIES ORDER BY strikes DESC LIMIT 40`
).map((r) => ({ slug: slugify(r.name), ...r }));

const gaTotalsFor = (segCond, segment) => {
  const t = one(`SELECT count(*) strikes, ${DMG_SUM} damaging,
      sum(COALESCE(NR_INJURIES,0)) injuries, sum(COALESCE(NR_FATALITIES,0)) fatalities,
      sum(CASE WHEN COALESCE(NR_FATALITIES,0) > 0 THEN 1 ELSE 0 END) fatal_events,
      sum(CASE WHEN COALESCE(NR_FATALITIES,0) > 0 AND PHASE_OF_FLIGHT = 'En Route' THEN 1 ELSE 0 END) fatal_en_route,
      sum(CASE WHEN COALESCE(NR_FATALITIES,0) > 0 AND STR_WINDSHLD = '1' THEN 1 ELSE 0 END) fatal_windshield,
      sum(COALESCE(AOS,0)) aos_hours, sum(COALESCE(COST_REPAIRS_INFL_ADJ,0)) cost_repairs,
      sum(CASE WHEN WARNED='Yes' THEN 1 ELSE 0 END) warned_yes,
      sum(CASE WHEN WARNED='No' THEN 1 ELSE 0 END) warned_no,
      sum(CASE WHEN WARNED IS NULL OR WARNED NOT IN ('Yes','No') THEN 1 ELSE 0 END) warned_unknown,
      sum(CASE WHEN EFFECT LIKE '%Precautionary Landing%' THEN 1 ELSE 0 END) precautionary,
      sum(CASE WHEN EFFECT LIKE '%Aborted Take-off%' THEN 1 ELSE 0 END) aborted_takeoff,
      sum(CASE WHEN EFFECT LIKE '%Engine Shutdown%' THEN 1 ELSE 0 END) engine_shutdown
    FROM strike_reports WHERE ${segCond}`);
  return Object.entries(t).map(([metric, value]) => ({ segment, metric, value: Number(value) || 0 }));
};
const gaTotals = [
  ...gaTotalsFor(SEGMENTS.ga, 'ga'),
  ...gaTotalsFor(SEGMENTS.commercial, 'commercial'),
];

const seed = {
  generatedAt: new Date().toISOString().slice(0, 10),
  source: 'FAA National Wildlife Strike Database',
  sourceUrl: 'https://wildlife.faa.gov/',
  license: 'US Government Public Domain',
  gates: { AIRPORT_MIN, SPECIES_MIN, AIRCRAFT_MIN },
  hub: { ...totals, yearly, topAirports, topSpecies, topPhases },
  airports, species, aircraft, years, notable, families,
  gaCuts, gaSpecies, gaTotals,
};

fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, JSON.stringify(seed));
db.close();
console.error(JSON.stringify({
  out: OUT, bytes: fs.statSync(OUT).size,
  counts: { airports: airports.length, species: species.length, aircraft: aircraft.length, years: years.length, notable: notable.length },
  hub: { total: totals.total, damaging: totals.damaging, fatalities: totals.fatalities },
  ga: { cuts: gaCuts.length, species: gaSpecies.length, totals: gaTotals.length },
}, null, 2));
