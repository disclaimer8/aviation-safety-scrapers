'use strict';
//
// scrape._internal — pure decision logic extracted from the browser-driven
// scraper (see src/scrape.js). No Playwright/browser required: these are the
// guards that stop a bot-challenge/interstitial page (or a listing broken by
// a site redesign) from silently reporting "0 rows, must be past the end".

const fs = require('fs');
const path = require('path');
const { _internal } = require('../src/scrape');
const { assertNotChallengePage, assertFirstPageNotEmpty } = _internal;

const FIXTURES = path.join(__dirname, 'fixtures', 'atsb');
const REAL_ATSB_HTML = fs.readFileSync(path.join(FIXTURES, 'ao_2023_014.html'), 'utf8');

const CHALLENGE_HTML = `<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div id="challenge-running">Checking your browser before accessing www.atsb.gov.au.</div></body></html>`;

describe('scrape._internal.assertNotChallengePage', () => {
  it('does not throw when rows are non-empty, regardless of page shell', () => {
    expect(() => assertNotChallengePage([{ investigation_id: 'AO-2024-001' }], CHALLENGE_HTML, 3)).not.toThrow();
  });

  it('does not throw when rows are empty but the page is a genuine ATSB/GovCMS response', () => {
    // A real ATSB page shell (Drupal generator meta + CivicTheme header) with
    // 0 result anchors — the normal "past the last listing page" case.
    expect(() => assertNotChallengePage([], REAL_ATSB_HTML, 614)).not.toThrow();
  });

  it('throws when rows are empty AND the page does not look like ATSB/GovCMS (challenge/interstitial)', () => {
    expect(() => assertNotChallengePage([], CHALLENGE_HTML, 3))
      .toThrow(/challenge\/interstitial/);
  });

  it('throws on a blank/empty response body too', () => {
    expect(() => assertNotChallengePage([], '', 0)).toThrow(/challenge\/interstitial/);
  });
});

describe('scrape._internal.assertFirstPageNotEmpty', () => {
  it('does not throw when the first page has rows', () => {
    expect(() => assertFirstPageNotEmpty([{ investigation_id: 'AO-2024-001' }], 0, 0)).not.toThrow();
  });

  it('does not throw when a LATER page (not the first) is empty', () => {
    expect(() => assertFirstPageNotEmpty([], 614, 0)).not.toThrow();
  });

  it('throws when the FIRST page (pg === startPage) is empty', () => {
    expect(() => assertFirstPageNotEmpty([], 0, 0)).toThrow(/first page/);
  });

  it('honours a non-zero startPage as "first"', () => {
    expect(() => assertFirstPageNotEmpty([], 50, 50)).toThrow(/first page/);
    expect(() => assertFirstPageNotEmpty([], 51, 50)).not.toThrow();
  });
});
