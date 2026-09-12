'use strict';
const fs = require('fs');
const path = require('path');
const { DOCS_META, composeNarrative } = require('../src/meta');

const DOCS = path.join(__dirname, '..', 'docs');

describe('the curated metadata', () => {
  it('has a text file on disk for every record', () => {
    const missing = DOCS_META
      .filter((m) => !fs.existsSync(path.join(DOCS, `${m.f}.txt`)))
      .map((m) => m.f);
    expect(missing).toEqual([]);
  });

  it('has no text file that no record claims', () => {
    // The other direction matters too: a doc nobody references is text that
    // was extracted and then silently dropped from the build.
    const claimed = new Set(DOCS_META.map((m) => m.f));
    const orphans = fs
      .readdirSync(DOCS)
      .filter((f) => f.endsWith('.txt'))
      .map((f) => f.replace(/\.txt$/, ''))
      .filter((f) => !claimed.has(f));
    expect(orphans).toEqual([]);
  });

  it('gives every record a unique case_id', () => {
    const ids = DOCS_META.map((m) => m.case_id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('dates every record as ISO yyyy-mm-dd', () => {
    for (const m of DOCS_META) {
      expect(m.event_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    }
  });

  it('points every record at an official caac.gov.cn or mem.gov.cn URL', () => {
    // These are 官方文件; a source_url anywhere else would mean the text came
    // from a republisher, which changes both provenance and copyright.
    for (const m of DOCS_META) {
      const host = new URL(m.url).hostname;
      expect(host === 'www.mem.gov.cn' || host.endsWith('caac.gov.cn')).toBe(true);
    }
  });

  it('records fatalities only where the report states them', () => {
    const withFatal = DOCS_META.filter((m) => m.fatal !== null);
    expect(withFatal.map((m) => m.case_id).sort()).toEqual([
      'CAAC-MU5735-PRELIM',
      'CAAC-MU5735-PROGRESS',
      'MEM-2004-1121',
      'MEM-2010-0824',
    ]);
    for (const m of withFatal) expect(m.fatal).toBeGreaterThan(0);
  });
});

describe('composeNarrative', () => {
  it('produces a narrative for every record', () => {
    for (const m of DOCS_META) {
      const n = composeNarrative(DOCS, m);
      expect(typeof n).toBe('string');
      expect(n.length).toBeGreaterThan(1000);
    }
  });

  it('leads with the report title when the text does not already', () => {
    const m = DOCS_META.find((x) => x.f === 'membaotou');
    const n = composeNarrative(DOCS, m);
    expect(n.startsWith(m.title.slice(0, 10))).toBe(true);
  });

  it('collapses the whitespace the extraction left behind', () => {
    for (const m of DOCS_META) {
      const n = composeNarrative(DOCS, m);
      expect(n).not.toMatch(/\s{2,}/);
      expect(n).not.toMatch(/^\s|\s$/);
    }
  });

  it('returns null rather than throwing when a text file is absent', () => {
    expect(composeNarrative(DOCS, { f: 'no-such-doc', title: 'x' })).toBeNull();
  });

  it('clears the 300 characters prod scores a narrative on', () => {
    for (const m of DOCS_META) {
      expect(composeNarrative(DOCS, m).length).toBeGreaterThanOrEqual(300);
    }
  });
});
