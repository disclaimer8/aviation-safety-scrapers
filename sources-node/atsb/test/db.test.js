'use strict';
//
// atsb db.upsert() — guards against a parse-empty detail page clobbering
// previously-good data:
//   1. refuses the upsert when the parsed row has neither occurrence_date
//      nor title (almost certainly a challenge page / broken parse, not a
//      real investigation record).
//   2. COALESCE(excluded.col, col) semantics for fact/narrative columns — a
//      null field in an otherwise-valid fresh parse leaves the stored value
//      alone instead of nulling it out.

const { openDb, upsert, getById } = require('../src/db');

function freshDb() {
  return openDb(':memory:');
}

const FULL_ROW = {
  investigation_id: 'AO-2024-001',
  source_url: 'https://www.atsb.gov.au/investigations/ao-2024-001',
  title: 'Engine failure involving Cessna 172, VH-ABC',
  occurrence_date: '01/02/2024',
  normalized_date: '2024-02-01',
  release_date: '10/03/2024',
  normalized_release_date: '2024-03-10',
  location_text: 'Bankstown',
  state: 'New South Wales',
  operator: 'Test Aviation Pty Ltd',
  aircraft_manufacturer: 'Cessna',
  aircraft_model: '172',
  aircraft_registration: 'VH-ABC',
  aircraft_serial: '12345',
  occurrence_category: 'Engine failure or malfunction',
  occurrence_class: 'Accident',
  highest_injury_level: 'None',
  fatalities_parsed: 0,
  fatalities_estimated: 0,
  sector: 'General aviation',
  damage: 'Minor',
  operation_type: 'Private',
  departure_point: 'Bankstown',
  destination: 'Camden',
  investigation_status: 'Completed',
  investigation_level: 'Short',
  investigation_type: 'Investigation',
  report_status: 'Final',
  summary_text: 'What happened: the engine failed shortly after takeoff.',
  contributing_factors: 'The engine failed for undetermined reasons.',
  other_factors: null,
  other_findings: null,
  findings_text: 'Contributing factors\nThe engine failed for undetermined reasons.',
  safety_analysis_text: null,
  report_pdf_url: 'https://www.atsb.gov.au/media/report.pdf',
};

describe('atsb db.upsert — refusal guard', () => {
  it('throws when the parsed row has neither occurrence_date nor title', () => {
    const db = freshDb();
    const emptyRow = { investigation_id: 'AO-2024-002' };
    expect(() => upsert(db, emptyRow, {}, 1000)).toThrow(/refused/);
    expect(getById(db, 'AO-2024-002')).toBeUndefined();
    db.close();
  });

  it('does not throw when only title is present (occurrence_date missing)', () => {
    const db = freshDb();
    const row = { investigation_id: 'AO-2024-003', title: 'Some title' };
    expect(() => upsert(db, row, {}, 1000)).not.toThrow();
    db.close();
  });

  it('does not throw when only occurrence_date is present (title missing)', () => {
    const db = freshDb();
    const row = { investigation_id: 'AO-2024-004', occurrence_date: '01/01/2024' };
    expect(() => upsert(db, row, {}, 1000)).not.toThrow();
    db.close();
  });

  it('accepts a fully-populated row', () => {
    const db = freshDb();
    expect(() => upsert(db, FULL_ROW, {}, 1000)).not.toThrow();
    const got = getById(db, 'AO-2024-001');
    expect(got.title).toBe(FULL_ROW.title);
    expect(got.aircraft_model).toBe('172');
    db.close();
  });
});

describe('atsb db.upsert — COALESCE preserves prior good data', () => {
  it('a parse-empty-ish re-run (but passing the refusal guard) does not null out previously stored facts', () => {
    const db = freshDb();
    upsert(db, FULL_ROW, {}, 1000);

    // Simulate a later re-fetch that only recovered occurrence_date/title
    // (e.g. a partially-broken parse after a layout tweak) — everything
    // else comes back undefined/null.
    const thinRow = {
      investigation_id: 'AO-2024-001',
      title: FULL_ROW.title,
      occurrence_date: FULL_ROW.occurrence_date,
    };
    upsert(db, thinRow, {}, 2000);

    const got = getById(db, 'AO-2024-001');
    // Fields absent from the thin re-parse keep their previously-stored value.
    expect(got.aircraft_model).toBe('172');
    expect(got.aircraft_registration).toBe('VH-ABC');
    expect(got.summary_text).toBe(FULL_ROW.summary_text);
    expect(got.contributing_factors).toBe(FULL_ROW.contributing_factors);
    expect(got.report_pdf_url).toBe(FULL_ROW.report_pdf_url);
  });

  it('a real new value DOES overwrite the stored one', () => {
    const db = freshDb();
    upsert(db, FULL_ROW, {}, 1000);

    const updated = { ...FULL_ROW, damage: 'Destroyed', investigation_status: 'Active' };
    upsert(db, updated, {}, 2000);

    const got = getById(db, 'AO-2024-001');
    expect(got.damage).toBe('Destroyed');
    expect(got.investigation_status).toBe('Active');
  });

  it('fetched_at/updated_at always advance, even on a thin re-parse', () => {
    const db = freshDb();
    upsert(db, FULL_ROW, {}, 1000);
    const thinRow = {
      investigation_id: 'AO-2024-001',
      title: FULL_ROW.title,
      occurrence_date: FULL_ROW.occurrence_date,
    };
    upsert(db, thinRow, {}, 2000);
    const got = getById(db, 'AO-2024-001');
    expect(got.fetched_at).toBe(2000);
    expect(got.updated_at).toBe(2000);
  });

  it('narrative_text/probable_cause also use COALESCE (a re-run with no narrative keeps the old one)', () => {
    const db = freshDb();
    upsert(db, FULL_ROW, { narrative_text: 'A real narrative.', probable_cause: 'Engine failure.' }, 1000);
    upsert(db, FULL_ROW, { narrative_text: null, probable_cause: null }, 2000);
    const got = getById(db, 'AO-2024-001');
    expect(got.narrative_text).toBe('A real narrative.');
    expect(got.probable_cause).toBe('Engine failure.');
  });
});
