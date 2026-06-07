'use strict';
const { joinNtsbTables, buildWeatherSummary, buildFactorsJson } = require('../src/parse');

describe('ntsbParse.joinNtsbTables', () => {
  it('merges narratives + findings + weather + occurrences by ev_id', () => {
    const tables = {
      events: [{ ev_id: 'E1', ev_date: '2026-04-25', ev_city: 'Minneapolis',
                 ev_state: 'MN', ev_country: 'USA', latitude: '44.9778', longitude: '-93.265' }],
      narratives: [{ ev_id: 'E1', narr_accp: 'The pilot reported engine roughness.',
                     narr_cause: 'Failure of the carburetor heat control during cruise flight.' }],
      findings: [
        { ev_id: 'E1', finding_description: 'Loss of engine power (partial)', modifier_code: 'C' },
        { ev_id: 'E1', finding_description: 'Pilot training inadequate', modifier_code: 'F' },
      ],
      occurrences: [{ ev_id: 'E1', occurrence_code: 'CRZ', phase_no: '550' }],
      weather: [{ ev_id: 'E1', wx_cond_basic: 'VMC', wx_temp: '15',
                  wind_vel_kts: '9', wind_dir_deg: '270', vis_sm: '10' }],
      aircraft: [{ ev_id: 'E1', acft_make: 'BEECH', acft_model: 'F33A',
                   regis_no: 'N12345', damage: 'SUBS' }],
    };
    const out = joinNtsbTables(tables);
    expect(out).toHaveLength(1);
    expect(out[0].ev_id).toBe('E1');
    expect(out[0].narrative_text).toBe('The pilot reported engine roughness.');
    expect(out[0].probable_cause).toBe('Failure of the carburetor heat control during cruise flight.');
    expect(JSON.parse(out[0].factors_json)).toEqual([
      'Loss of engine power (partial)', 'Pilot training inadequate',
    ]);
    expect(out[0].phase_of_flight).toBe('CRZ');
    expect(out[0].weather_summary).toBe('VMC, wind 270/09kt, vis 10sm');
  });

  it('skips events with no narratives row', () => {
    const tables = {
      events: [{ ev_id: 'E2', ev_date: '2026-04-25' }],
      narratives: [],
      findings: [], occurrences: [], weather: [], aircraft: [],
    };
    const out = joinNtsbTables(tables);
    expect(out).toHaveLength(0);
  });

  it('empty narrative + empty cause → row still emitted with nulls', () => {
    const tables = {
      events: [{ ev_id: 'E3', ev_date: '2026-04-25' }],
      narratives: [{ ev_id: 'E3', narr_accp: '', narr_cause: '' }],
      findings: [], occurrences: [], weather: [], aircraft: [],
    };
    const out = joinNtsbTables(tables);
    expect(out).toHaveLength(1);
    expect(out[0].narrative_text).toBeNull();
    expect(out[0].probable_cause).toBeNull();
  });
});

describe('ntsbParse.buildWeatherSummary', () => {
  it('combines VMC + wind + visibility', () => {
    expect(buildWeatherSummary({
      wx_cond_basic: 'VMC', wind_vel_kts: '9', wind_dir_deg: '270', vis_sm: '10',
    })).toBe('VMC, wind 270/09kt, vis 10sm');
  });
  it('VMC only when no wind/vis', () => {
    expect(buildWeatherSummary({ wx_cond_basic: 'VMC' })).toBe('VMC');
  });
  it('returns null when all empty', () => {
    expect(buildWeatherSummary({})).toBeNull();
  });
});

describe('ntsbParse.buildFactorsJson', () => {
  it('returns null when no findings', () => {
    expect(buildFactorsJson([])).toBeNull();
  });
  it('returns JSON array of finding_description strings', () => {
    expect(buildFactorsJson([
      { finding_description: 'A' }, { finding_description: 'B' },
    ])).toBe('["A","B"]');
  });
});
