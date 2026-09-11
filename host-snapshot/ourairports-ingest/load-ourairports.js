#!/usr/bin/env node
// Load OurAirports airports.csv (CC0 public domain) into ourairports.db
// (table ref_airports). The prod sync ATTACHes this and copies into the prod
// ref_airports table. Node + better-sqlite3 + csv-parse.
const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');
const { parse } = require('csv-parse');

const DIR = process.argv[2] || __dirname;
const OUT = process.argv[3] || path.join(DIR, 'ourairports.db');
const CSV = path.join(DIR, 'airports.csv');

if (fs.existsSync(OUT)) fs.unlinkSync(OUT);
const db = new Database(OUT);
db.pragma('journal_mode = WAL');
db.pragma('synchronous = OFF');
db.exec(`CREATE TABLE ref_airports (
  ident TEXT PRIMARY KEY, type TEXT, name TEXT, latitude REAL, longitude REAL,
  elevation_ft INTEGER, iso_country TEXT, iso_region TEXT, municipality TEXT,
  scheduled_service TEXT, icao_code TEXT, iata_code TEXT, gps_code TEXT, local_code TEXT,
  wikipedia_link TEXT )`);
const ins = db.prepare(`INSERT OR REPLACE INTO ref_airports
  (ident, type, name, latitude, longitude, elevation_ft, iso_country, iso_region, municipality,
   scheduled_service, icao_code, iata_code, gps_code, local_code, wikipedia_link)
  VALUES (@ident,@type,@name,@latitude,@longitude,@elevation_ft,@iso_country,@iso_region,@municipality,
   @scheduled_service,@icao_code,@iata_code,@gps_code,@local_code,@wikipedia_link)`);

(async () => {
  let n = 0, batch = [];
  const tx = db.transaction((rows) => { for (const r of rows) ins.run(r); });
  const parser = fs.createReadStream(CSV).pipe(parse({ columns: true, relax_quotes: true, skip_records_with_error: true }));
  for await (const r of parser) {
    if (!r.ident) continue;
    batch.push({
      ident: r.ident, type: r.type || null, name: r.name || null,
      latitude: r.latitude_deg ? parseFloat(r.latitude_deg) : null,
      longitude: r.longitude_deg ? parseFloat(r.longitude_deg) : null,
      elevation_ft: r.elevation_ft ? parseInt(r.elevation_ft, 10) : null,
      iso_country: r.iso_country || null, iso_region: r.iso_region || null, municipality: r.municipality || null,
      scheduled_service: r.scheduled_service || null, icao_code: r.icao_code || null, iata_code: r.iata_code || null,
      gps_code: r.gps_code || null, local_code: r.local_code || null, wikipedia_link: r.wikipedia_link || null,
    });
    if (batch.length >= 5000) { tx(batch); n += batch.length; batch = []; }
  }
  if (batch.length) { tx(batch); n += batch.length; }
  db.exec('CREATE INDEX idx_refap_country ON ref_airports(iso_country)');
  db.exec('CREATE INDEX idx_refap_iata ON ref_airports(iata_code)');
  db.exec('CREATE INDEX idx_refap_gps ON ref_airports(gps_code)');
  const total = db.prepare('SELECT count(*) c FROM ref_airports').get().c;
  const byType = db.prepare("SELECT type, count(*) c FROM ref_airports GROUP BY type ORDER BY c DESC LIMIT 8").all();
  const withCoords = db.prepare('SELECT count(*) c FROM ref_airports WHERE latitude IS NOT NULL').get().c;
  console.error(JSON.stringify({ inserted: n, total, withCoords, byType }, null, 2));
  db.close();
})();
