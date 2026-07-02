'use strict';
//
// cli.js SPARQL pagination — regression test for the truncation bug where a
// single-shot `LIMIT 5000` fetch silently dropped rows once multi-valued
// ?causeLabel/?date bindings pushed the GROUP BY row count toward the cap.
// fetchSparql() must now page with OFFSET until a short page comes back,
// and must emit a loud SILENT_FAIL_SUSPECT warning if it exhausts MAX_PAGES
// while still receiving full pages (a real truncation, not just "done").

let fetchSparql, fetchSparqlPage, buildSparql, PAGE_SIZE, MAX_PAGES;

beforeEach(() => {
  jest.resetModules();
  ({ fetchSparql, fetchSparqlPage, buildSparql, PAGE_SIZE, MAX_PAGES } = require('../src/cli'));
});

function makeBindings(n, offset = 0) {
  return Array.from({ length: n }, (_, i) => ({
    event: { value: `http://www.wikidata.org/entity/Q${offset + i}` },
    eventLabel: { value: `Event ${offset + i}` },
  }));
}

function mockFetchSequence(pages) {
  let call = 0;
  global.fetch = jest.fn(async () => {
    const bindings = pages[call] || [];
    call++;
    return {
      ok: true,
      status: 200,
      json: async () => ({ results: { bindings } }),
    };
  });
  return () => call;
}

describe('buildSparql', () => {
  it('embeds LIMIT and OFFSET for the requested page', () => {
    const q0 = buildSparql(0);
    const q1 = buildSparql(5000);
    expect(q0).toMatch(/LIMIT 5000/);
    expect(q0).toMatch(/OFFSET 0/);
    expect(q1).toMatch(/OFFSET 5000/);
  });

  it('aggregates causeLabel via GROUP_CONCAT (not a bare group-by var)', () => {
    const q = buildSparql(0);
    expect(q).toMatch(/GROUP_CONCAT\(DISTINCT \?causeLabelRaw; SEPARATOR=";;"\) AS \?causeLabel/);
    // causeLabel must not appear ungrouped in the GROUP BY clause anymore.
    const groupByLine = q.split('\n').find((l) => l.trim().startsWith('GROUP BY'));
    expect(groupByLine).not.toMatch(/\?causeLabel\b/);
  });
});

describe('fetchSparql pagination', () => {
  afterEach(() => {
    delete global.fetch;
  });

  it('returns all bindings from a single short page without paginating further', async () => {
    mockFetchSequence([makeBindings(3)]);
    const json = await fetchSparql();
    expect(json.results.bindings).toHaveLength(3);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it('follows OFFSET across multiple full pages until a short page ends it', async () => {
    const getCalls = mockFetchSequence([
      makeBindings(PAGE_SIZE, 0),
      makeBindings(PAGE_SIZE, PAGE_SIZE),
      makeBindings(120, PAGE_SIZE * 2), // short page => stop
    ]);
    const json = await fetchSparql();
    expect(json.results.bindings).toHaveLength(PAGE_SIZE * 2 + 120);
    expect(getCalls()).toBe(3);

    // Verify OFFSET actually advanced on each call.
    const urls = global.fetch.mock.calls.map((c) => decodeURIComponent(c[0]));
    expect(urls[0]).toMatch(/OFFSET 0/);
    expect(urls[1]).toMatch(new RegExp(`OFFSET ${PAGE_SIZE}`));
    expect(urls[2]).toMatch(new RegExp(`OFFSET ${PAGE_SIZE * 2}`));
  });

  it('warns loudly with a grep-able marker when MAX_PAGES is exhausted on full pages', async () => {
    const warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const pages = Array.from({ length: MAX_PAGES }, (_, i) => makeBindings(PAGE_SIZE, i * PAGE_SIZE));
    mockFetchSequence(pages);

    const json = await fetchSparql();

    expect(json.results.bindings).toHaveLength(PAGE_SIZE * MAX_PAGES);
    expect(global.fetch).toHaveBeenCalledTimes(MAX_PAGES);
    expect(warnSpy).toHaveBeenCalled();
    const warned = warnSpy.mock.calls.map((c) => c.join(' ')).join('\n');
    expect(warned).toMatch(/SILENT_FAIL_SUSPECT source=wikidata truncated/);

    warnSpy.mockRestore();
  });

  it('does not warn when the loop ends via a short page (not exhaustion)', async () => {
    const warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
    mockFetchSequence([makeBindings(PAGE_SIZE, 0), makeBindings(10, PAGE_SIZE)]);

    await fetchSparql();

    expect(warnSpy).not.toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  it('propagates a non-ok response as an error', async () => {
    global.fetch = jest.fn(async () => ({ ok: false, status: 500 }));
    await expect(fetchSparqlPage(0)).rejects.toThrow('Wikidata SPARQL 500');
  });
});
