'use strict';
//
// atsbBrowserScraper — browser-based egress for the redesigned ATSB site.
//
// Why a real browser: ATSB sits behind Akamai, which RST/resets the HTTP/2
// stream for any client whose TLS+H2 fingerprint isn't a real browser
// (plain `curl` / Node `fetch` both get nothing — TCP/TLS complete, then the
// stream is killed with INTERNAL_ERROR). On top of that, the aviation
// investigations listing is JS-hydrated (Drupal Views AJAX), so even if we
// could fetch the listing URL the result rows wouldn't be in the static
// HTML. A headless Chromium solves both: it passes the fingerprint check and
// executes the hydration. This is the egress a plain HTTP client can never
// achieve. Run it from a residential / non-datacenter IP for best results.
//
// `playwright` is lazy-required so an environment that never runs the scraper
// does not need the package or a Chromium download. Install it only where the
// scraper runs: `npm i playwright && npx playwright install chromium`.

const atsbParse = require('./parse');

const BASE = 'https://www.atsb.gov.au';
// Aviation investigations, newest occurrence first. transport_mode=607 = Aviation.
const listingUrlFor = page =>
  `${BASE}/investigations?atsb_sort=occurrence_date_desc&transport_mode=607&page=${page}`;
const detailUrlFor = slug => `${BASE}/investigations/${String(slug).toLowerCase()}`;

const UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ' +
           '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36';

const sleep = ms => new Promise(r => setTimeout(r, ms));

// ── Pure decision logic (no browser / I/O — unit-testable directly) ────────
//
// Both guards exist because "0 rows on this page" is ambiguous: it's the
// NORMAL signal that we've walked past the last listing page, but it's also
// exactly what a bot-challenge/interstitial page or a parser broken by a
// site redesign produces. Silently treating every 0-row page as "the end"
// is how a scrape that never got past Akamai reports a clean listed=0 run.

// Throws when a listing page has 0 result rows AND doesn't even look like a
// genuine ATSB/GovCMS response — i.e. Akamai almost certainly served a
// challenge/interstitial instead of the real listing.
function assertNotChallengePage(rows, html, pageNum) {
  if (rows.length === 0 && !atsbParse.looksLikeAtsbPage(html)) {
    throw new Error(
      `ATSB listing page ${pageNum} returned 0 rows and does not look like a ` +
      'genuine ATSB/GovCMS page (missing Drupal generator meta / CivicTheme ' +
      'header) — likely an Akamai challenge/interstitial page, not the real listing.'
    );
  }
}

// Throws when the FIRST page walked (pg === startPage) has 0 rows. The
// archive has thousands of aviation reports, so an empty first page always
// means the scrape failed (challenge page, or a parser selector drifting out
// of sync with a site redesign) — never that we've reached the end of a
// listing that starts at page 0. Independent of assertNotChallengePage: this
// still fires even when the empty page DOES look like a genuine ATSB
// response (e.g. the listing markup changed and parseListingPage can no
// longer find any result anchors).
function assertFirstPageNotEmpty(rows, pg, startPage) {
  if (rows.length === 0 && pg === startPage) {
    throw new Error(
      `ATSB listing page ${pg} (the first page) returned 0 rows — treating an ` +
      'empty first page as a scrape failure, not "past the last page".'
    );
  }
}

// ⚠️ headless is DISABLED by default on purpose. Akamai resets the HTTP/2
// stream (net::ERR_HTTP2_PROTOCOL_ERROR) for headless Chromium's TLS/H2
// fingerprint — verified across bundled chromium, `channel:'chrome'`, and
// --disable-blink-features=AutomationControlled. A HEADED browser passes
// cleanly. On a display-less server run the whole process under Xvfb:
// `xvfb-run -a node src/cli.js build`. Override with ATSB_HEADLESS=1 only if a
// future egress (real display / stealth) is proven.
const os = require('os');
const path = require('path');

function createScraper({
  headless = process.env.ATSB_HEADLESS === '1',
  navTimeoutMs = 45_000,
  perRequestDelayMs = 2500,
  // A PERSISTENT profile is load-bearing: Akamai issues a JS-solved
  // clearance cookie (_abck / bm_sz) on the first challenged navigation and
  // honours it thereafter. An ephemeral context gets re-challenged on nearly
  // every navigation (listing may pass, the next detail page resets). Reusing
  // one on-disk profile across pages AND across weekly runs keeps the trust
  // token warm. Point ATSB_PROFILE_DIR at a persistent path to retain it.
  userDataDir = process.env.ATSB_PROFILE_DIR || path.join(os.tmpdir(), 'atsb-chromium-profile'),
  // Optional egress proxy (e.g. Cloudflare WARP in proxy mode:
  // ATSB_PROXY=socks5://127.0.0.1:40000). Lets a deep backfill rotate the
  // egress IP between batches to dodge Akamai's per-IP rate challenge.
  proxy = process.env.ATSB_PROXY || null,
} = {}) {
  let context = null, page = null;
  let _detachSignals = null;

  // Akamai resets the H2 stream (net::ERR_HTTP2_PROTOCOL_ERROR) when it wants
  // to re-challenge the IP/session. Retry with escalating backoff so the
  // challenge window clears; between attempts re-warm via the homepage to
  // refresh the clearance cookie.
  async function gotoWithRetry(p, url, attempts = 5) {
    let lastErr;
    for (let i = 0; i < attempts; i++) {
      try {
        await p.goto(url, { waitUntil: 'domcontentloaded' });
        return;
      } catch (e) {
        lastErr = e;
        await sleep(4000 * Math.pow(2, i)); // 4/8/16/32/64 s
        if (i >= 1) { try { await p.goto(BASE + '/', { waitUntil: 'domcontentloaded' }); await sleep(2000); } catch { /* noop */ } }
      }
    }
    throw lastErr;
  }

  async function ensurePage() {
    if (page) return page;
    const { chromium } = require('playwright');
    const launchOpts = {
      headless,
      userAgent: UA,
      locale: 'en-AU',
      timezoneId: 'Australia/Sydney',
      viewport: { width: 1366, height: 900 },
      args: ['--disable-blink-features=AutomationControlled'],
    };
    if (proxy) launchOpts.proxy = { server: proxy };
    context = await chromium.launchPersistentContext(userDataDir, launchOpts);
    // Tear the headed Chromium down even on signal kill (systemd stop, reboot,
    // sibling OOM) — without this the browser orphans under Xvfb and piles up
    // across weekly runs. close() detaches these; a 5s failsafe guards a hung close.
    if (!_detachSignals) {
      const onSignal = () => {
        const failsafe = setTimeout(() => process.exit(143), 5000);
        if (failsafe.unref) failsafe.unref();
        Promise.resolve(close()).catch(() => {}).finally(() => { clearTimeout(failsafe); process.exit(143); });
      };
      process.once('SIGTERM', onSignal);
      process.once('SIGINT', onSignal);
      _detachSignals = () => {
        process.removeListener('SIGTERM', onSignal);
        process.removeListener('SIGINT', onSignal);
      };
    }
    page = context.pages()[0] || await context.newPage();
    page.setDefaultNavigationTimeout(navTimeoutMs);
    // Warm-up: acquire the Akamai clearance cookie from the homepage before
    // hitting listing/detail pages (skipped if the profile is already warm).
    try { await gotoWithRetry(page, BASE + '/'); await sleep(1500); } catch { /* proceed anyway */ }
    return page;
  }

  // Navigate one aviation listing page and return the parsed rows
  // ({ investigation_id, detail_url }). Empty array ⇒ past the last page —
  // UNLESS the response doesn't even look like a genuine ATSB page, in which
  // case Akamai almost certainly served a challenge/interstitial instead of
  // the real listing; that's a scrape failure, not "we reached the end", so
  // it throws instead of silently returning [] (see atsbParse.looksLikeAtsbPage).
  async function listPage(pageNum) {
    const p = await ensurePage();
    await gotoWithRetry(p, listingUrlFor(pageNum));
    // Wait for the AJAX-hydrated result anchors; tolerate a genuinely empty
    // page (past the end) by capping the wait and falling through.
    await p.waitForSelector('a[href*="/investigations/ao-"]', { timeout: 12_000 }).catch(() => {});
    const html = await p.content();
    await sleep(perRequestDelayMs);
    const rows = atsbParse.parseListingPage(html);
    assertNotChallengePage(rows, html, pageNum);
    return rows;
  }

  // Walk listing pages from `startPage`, yielding deduped rows newest-first,
  // until an empty page OR `maxPages` reached. The pager's "Last" link sits
  // around page 613 (~7,360 aviation reports) — full back-catalogue.
  async function listSlugs({ maxPages = Infinity, startPage = 0, onPage = null } = {}) {
    const seen = new Set();
    const all = [];
    for (let pg = startPage; pg < startPage + maxPages; pg++) {
      let rows;
      try {
        rows = await listPage(pg);
      } catch (e) {
        // transient nav failure — one retry, then give up this page
        await sleep(perRequestDelayMs * 2);
        try { rows = await listPage(pg); }
        catch (e2) {
          if (onPage) onPage(pg, 0, e2.message);
          // A failure on the very first page — whether a thrown
          // challenge-page detection or a genuine nav error — means the
          // WHOLE run produced nothing. That must surface as a hard
          // failure, not a silent "0 rows, must be past the end" (SILENT_FAIL
          // guard — see cli.js).
          if (pg === startPage) throw e2;
          break;
        }
      }
      const fresh = rows.filter(r => r.investigation_id && !seen.has(r.investigation_id));
      for (const r of fresh) seen.add(r.investigation_id);
      all.push(...fresh);
      if (onPage) onPage(pg, fresh.length, null);
      if (rows.length === 0) {
        assertFirstPageNotEmpty(rows, pg, startPage);
        break; // past the last page
      }
    }
    return all;
  }

  async function fetchDetailHtml(url) {
    const p = await ensurePage();
    const full = url.startsWith('http') ? url : BASE + url;
    await gotoWithRetry(p, full);
    await p.waitForSelector('table.ct-table, article h1, h1', { timeout: 15_000 }).catch(() => {});
    const html = await p.content();
    await sleep(perRequestDelayMs);
    return html;
  }

  async function close() {
    if (_detachSignals) { _detachSignals(); _detachSignals = null; }
    try { if (context) await context.close(); } catch { /* noop */ }
    context = page = null;
  }

  return { listSlugs, listPage, fetchDetailHtml, close, _urls: { listingUrlFor, detailUrlFor } };
}

module.exports = {
  createScraper,
  listingUrlFor,
  detailUrlFor,
  _internal: { assertNotChallengePage, assertFirstPageNotEmpty },
};
