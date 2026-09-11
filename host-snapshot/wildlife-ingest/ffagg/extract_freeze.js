#!/usr/bin/env node
/*
 * extract_freeze.js — freeze the aggregates behind the light-aircraft
 * certification-asymmetry paper out of the raw FAA National Wildlife Strike
 * Database (wildlife.db, built from wildlife.faa.gov/assets/database.zip).
 *
 * Runs where the raw database lives (mini-PC), writes CSVs that are committed
 * alongside the manuscript so every number in the paper is reproducible from
 * the repository without access to the raw dump.
 *
 * Usage: node extract_freeze.js [wildlife.db] [outdir]
 *
 * Definitions used throughout, stated once here and inherited by every table:
 *   damaging      DAMAGE_LEVEL IN ('M','M?','S','D')   -- excludes None/null
 *   mass class    FAA AC_MASS: 1 <=2,250 kg; 2 2,251-5,700; 3 5,701-27,000;
 *                 4 27,001-272,000; 5 >272,000 kg
 *   mammal        SPECIES_ID prefix '1' (FAA taxonomy: 1G deer, 1C bats, ...)
 *   component     STR_<part> = struck, DAM_<part> = damaged; a single strike
 *                 can set several, so components do not partition strikes.
 */
const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');

const DB = process.argv[2] || path.join(process.env.HOME, 'wildlife-ingest', 'wildlife.db');
const OUT = process.argv[3] || path.join(__dirname, 'freeze');

const DAMAGING = "DAMAGE_LEVEL IN ('M','M?','S','D')";
const MASS = "AC_MASS IN ('1','2','3','4','5')";
const MAMMAL = "SPECIES_ID LIKE '1%'";

const COMPONENTS = [
  ['Windshield', 'STR_WINDSHLD', 'DAM_WINDSHLD'],
  ['Nose', 'STR_NOSE', 'DAM_NOSE'],
  ['Radome', 'STR_RAD', 'DAM_RAD'],
  ['Engine 1', 'STR_ENG1', 'DAM_ENG1'],
  ['Engine 2', 'STR_ENG2', 'DAM_ENG2'],
  ['Propeller', 'STR_PROP', 'DAM_PROP'],
  ['Wing or rotor', 'STR_WING_ROT', 'DAM_WING_ROT'],
  ['Fuselage', 'STR_FUSE', 'DAM_FUSE'],
  ['Landing gear', 'STR_LG', 'DAM_LG'],
  ['Tail', 'STR_TAIL', 'DAM_TAIL'],
  ['Lights', 'STR_LGHTS', 'DAM_LGHTS'],
];

const SPEED_BANDS = [
  ['<80', 'SPEED < 80'],
  ['80-99', 'SPEED BETWEEN 80 AND 99'],
  ['100-129', 'SPEED BETWEEN 100 AND 129'],
  ['130-159', 'SPEED BETWEEN 130 AND 159'],
  ['160+', 'SPEED >= 160'],
];

const db = new Database(DB, { readonly: true });
const all = (sql) => db.prepare(sql).all();
const one = (sql) => db.prepare(sql).get();

fs.mkdirSync(OUT, { recursive: true });
const csv = (name, header, rows) => {
  const esc = (v) => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const body = [header.join(','), ...rows.map((r) => r.map(esc).join(','))].join('\n');
  fs.writeFileSync(path.join(OUT, name), `${body}\n`);
  return rows.length;
};

// ---- 1. Strikes, damage and human harm by aircraft mass class ----
const massRows = all(`SELECT AC_MASS mass_class, count(*) strikes,
    sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging,
    sum(COALESCE(NR_FATALITIES,0)) fatalities,
    sum(CASE WHEN COALESCE(NR_FATALITIES,0) > 0 THEN 1 ELSE 0 END) fatal_events,
    sum(COALESCE(NR_INJURIES,0)) injuries,
    sum(CASE WHEN DAMAGE_LEVEL = 'D' THEN 1 ELSE 0 END) destroyed
  FROM strike_reports WHERE ${MASS} GROUP BY AC_MASS ORDER BY AC_MASS`);
csv('mass_class.csv',
  ['mass_class', 'strikes', 'damaging', 'fatalities', 'fatal_events', 'injuries', 'destroyed'],
  massRows.map((r) => [r.mass_class, r.strikes, r.damaging, r.fatalities, r.fatal_events, r.injuries, r.destroyed]));

// Reports with no mass class recorded — the excluded remainder, reported so the
// paper can state what fraction of the corpus its denominators omit.
const unclassified = one(`SELECT count(*) strikes,
    sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging,
    sum(COALESCE(NR_FATALITIES,0)) fatalities
  FROM strike_reports WHERE AC_MASS IS NULL OR AC_MASS NOT IN ('1','2','3','4','5')`);
const corpus = one(`SELECT count(*) strikes, min(INCIDENT_YEAR) first_year, max(INCIDENT_YEAR) last_year,
    sum(COALESCE(NR_FATALITIES,0)) fatalities FROM strike_reports`);
csv('corpus.csv',
  ['metric', 'value'],
  [['total_reports', corpus.strikes], ['first_year', corpus.first_year], ['last_year', corpus.last_year],
    ['total_fatalities', corpus.fatalities], ['unclassified_reports', unclassified.strikes],
    ['unclassified_damaging', unclassified.damaging], ['unclassified_fatalities', unclassified.fatalities]]);

// ---- 2. Component-level conditional damage by mass class ----
// P(damaged | struck) per component. This is the paper's central table: the
// certification requirement that differs between transport and light aircraft
// is a component-level one, so a component-level rate is what tests it.
const compRows = [];
for (const [label, strCol, damCol] of COMPONENTS) {
  for (const m of ['1', '2', '3', '4', '5']) {
    const r = one(`SELECT count(*) struck, sum(CASE WHEN ${damCol}='1' THEN 1 ELSE 0 END) damaged
      FROM strike_reports WHERE AC_MASS='${m}' AND ${strCol}='1'`);
    if (r.struck > 0) compRows.push([label, m, r.struck, r.damaged || 0]);
  }
}
csv('component_by_mass.csv', ['component', 'mass_class', 'struck', 'damaged'], compRows);

// ---- 3. Speed-damage dose response, stratified by mass class ----
// Stratification is the point: pooled, the gradient could be a fleet-mix
// artefact (faster strikes involve heavier aircraft). Within a mass class it
// cannot be.
const speedRows = [];
for (const [band, cond] of SPEED_BANDS) {
  for (const m of ['1', '2', '3', '4']) {
    const r = one(`SELECT count(*) strikes, sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging
      FROM strike_reports WHERE AC_MASS='${m}' AND SPEED IS NOT NULL AND ${cond}`);
    if (r.strikes > 0) speedRows.push([band, m, r.strikes, r.damaging || 0]);
  }
}
// Reports with no recorded speed, by mass class — an exclusion the paper must
// quantify because completeness rises with severity.
const speedMissing = all(`SELECT AC_MASS mass_class, count(*) strikes,
    sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging
  FROM strike_reports WHERE ${MASS} AND SPEED IS NULL GROUP BY AC_MASS ORDER BY AC_MASS`);
csv('speed_by_mass.csv', ['speed_band', 'mass_class', 'strikes', 'damaging'], speedRows);
csv('speed_missing.csv', ['mass_class', 'strikes', 'damaging'],
  speedMissing.map((r) => [r.mass_class, r.strikes, r.damaging]));

// ---- 4. Time of day by taxon and mass class ----
// The nocturnal-mammal confound: terrestrial mammals are struck mostly at night
// and damage the aircraft far more often, so an unstratified night-vs-day
// damage rate attributes their effect to birds.
const todRows = [];
for (const tod of ['Dawn', 'Day', 'Dusk', 'Night']) {
  for (const m of ['1', '2', '3', '4']) {
    for (const [taxon, cond] of [['mammal', MAMMAL], ['bird', `(SPECIES_ID IS NULL OR NOT ${MAMMAL})`]]) {
      const r = one(`SELECT count(*) strikes, sum(CASE WHEN ${DAMAGING} THEN 1 ELSE 0 END) damaging
        FROM strike_reports WHERE AC_MASS='${m}' AND TIME_OF_DAY='${tod}' AND ${cond}`);
      if (r.strikes > 0) todRows.push([tod, m, taxon, r.strikes, r.damaging || 0]);
    }
  }
}
csv('time_of_day_by_taxon.csv', ['time_of_day', 'mass_class', 'taxon', 'strikes', 'damaging'], todRows);

// ---- 5. Fatal events, one row each ----
const fatal = all(`SELECT INCIDENT_YEAR year, AC_MASS mass_class, AIRCRAFT aircraft,
    SPECIES species, PHASE_OF_FLIGHT phase, NR_FATALITIES fatalities,
    CASE WHEN STR_WINDSHLD='1' THEN 1 ELSE 0 END windshield_struck,
    CASE WHEN DAM_WINDSHLD='1' THEN 1 ELSE 0 END windshield_damaged
  FROM strike_reports WHERE COALESCE(NR_FATALITIES,0) > 0
  ORDER BY INCIDENT_YEAR`);
csv('fatal_events.csv',
  ['year', 'mass_class', 'aircraft', 'species', 'phase', 'fatalities', 'windshield_struck', 'windshield_damaged'],
  fatal.map((r) => [r.year, r.mass_class || '', r.aircraft || '', r.species || '', r.phase || '',
    r.fatalities, r.windshield_struck, r.windshield_damaged]));

db.close();
console.error(JSON.stringify({
  out: OUT,
  mass_classes: massRows.length,
  components: compRows.length,
  speed_cells: speedRows.length,
  tod_cells: todRows.length,
  fatal_events: fatal.length,
  corpus: corpus.strikes,
}, null, 2));
