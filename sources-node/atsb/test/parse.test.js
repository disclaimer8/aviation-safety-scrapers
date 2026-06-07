'use strict';
//
// atsbParse — pure HTML parser, no I/O. Validates against 3 captured
// fixtures of the POST-REDESIGN ATSB site (Drupal 11 / CivicTheme,
// rendered HTML as a headless browser returns it):
//   - AO-2023-014 — short fatal investigation, Final (R44 helicopter)
//   - AO-2026-073 — short fatal investigation, Pending (R22 helicopter,
//                   structured "Injuries" cell, no narrative yet)
//   - AO-2024-006 — long incident investigation, Final (A380 FOD, Qantas)
// These exercise the ct-table metadata map, the h3/h4 section walk for
// Findings / Safety analysis / Executive summary, and both fatalities
// strategies (structured Injuries cell vs narrative heuristic).

const fs = require('fs');
const path = require('path');
const parse = require('../src/parse');

const FIXTURES = path.join(__dirname, 'fixtures', 'atsb');
const load = name => fs.readFileSync(path.join(FIXTURES, name), 'utf8');

describe('atsbParse.parseDetail — AO-2023-014 (fatal R44, Final, Short)', () => {
  const rec = parse.parseDetail(load('ao_2023_014.html'), 'AO-2023-014');

  it('pulls metadata fields from the ct-table data tables', () => {
    expect(rec.investigation_id).toBe('AO-2023-014');
    expect(rec.title).toMatch(/Robinson R44 II.*VH-WLH.*Bingegang/);
    expect(rec.occurrence_date).toBe('04/04/2023');
    expect(rec.normalized_date).toBe('2023-04-04');
    expect(rec.release_date).toBe('10/11/2023');
    expect(rec.normalized_release_date).toBe('2023-11-10');
    expect(rec.location_text).toBe('Bingegang');
    expect(rec.state).toBe('Queensland');
    expect(rec.aircraft_manufacturer).toBe('Robinson Helicopter Co');
    expect(rec.aircraft_model).toBe('R44 II');
    expect(rec.aircraft_registration).toBe('VH-WLH');
    expect(rec.aircraft_serial).toBe('14253');
    expect(rec.occurrence_category).toBe('Collision with terrain');
    expect(rec.occurrence_class).toBe('Accident');
    expect(rec.highest_injury_level).toBe('Fatal');
    expect(rec.sector).toBe('Helicopter');
    expect(rec.damage).toBe('Destroyed');
    expect(rec.report_status).toBe('Final');
    expect(rec.investigation_status).toBe('Completed');
  });

  it('source_url is the canonical /investigations/<id> link', () => {
    expect(rec.source_url).toBe('https://www.atsb.gov.au/investigations/ao-2023-014');
  });

  it('infers fatalities=1 from the narrative (no structured Injuries cell)', () => {
    expect(rec.fatalities_parsed).toBe(1);
  });

  it('operator stays null for private flights (no Aircraft operator row)', () => {
    expect(rec.operator).toBeNull();
  });

  it('summary_text contains the executive-summary subsections', () => {
    expect(rec.summary_text).toMatch(/What happened/);
    expect(rec.summary_text).toMatch(/What the ATSB found/);
    expect(rec.summary_text).toMatch(/Safety message/);
  });

  it('contributing_factors extracted clean — no ATSB boilerplate prefix', () => {
    expect(rec.contributing_factors).toMatch(/pilot likely lost awareness/i);
    expect(rec.contributing_factors).not.toMatch(/ATSB investigation report findings focus/);
  });

  it('separates other_factors from contributing_factors (h4 segmentation)', () => {
    expect(rec.other_factors).toMatch(/partially obscured/);
    expect(rec.other_factors).not.toMatch(/pilot likely lost awareness/i);
  });

  it('safety_analysis_text is captured', () => {
    expect(rec.safety_analysis_text).toBeTruthy();
    expect(rec.safety_analysis_text.length).toBeGreaterThan(500);
  });

  it('extracts the final-report PDF URL', () => {
    expect(rec.report_pdf_url).toMatch(/^https:\/\/www\.atsb\.gov\.au\/sites\/default\/files\/.*\.pdf/);
  });
});

describe('atsbParse.parseDetail — AO-2026-073 (fatal R22, Pending)', () => {
  const rec = parse.parseDetail(load('ao_2026_073.html'), 'AO-2026-073');

  it('parses fatalities=1 from the structured "Crew - 1 (fatal)" Injuries cell', () => {
    expect(rec.fatalities_parsed).toBe(1);
    expect(rec.highest_injury_level).toBe('Fatal');
  });

  it('captures the commercial operator', () => {
    expect(rec.operator).toBe('B & T Philp Pty Ltd');
  });

  it('pending reports have no findings/summary yet (narrative will be skipped)', () => {
    expect(rec.report_status).toBe('Pending');
    expect(rec.summary_text).toBeNull();
    expect(rec.findings_text).toBeNull();
    expect(rec.release_date).toBeNull();
  });
});

describe('atsbParse.parseDetail — AO-2024-006 (non-fatal A380, Final, long)', () => {
  const rec = parse.parseDetail(load('ao_2024_006.html'), 'AO-2024-006');

  it('pulls commercial operator + departure/destination', () => {
    expect(rec.operator).toBe('Qantas Airways Limited');
    expect(rec.departure_point).toMatch(/Sydney Airport/);
    expect(rec.destination).toMatch(/Los Angeles International Airport/);
  });

  it('investigation_id + normalized date', () => {
    expect(rec.investigation_id).toBe('AO-2024-006');
    expect(rec.normalized_date).toBe('2024-01-01');
    expect(rec.occurrence_class).toBe('Incident');
  });

  it('infers fatalities=0 from highest_injury_level="None"', () => {
    expect(rec.fatalities_parsed).toBe(0);
    expect(rec.highest_injury_level).toBe('None');
  });

  it('contributing_factors captured from the Findings section', () => {
    expect(rec.contributing_factors).toMatch(/aircraft maintenance engineer/i);
    expect(rec.contributing_factors).toMatch(/turning tool/i);
    expect(rec.contributing_factors).not.toMatch(/ATSB investigation report findings focus/);
  });

  it('safety_analysis_text captured (long-form investigation)', () => {
    expect(rec.safety_analysis_text).toBeTruthy();
    expect(rec.safety_analysis_text.length).toBeGreaterThan(500);
  });
});

describe('atsbParse.parseListingPage — post-redesign /investigations/ao-* anchors', () => {
  const listingHtml = `
    <html><body>
      <main>
        <a href="/investigations/ao-2026-075">Collision with terrain ...</a>
        <a href="https://www.atsb.gov.au/investigations/ao-2026-073">Engine failure ...</a>
        <a href="/investigations/ao-2026-073">Duplicate</a>
        <a href="/investigations/ro-2024-001">Rail report</a>
        <a href="/investigations/mo-2024-002">Marine report</a>
        <a href="/about-atsb">Nav link</a>
      </main>
    </body></html>`;

  it('extracts AO investigation ids + absolute detail URLs', () => {
    const rows = parse.parseListingPage(listingHtml);
    expect(rows).toHaveLength(2);
    expect(rows[0].investigation_id).toBe('AO-2026-075');
    expect(rows[0].detail_url).toBe('https://www.atsb.gov.au/investigations/ao-2026-075');
    expect(rows[1].investigation_id).toBe('AO-2026-073');
    expect(rows[1].detail_url).toBe('https://www.atsb.gov.au/investigations/ao-2026-073');
  });

  it('drops non-aviation (RO-/MO-) ids and dedupes repeats', () => {
    const rows = parse.parseListingPage(listingHtml);
    expect(rows.find(r => /^RO-|^MO-/.test(r.investigation_id))).toBeUndefined();
    expect(rows.filter(r => r.investigation_id === 'AO-2026-073')).toHaveLength(1);
  });
});

describe('atsbParse._internal.parseFatalitiesFromInjuries', () => {
  const f = parse._internal.parseFatalitiesFromInjuries;
  it('sums fatal counts across crew + passengers', () => {
    expect(f('Crew - 1 (fatal), Passengers - 2 (fatal)')).toBe(3);
  });
  it('returns 1 for a single fatal crew member', () => {
    expect(f('Crew - 1 (fatal)')).toBe(1);
  });
  it('returns 0 for a cell mentioning only non-fatal categories', () => {
    expect(f('Crew - 2 (minor), Passengers - 3 (none)')).toBe(0);
  });
  it('returns null when no Injuries cell present', () => {
    expect(f(null)).toBeNull();
  });
});

// ── I6: fatalities_estimated provenance flag ─────────────────────────────────
describe('atsbParse.parseDetail — I6 fatalities_estimated provenance flag', () => {
  it('AO-2026-073 (structured Injuries cell) → fatalities_estimated === 0', () => {
    // AO-2026-073 has "Crew - 1 (fatal)" in the structured Injuries cell,
    // so the count comes from parseFatalitiesFromInjuries (authoritative path).
    // fatalities_estimated must be 0.
    const rec = parse.parseDetail(load('ao_2026_073.html'), 'AO-2026-073');
    expect(rec.fatalities_parsed).toBe(1);
    expect(rec.fatalities_estimated).toBe(0);
  });

  it('narrative-only fixture → fatalities_estimated === 1 when count comes from prose', () => {
    // Minimal HTML with NO structured Injuries cell — only the prose mentions
    // that the pilot was fatally injured. parseFatalitiesFromInjuries returns
    // null, so parseFatalitiesFromNarrative fires → estimated flag must be 1.
    const narrativeOnlyHtml = `<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<article>
  <h1>Robinson R44 – fatal crash</h1>
  <table>
    <tr><th scope="row">Investigation Number</th><td>AO-2099-001</td></tr>
    <tr><th scope="row">Occurrence Date</th><td>01/01/2099</td></tr>
    <tr><th scope="row">Highest Injury Level</th><td>Fatal</td></tr>
  </table>
  <h3>Executive Summary</h3>
  <p>The pilot was fatally injured when the aircraft collided with terrain.</p>
</article>
</body></html>`;
    const rec = parse.parseDetail(narrativeOnlyHtml, 'AO-2099-001');
    // parseFatalitiesFromNarrative matches "pilot was fatally injured" → 1
    expect(rec.fatalities_parsed).toBe(1);
    // Because the count came from narrative prose, estimated flag must be 1
    expect(rec.fatalities_estimated).toBe(1);
  });

  it('AO-2023-014 (narrative-only, no Injuries cell) → fatalities_estimated === 1', () => {
    // AO-2023-014 has no structured Injuries row — fatalities are inferred
    // from the narrative heuristic → must be flagged estimated.
    const rec = parse.parseDetail(load('ao_2023_014.html'), 'AO-2023-014');
    expect(rec.fatalities_parsed).toBe(1);
    expect(rec.fatalities_estimated).toBe(1);
  });
});
