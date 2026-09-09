'use strict';

const dns = require('node:dns').promises;
const net = require('node:net');

const makParse = require('./parse');

const BASE = 'https://mak-iac.org';
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
          '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36';

// MAK's Bitrix front-end silently terminates connections once a single IP
// sustains ~1 req/s for 30+ minutes, so 2.5s between requests is the floor.
const PER_REQUEST_DELAY_MS = 2500;
const REQUEST_TIMEOUT_MS = 20_000;
const RETRY_MAX = 3;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// report_pdf_final and friends are absolute URLs resolved from hrefs on MAK's
// own pages, so this fetcher follows a target the site chooses. On the ingest
// box that reaches the LAN and the cloud metadata endpoint. The control
// plane's Go path has blocked this since GO-CP-8; here nothing did.
function isPrivateAddress(ip) {
  if (net.isIPv4(ip)) {
    const [a, b] = ip.split('.').map(Number);
    return a === 0 || a === 10 || a === 127
      || (a === 169 && b === 254)            // link-local / cloud metadata
      || (a === 172 && b >= 16 && b <= 31)
      || (a === 192 && b === 168)
      || (a === 100 && b >= 64 && b <= 127)  // CGNAT
      || a >= 224;                           // multicast / reserved
  }
  const s = ip.toLowerCase().replace(/^\[|\]$/g, '');
  return s === '::' || s === '::1'
    || s.startsWith('fe80:') || s.startsWith('fc') || s.startsWith('fd')
    || s.startsWith('::ffff:127.') || s.startsWith('::ffff:10.')
    || s.startsWith('::ffff:169.254.') || s.startsWith('::ffff:192.168.');
}

async function assertPublicTarget(rawUrl) {
  let u;
  try {
    u = new URL(rawUrl);
  } catch {
    throw new Error(`refusing to fetch an unparseable URL: ${rawUrl}`);
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') {
    throw new Error(`refusing to fetch ${u.protocol} URL: ${rawUrl}`);
  }
  const host = u.hostname.replace(/^\[|\]$/g, '');
  let addresses;
  if (net.isIP(host)) {
    addresses = [{ address: host }];
  } else {
    try {
      addresses = await dns.lookup(host, { all: true });
    } catch {
      return; // let fetch report the DNS failure in its own words
    }
  }
  for (const { address } of addresses) {
    if (isPrivateAddress(address)) {
      throw new Error(
        `refusing to fetch ${rawUrl}: ${host} resolves to the private/internal ` +
        `address ${address}`);
    }
  }
}

/**
 * Resilient fetch tuned for MAK's flaky Bitrix CDN:
 *   - AbortController timeout so a hung socket on a multi-MB PDF retries
 *     instead of stalling.
 *   - 3 attempts with 2/4/8s backoff (Bitrix's rate-limit window is short).
 *   - Full browser header set; a bare User-Agent gets flagged after a few
 *     dozen rapid hits.
 *   - Optional Referer for /upload/iblock/*.pdf — Bitrix-CDN serves an RST on
 *     hot-linked file downloads without a matching Referer.
 */
async function fetchWithRetry(url, { binary = false, referer = null } = {}) {
  await assertPublicTarget(url);   // before any attempt, and before any retry
  let lastErr;
  for (let attempt = 0; attempt < RETRY_MAX; attempt++) {
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
    try {
      const headers = {
        'User-Agent': UA,
        Accept: 'text/html,application/xhtml+xml,application/xml,application/pdf;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
      };
      if (referer) headers.Referer = referer;
      const r = await fetch(url, { headers, signal: ctrl.signal });
      if (r.status === 503 || r.status === 429) {
        lastErr = new Error(`HTTP ${r.status}`);
        await sleep(2000 * Math.pow(2, attempt));
        continue;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return binary ? Buffer.from(await r.arrayBuffer()) : await r.text();
    } catch (e) {
      lastErr = e.name === 'AbortError'
        ? new Error(`timeout-${REQUEST_TIMEOUT_MS}ms`)
        : (e.cause?.message ? new Error(`${e.message} (${e.cause.message})`) : e);
      await sleep(2000 * Math.pow(2, attempt));
    } finally {
      clearTimeout(tid);
    }
  }
  throw lastErr;
}

// List the accident slugs reported in a given year.
async function listYear(year) {
  const html = await fetchWithRetry(`${BASE}/rassledovaniya/?YEAR=${year}`);
  return makParse.parseYearListing(html);
}

module.exports = {
  BASE, PER_REQUEST_DELAY_MS, fetchWithRetry, listYear, sleep,
  assertPublicTarget, isPrivateAddress,
};
