'use strict';
//
// wikidata db.upsert() — narrative-preservation guard.
//
// composeNarrative() (src/compose.js) falls back to a short fact-sentence +
// CONTEXT_BOILERPLATE filler whenever there's no (or too-short) Wikipedia
// text — which is exactly what happens on a `build` run without --enrich.
// Before this fix, a plain re-run's upsert unconditionally overwrote
// narrative_text, so it silently replaced a previously enriched, real
// Wikipedia narrative with generic boilerplate. The fix: only overwrite
// narrative_text when the new value came from an actual Wikipedia fetch
// (row.narrative_from_fetch === 1) OR is longer than what's already stored.

const { openDb, upsert } = require('../src/db');

function freshDb() {
  return openDb(':memory:');
}

const REAL_NARRATIVE =
  'TWA Flight 800 was a Boeing 747-131 that exploded and crashed into the ' +
  'Atlantic Ocean near East Moriches, New York, on July 17, 1996, killing ' +
  'all 230 people on board. The National Transportation Safety Board ' +
  'determined the probable cause was an explosion of flammable fuel/air ' +
  'vapors in the center wing fuel tank, likely from a short circuit outside ' +
  'the tank that allowed excess voltage to enter the tank through electrical ' +
  'wiring. The exact source of the short circuit was never conclusively ' +
  'identified, but investigators concluded the fuel/air vapor was flammable ' +
  'because the center wing tank was nearly empty, allowing vapors and air to ' +
  'mix in an explosive ratio, likely raised to an unusually high temperature ' +
  'by heat-generating air conditioning packs directly underneath the tank.';

const BOILERPLATE_FALLBACK =
  'On July 17, 1996, an aircraft was involved in an aviation accident.\n\n' +
  'This incident is catalogued in the Wikidata aviation-safety dataset, ' +
  'which tracks more than three thousand documented aircraft accidents and ' +
  'aviation occurrences spanning the entire history of powered and unpowered ' +
  'flight. Cross-referencing records like this with primary investigative ' +
  'sources helps researchers analyse historical safety trends and identify ' +
  'recurring contributing factors across eras of aviation.';

function baseRow(overrides = {}) {
  return {
    q_id: 'Q7727806',
    source_url: 'https://www.wikidata.org/wiki/Q7727806',
    label: 'TWA Flight 800',
    slug: '1996-07-17-twa-flight-800',
    event_date: '1996-07-17',
    narrative_text: REAL_NARRATIVE,
    narrative_from_fetch: 1,
    probable_cause: 'fuel tank explosion',
    factors_json: null,
    fetched_at: 1000,
    updated_at: 1000,
    ...overrides,
  };
}

describe('wikidata db.upsert — narrative preservation', () => {
  it('an enriched (--enrich) run overwrites a stored narrative with the new fetch', () => {
    const db = freshDb();
    upsert(db, baseRow());
    const updatedText = REAL_NARRATIVE + ' Updated with a corrected detail.';
    upsert(db, baseRow({ narrative_text: updatedText, narrative_from_fetch: 1, updated_at: 2000 }));
    const got = db.prepare('SELECT narrative_text FROM accidents WHERE q_id = ?').get('Q7727806');
    expect(got.narrative_text).toBe(updatedText);
  });

  it('a plain re-run (no --enrich) does NOT overwrite a real narrative with the boilerplate fallback', () => {
    const db = freshDb();
    upsert(db, baseRow()); // enriched, real Wikipedia narrative stored
    upsert(db, baseRow({ narrative_text: BOILERPLATE_FALLBACK, narrative_from_fetch: 0, updated_at: 2000 }));
    const got = db.prepare('SELECT narrative_text FROM accidents WHERE q_id = ?').get('Q7727806');
    expect(got.narrative_text).toBe(REAL_NARRATIVE);
  });

  it('a plain re-run DOES write a narrative on first insert (no prior row to preserve)', () => {
    const db = freshDb();
    upsert(db, baseRow({ narrative_text: BOILERPLATE_FALLBACK, narrative_from_fetch: 0 }));
    const got = db.prepare('SELECT narrative_text FROM accidents WHERE q_id = ?').get('Q7727806');
    expect(got.narrative_text).toBe(BOILERPLATE_FALLBACK);
  });

  it('a non-fetch narrative that is LONGER than the stored one still overwrites it', () => {
    const db = freshDb();
    upsert(db, baseRow({ narrative_text: 'short stub', narrative_from_fetch: 0 }));
    const longerFallback = BOILERPLATE_FALLBACK; // longer than "short stub"
    upsert(db, baseRow({ narrative_text: longerFallback, narrative_from_fetch: 0, updated_at: 2000 }));
    const got = db.prepare('SELECT narrative_text FROM accidents WHERE q_id = ?').get('Q7727806');
    expect(got.narrative_text).toBe(longerFallback);
  });

  it('defaults narrative_from_fetch to 0 when the row omits it (defensive default in upsert)', () => {
    const db = freshDb();
    const row = baseRow();
    delete row.narrative_from_fetch;
    upsert(db, row); // first insert always writes regardless
    const shorter = { ...row, narrative_text: 'short', updated_at: 2000 };
    delete shorter.narrative_from_fetch;
    upsert(db, shorter);
    const got = db.prepare('SELECT narrative_text FROM accidents WHERE q_id = ?').get('Q7727806');
    // shorter, non-fetch (defaulted to 0) narrative must NOT clobber the longer stored one
    expect(got.narrative_text).toBe(REAL_NARRATIVE);
  });

  it('other columns (label, slug, probable_cause) still refresh unconditionally', () => {
    const db = freshDb();
    upsert(db, baseRow());
    upsert(db, baseRow({
      label: 'TWA Flight 800 (updated label)',
      probable_cause: 'revised cause',
      narrative_from_fetch: 0,
      updated_at: 2000,
    }));
    const got = db.prepare('SELECT label, probable_cause, updated_at FROM accidents WHERE q_id = ?').get('Q7727806');
    expect(got.label).toBe('TWA Flight 800 (updated label)');
    expect(got.probable_cause).toBe('revised cause');
    expect(got.updated_at).toBe(2000);
  });
});
