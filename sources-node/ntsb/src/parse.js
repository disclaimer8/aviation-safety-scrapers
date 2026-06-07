'use strict';

function buildWeatherSummary(w) {
  if (!w) return null;
  const parts = [];
  if (w.wx_cond_basic) parts.push(w.wx_cond_basic);
  if (w.wind_dir_deg && w.wind_vel_kts) {
    const dir = String(w.wind_dir_deg).padStart(3, '0');
    const kt  = String(w.wind_vel_kts).padStart(2, '0');
    parts.push(`wind ${dir}/${kt}kt`);
  }
  if (w.vis_sm) parts.push(`vis ${w.vis_sm}sm`);
  if (parts.length === 0) return null;
  return parts.join(', ');
}

function buildFactorsJson(findings) {
  if (!findings || findings.length === 0) return null;
  const arr = findings
    .map(f => (f.finding_description || '').trim())
    .filter(Boolean);
  if (arr.length === 0) return null;
  return JSON.stringify(arr);
}

function joinNtsbTables(tables) {
  const narrativeByEv = new Map();
  for (const r of tables.narratives || []) narrativeByEv.set(r.ev_id, r);

  const findingsByEv = new Map();
  for (const r of tables.findings || []) {
    if (!findingsByEv.has(r.ev_id)) findingsByEv.set(r.ev_id, []);
    findingsByEv.get(r.ev_id).push(r);
  }

  const occurrenceByEv = new Map();
  for (const r of tables.occurrences || []) {
    if (!occurrenceByEv.has(r.ev_id)) occurrenceByEv.set(r.ev_id, r);
  }

  // Weather fields live INSIDE the events table (no separate weather table in
  // the real NTSB MDB). We still accept a tables.weather Map for backwards
  // compatibility with the test fixtures from Task 5.
  const weatherByEv = new Map();
  for (const r of tables.weather || []) weatherByEv.set(r.ev_id, r);

  const aircraftByEv = new Map();
  for (const r of tables.aircraft || []) aircraftByEv.set(r.ev_id, r);

  const out = [];
  for (const ev of tables.events || []) {
    const narr = narrativeByEv.get(ev.ev_id);
    if (!narr) continue;

    const narrativeText = (narr.narr_accp || '').trim() || null;
    const probableCause = (narr.narr_cause || '').trim() || null;
    const findings = findingsByEv.get(ev.ev_id) || [];
    const occ      = occurrenceByEv.get(ev.ev_id);
    const wxRow    = weatherByEv.get(ev.ev_id) || ev;  // fall back to event row
    const acft     = aircraftByEv.get(ev.ev_id);

    out.push({
      ev_id: ev.ev_id,
      ev_date: ev.ev_date,
      narrative_text: narrativeText,
      probable_cause: probableCause,
      factors_json: buildFactorsJson(findings),
      phase_of_flight: occ?.phase_of_flight || occ?.occurrence_code || null,
      weather_summary: buildWeatherSummary(wxRow),
      ev_meta: {
        city: ev.ev_city, state: ev.ev_state, country: ev.ev_country,
        lat: ev.latitude, lon: ev.longitude,
        aircraft_make: acft?.acft_make, aircraft_model: acft?.acft_model,
        registration: acft?.regis_no,
      },
    });
  }
  return out;
}

module.exports = { joinNtsbTables, buildWeatherSummary, buildFactorsJson };
