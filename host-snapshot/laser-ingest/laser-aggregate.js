#!/usr/bin/env node
/*
 * laser-aggregate.js — fold the FAA laser-incident dataset (laser.db, built from
 * the per-year XLSX at faa.gov/about/initiatives/reported-laser-incidents-*,
 * US-gov public domain) into an aggregate seed JSON for the laser-strikes
 * vertical. Mirrors the wildlife/SDR aggregate pattern.
 *
 * Usage: node laser-aggregate.js [laser.db] [out.json]
 */
const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');

const DB = process.argv[2] || path.join(process.env.HOME, 'laser-ingest', 'laser.db');
const OUT = process.argv[3] || path.join(__dirname, '..', 'data', 'laser-strikes.json');

const AIRPORT_MIN = 50;
const CITY_MIN = 100;
const AIRCRAFT_MIN = 50;
const NOTABLE_CAP = 300;
const RECENT_PER_CITY = 10;
const TOPN = 15;
const YEARS = 'incident_year BETWEEN 2000 AND 2030'; // drop bad-date outliers

function slugify(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80);
}
// slugify strips non-alphanumerics → distinct ids can collide; rows arrive
// ordered by count DESC, so keep the FIRST (largest) per slug. (wildlife lesson)
function dedupeBySlug(rows) {
  const seen = new Set();
  return rows.filter((r) => (seen.has(r.slug) ? false : (seen.add(r.slug), true)));
}

const db = new Database(DB, { readonly: true });
const all = (sql, ...a) => db.prepare(sql).all(...a);
const one = (sql, ...a) => db.prepare(sql).get(...a);
const INJ = "injury IS NOT NULL AND injury NOT IN ('No','no','NO','','Unk','Unknown')";

const totals = one(`SELECT count(*) total,
  sum(CASE WHEN ${INJ} THEN 1 ELSE 0 END) injuries,
  min(incident_year) firstYear, max(incident_year) lastYear
  FROM laser_reports WHERE ${YEARS}`);

const yearly = all(`SELECT incident_year year, count(*) total,
  sum(CASE WHEN ${INJ} THEN 1 ELSE 0 END) injuries
  FROM laser_reports WHERE ${YEARS} GROUP BY incident_year ORDER BY incident_year`);

const topStates = all(`SELECT state name, count(*) c FROM laser_reports
  WHERE ${YEARS} AND state IS NOT NULL AND state <> '' AND state NOT LIKE 'Unk%'
  GROUP BY state ORDER BY c DESC LIMIT ${TOPN}`);
const topColors = all(`SELECT laser_color name, count(*) c FROM laser_reports
  WHERE ${YEARS} AND laser_color IS NOT NULL AND laser_color <> '' AND laser_color NOT LIKE 'Unk%'
    AND laser_color NOT LIKE 'Check Remarks%' -- FAA placeholder, not a color (audit 2026-07-02)
  GROUP BY laser_color ORDER BY c DESC LIMIT 10`);
const topAirportsHub = all(`SELECT airport name, count(*) c FROM laser_reports
  WHERE ${YEARS} AND airport IS NOT NULL AND airport <> '' AND airport NOT LIKE 'Unk%'
  GROUP BY airport ORDER BY c DESC LIMIT ${TOPN}`);

// per-STATE
const stateRows = all(`SELECT state name, count(*) total,
  sum(CASE WHEN ${INJ} THEN 1 ELSE 0 END) injuries, min(incident_year) firstYear, max(incident_year) lastYear
  FROM laser_reports WHERE ${YEARS} AND state IS NOT NULL AND state <> '' AND state NOT LIKE 'Unk%'
  GROUP BY state ORDER BY total DESC`);
const stateTopCities = db.prepare(`SELECT city name, count(*) c FROM laser_reports
  WHERE state = ? AND city IS NOT NULL AND city <> '' GROUP BY city ORDER BY c DESC LIMIT 8`);
const states = dedupeBySlug(stateRows.map((s) => ({ ...s, slug: slugify(s.name), topCities: stateTopCities.all(s.name) })));

// per-AIRPORT
const airportRows = all(`SELECT airport name, count(*) total,
  sum(CASE WHEN ${INJ} THEN 1 ELSE 0 END) injuries, max(state) state
  FROM laser_reports WHERE ${YEARS} AND airport IS NOT NULL AND airport <> '' AND airport NOT LIKE 'Unk%'
  GROUP BY airport HAVING total >= ${AIRPORT_MIN} ORDER BY total DESC`);
const airportTopCity = db.prepare(`SELECT city name, count(*) c FROM laser_reports WHERE airport = ? AND city <> '' GROUP BY city ORDER BY c DESC LIMIT 5`);
const airports = dedupeBySlug(airportRows.map((a) => ({ ...a, slug: slugify(a.name), topCities: airportTopCity.all(a.name) })));

// per-CITY. Each city carries its own yearly trend + newest reports so the
// city page never depends on the injury-only global `notable` slice (24 of 96
// cities had zero injury rows there → thin single-sentence pages).
const cityRows = all(`SELECT city name, max(state) state, count(*) total,
  sum(CASE WHEN ${INJ} THEN 1 ELSE 0 END) injuries
  FROM laser_reports WHERE ${YEARS} AND city IS NOT NULL AND city <> '' AND city NOT LIKE 'Unk%'
  GROUP BY city, state HAVING total >= ${CITY_MIN} ORDER BY total DESC`);
const cityYearly = db.prepare(`SELECT incident_year year, count(*) total,
  sum(CASE WHEN ${INJ} THEN 1 ELSE 0 END) injuries
  FROM laser_reports WHERE ${YEARS} AND city = ? AND state = ?
  GROUP BY incident_year ORDER BY incident_year`);
// Null out FAA free-text junk ("Unk", "Unknown", "Check Remarks", blanks) so
// renderers — which drop falsy fields — never emit "Unk ft" / "Unk laser".
const cityRecent = db.prepare(`SELECT id, incident_date date,
  CASE WHEN aircraft = '' OR aircraft LIKE 'Unk%' THEN NULL ELSE aircraft END aircraft,
  CASE WHEN altitude = '' OR altitude LIKE 'Unk%' THEN NULL ELSE altitude END altitude,
  CASE WHEN airport = '' OR airport LIKE 'Unk%' THEN NULL ELSE airport END airport,
  CASE WHEN laser_color = '' OR laser_color LIKE 'Unk%' OR laser_color LIKE 'Check%' THEN NULL ELSE laser_color END color
  FROM laser_reports WHERE ${YEARS} AND city = ? AND state = ?
  ORDER BY incident_date DESC, id DESC LIMIT ${RECENT_PER_CITY}`);
const cities = dedupeBySlug(cityRows.map((c) => ({
  ...c, slug: slugify(`${c.name}-${c.state}`),
  yearly: cityYearly.all(c.name, c.state), recent: cityRecent.all(c.name, c.state),
})));

// per-AIRCRAFT
const aircraftRows = all(`SELECT aircraft name, count(*) total,
  sum(CASE WHEN ${INJ} THEN 1 ELSE 0 END) injuries
  FROM laser_reports WHERE ${YEARS} AND aircraft IS NOT NULL AND aircraft <> '' AND aircraft NOT LIKE 'Unk%'
  GROUP BY aircraft HAVING total >= ${AIRCRAFT_MIN} ORDER BY total DESC`);
const aircraft = dedupeBySlug(aircraftRows.map((a) => ({ ...a, slug: slugify(a.name) })));

// per-YEAR
const yearRows = all(`SELECT incident_year year, count(*) total,
  sum(CASE WHEN ${INJ} THEN 1 ELSE 0 END) injuries
  FROM laser_reports WHERE ${YEARS} GROUP BY incident_year ORDER BY incident_year DESC`);
const yearTopStates = db.prepare(`SELECT state name, count(*) c FROM laser_reports WHERE incident_year = ? AND state <> '' GROUP BY state ORDER BY c DESC LIMIT 8`);
const years = yearRows.map((y) => ({ ...y, topStates: yearTopStates.all(y.year) }));

// NOTABLE: reported injuries. Same junk-nulling as cityRecent — 116 of 243
// injury rows carry "Unk"/"Check" placeholders that otherwise render.
const notable = all(`SELECT id, incident_date date, incident_time time,
  CASE WHEN aircraft = '' OR aircraft LIKE 'Unk%' THEN NULL ELSE aircraft END aircraft,
  CASE WHEN altitude = '' OR altitude LIKE 'Unk%' THEN NULL ELSE altitude END altitude,
  CASE WHEN airport = '' OR airport LIKE 'Unk%' THEN NULL ELSE airport END airport,
  CASE WHEN laser_color = '' OR laser_color LIKE 'Unk%' OR laser_color LIKE 'Check%' THEN NULL ELSE laser_color END color,
  city, state
  FROM laser_reports WHERE ${YEARS} AND ${INJ}
  ORDER BY incident_date DESC LIMIT ${NOTABLE_CAP}`);

const seed = {
  generatedAt: new Date().toISOString().slice(0, 10),
  source: 'FAA Reported Laser Incidents',
  sourceUrl: 'https://www.faa.gov/aircraft/safety/report/laserinfo',
  license: 'US Government Public Domain',
  gates: { AIRPORT_MIN, CITY_MIN, AIRCRAFT_MIN },
  hub: { ...totals, yearly, topStates, topColors, topAirports: topAirportsHub },
  states, airports, cities, aircraft, years, notable,
};
fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, JSON.stringify(seed));
db.close();
console.error(JSON.stringify({
  out: OUT, bytes: fs.statSync(OUT).size,
  counts: { states: states.length, airports: airports.length, cities: cities.length, aircraft: aircraft.length, years: years.length, notable: notable.length },
  hub: { total: totals.total, injuries: totals.injuries, years: `${totals.firstYear}-${totals.lastYear}` },
}, null, 2));
