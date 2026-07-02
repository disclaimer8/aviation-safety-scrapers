'use strict';
//
// atsbParse.js — pure parsers for ATSB aviation investigation pages.
// No I/O — input is HTML strings, output is structured records.
//
// ⚠️ REWRITTEN 2026-05-25 for the post-redesign ATSB site (Drupal 11 +
// GovCMS, "CivicTheme"). The old field--name-<key> div convention is GONE;
// every metadata field now lives in a <table class="ct-table"> as a
// <th scope="row">Label</th><td>value</td> row, and the report body is a
// flat run of <h3>/<h4> sections inside <article>. Detail pages are also
// AJAX-shell + server-rendered content — parseDetail expects the rendered
// HTML (what a headless browser returns via page.content()), NOT a bare
// `fetch()` of the URL (Akamai resets non-browser TLS fingerprints and the
// listing is JS-hydrated).
//
// Two extractors:
//   parseListingPage(html)  → [{ investigation_id, detail_url, title }]
//   parseDetail(html, slug)  → flat record with 30+ fields

const cheerio = require('cheerio');
const BASE = 'https://www.atsb.gov.au';

// ── Helpers ──────────────────────────────────────────────────────────────

const clean = s => (s == null ? null : String(s).replace(/\s+/g, ' ').trim() || null);

function normalizeDate(ddmmyyyy) {
  if (!ddmmyyyy) return null;
  const m = ddmmyyyy.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  if (!m) return null;
  return `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
}

// Build a {normalized-label → value} map from every data table on the page.
// Data tables carry <th scope="row"> rows; the disclaimer callout boxes
// (class ct-theme-light, no th cells) are skipped. First value wins so the
// occurrence table + the PRIMARY aircraft table take precedence over any
// secondary-aircraft tables on multi-aircraft occurrences.
function buildFieldMap($) {
  const map = {};
  $('table').each((_, t) => {
    $(t).find('tr').each((_, tr) => {
      const th = clean($(tr).find('th').first().text());
      const td = clean($(tr).find('td').first().text());
      if (!th || td == null) return;
      const key = th.toLowerCase();
      if (!(key in map)) map[key] = td;
    });
  });
  return map;
}

// Flatten a DOM subtree into an ordered stream of heading markers + text
// fragments (document order). Headings are emitted once (we don't recurse
// into them); leaf text nodes are emitted verbatim so joining reconstructs
// readable prose. This is robust to the redesign's nested layout-builder
// wrappers, where same-section content is NOT sibling-adjacent.
function flatten($, rootEl) {
  const out = [];
  (function rec(el) {
    const kids = el.children || [];
    for (const c of kids) {
      if (c.type === 'text') {
        const t = (c.data || '').replace(/\s+/g, ' ');
        if (t.trim()) out.push({ kind: 'text', text: t });
      } else if (c.type === 'tag') {
        const tag = (c.tagName || '').toLowerCase();
        if (/^h[1-6]$/.test(tag)) {
          out.push({ kind: 'h', level: +tag[1], text: $(c).text().replace(/\s+/g, ' ').trim() });
        } else if (tag === 'script' || tag === 'style' || tag === 'nav' || tag === 'footer') {
          // skip non-content
        } else {
          rec(c);
        }
      }
    }
  })(rootEl);
  return out;
}

// Return the slice of flattened tokens belonging to the section whose
// heading matches `re`, i.e. everything after that heading up to (but not
// including) the next heading at the same or higher level. null if absent.
function sectionSlice(flat, re) {
  const i = flat.findIndex(tok => tok.kind === 'h' && re.test(tok.text));
  if (i < 0) return null;
  const level = flat[i].level;
  const slice = [];
  for (let j = i + 1; j < flat.length; j++) {
    const tok = flat[j];
    if (tok.kind === 'h' && tok.level <= level) break;
    slice.push(tok);
  }
  return { heading: flat[i].text, level, slice };
}

// Join a token slice into prose, prefixing nested sub-headings on their own
// line (so "What happened" / "Contributing factors" etc. survive as labels).
function sliceToText(slice) {
  const parts = [];
  for (const tok of slice) {
    if (tok.kind === 'h') parts.push(`\n${tok.text}\n`);
    else parts.push(tok.text);
  }
  return parts.join('').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
}

// ── Fatalities ─────────────────────────────────────────────────────────────

// Post-redesign, the aircraft table often carries a structured Injuries cell
// like "Crew - 1 (fatal)" or "Crew - 1 (fatal), Passengers - 2 (fatal)".
// Sum the numbers immediately preceding a "(fatal)" tag. Returns null when
// no Injuries cell is present (caller falls back to the narrative heuristic).
function parseFatalitiesFromInjuries(injuriesText) {
  if (!injuriesText) return null;
  let total = 0;
  let matched = false;
  const re = /(\d+)\s*\(\s*fatal\s*\)/gi;
  let m;
  while ((m = re.exec(injuriesText)) !== null) {
    total += parseInt(m[1], 10);
    matched = true;
  }
  if (matched) return total;
  // An Injuries cell that mentions only non-fatal categories ⇒ 0 fatalities.
  if (/\b(serious|minor|none|nil)\b/i.test(injuriesText)) return 0;
  return null;
}

// Fallback heuristic for the older / short reports that don't carry a
// structured Injuries cell — best-effort regex over the narrative body.
// Returns null when ambiguous so the caller can show the categorical badge.
const NUM = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10 };
function parseFatalitiesFromNarrative(narrative, highestInjuryLevel) {
  if (highestInjuryLevel && /none|nil/i.test(highestInjuryLevel)) return 0;
  if (!narrative) return null;
  const text = narrative.toLowerCase();
  const wordOrDigit = String.raw`(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)`;
  const patterns = [
    new RegExp(`(${wordOrDigit})\\s+(?:of\\s+the\\s+\\w+\\s+)?(?:occupants?|passengers?|crew|pilots?|people|persons?)\\s+(?:were\\s+|was\\s+)?(?:killed|died|fatally\\s+injured|fatally\\s+wounded)`, 'i'),
    new RegExp(`(?:killed|fatally\\s+injured)\\s+(${wordOrDigit})\\s+(?:occupants?|passengers?|crew|people|persons?)`, 'i'),
    /\b(?:the\s+)?pilot\s+(?:was\s+)?(?:killed|fatally\s+(?:injured|wounded))/i,
    /\b(?:both|all)\s+(?:the\s+)?(?:pilots?|crew\s+members?)\s+(?:were\s+)?(?:killed|fatally)/i,
  ];
  for (const re of patterns) {
    const m = text.match(re);
    if (!m) continue;
    if (re.source.startsWith('\\b(?:the\\s+)?pilot')) return 1;
    if (re.source.startsWith('\\b(?:both')) return 2;
    const token = (m[1] || '').toLowerCase();
    if (/^\d+$/.test(token)) return parseInt(token, 10);
    if (NUM[token] != null) return NUM[token];
  }
  const piloPlus = text.match(/pilot\s+and\s+(\d+|one|two|three|four|five)\s+(?:passengers?|occupants?|crew)\s+(?:were\s+)?(?:killed|died|fatally)/i);
  if (piloPlus) {
    const n = /^\d+$/.test(piloPlus[1]) ? parseInt(piloPlus[1], 10) : NUM[piloPlus[1].toLowerCase()];
    return Number.isFinite(n) ? 1 + n : null;
  }
  return null;
}

// ── Findings ─────────────────────────────────────────────────────────────

// Boilerplate that prefixes every Findings section (and a stock lead-in some
// reports add before listing factors). Stripped so the cause chip stays clean.
const ATSB_FINDINGS_BOILERPLATE =
  /ATSB investigation report findings focus on safety factors[\s\S]*?(?=\bcontributing factors?\b|$)/i;
const FROM_EVIDENCE_BOILERPLATE =
  /From the evidence available, the following findings are made[\s\S]*?\.\s*/i;

function extractFindings(flat) {
  const sec = sectionSlice(flat, /^findings$/i);
  if (!sec) return null;

  const out = { contributing_factors: null, other_factors: null, other_findings: null };
  let current = null;
  let buf = [];
  const pre = [];
  const flush = () => {
    if (!current) return;
    const body = buf.join(' ').replace(/\s+/g, ' ').trim();
    if (body) {
      if (/^contributing\s+factors?$/i.test(current)) out.contributing_factors = body;
      else if (/^other\s+factors/i.test(current))     out.other_factors = body;
      else if (/^other\s+findings/i.test(current))     out.other_findings = body;
    }
    buf = [];
  };

  for (const tok of sec.slice) {
    if (tok.kind === 'h') { flush(); current = tok.text; continue; }
    (current ? buf : pre).push(tok.text);
  }
  flush();

  let preText = pre.join(' ').replace(/\s+/g, ' ').trim()
    .replace(ATSB_FINDINGS_BOILERPLATE, '')
    .replace(FROM_EVIDENCE_BOILERPLATE, '')
    .trim();

  const parts = [];
  if (out.contributing_factors) parts.push(`Contributing factors\n${out.contributing_factors}`);
  if (out.other_factors)        parts.push(`Other factors that increased risk\n${out.other_factors}`);
  if (out.other_findings)       parts.push(`Other findings\n${out.other_findings}`);
  if (!parts.length && preText) parts.push(preText);
  const text = parts.join('\n\n').trim();
  if (!text) return null;
  return { ...out, text };
}

// ── parseDetail ──────────────────────────────────────────────────────────

function parseDetail(html, providedInvestigationId) {
  const $ = cheerio.load(html);
  const fields = buildFieldMap($);
  const field = key => fields[key] || null;

  const title = clean($('h1').first().text());
  const investigationId =
    (field('investigation number') || providedInvestigationId || '').toUpperCase() || null;

  const root = $('article').first().get(0) || $('main').first().get(0) || $('body').get(0);
  const flat = root ? flatten($, root) : [];

  // Executive summary (What happened / What the ATSB found / Safety message).
  const exec = sectionSlice(flat, /^executive summary$/i);
  const summaryText = exec ? sliceToText(exec.slice) : null;

  // Safety analysis — long-form reports only.
  const sa = sectionSlice(flat, /^safety analysis$/i);
  const safetyAnalysis = sa ? sliceToText(sa.slice).slice(0, 5000) : null;

  const findings = extractFindings(flat);

  const highestInjuryLevel = field('highest injury level');
  const injuriesText = field('injuries');
  // Provenance: the structured Injuries cell is authoritative; a count recovered
  // from narrative prose is a heuristic guess → flag it estimated so downstream
  // can qualify it (audit I6). Estimated stays 0 when fatalities is null.
  let fatalitiesParsed = parseFatalitiesFromInjuries(injuriesText);
  let fatalitiesEstimated = 0;
  if (fatalitiesParsed == null) {
    fatalitiesParsed = parseFatalitiesFromNarrative(
      `${summaryText || ''} ${safetyAnalysis || ''} ${findings?.text || ''}`.trim(),
      highestInjuryLevel,
    );
    if (fatalitiesParsed != null) fatalitiesEstimated = 1;
  }

  // PDF download — first <a> pointing at a .pdf.
  let pdfUrl = null;
  $('a[href*=".pdf"]').each((_, a) => {
    if (pdfUrl) return;
    const href = $(a).attr('href') || '';
    if (!/\.pdf(\?|$)/i.test(href)) return;
    pdfUrl = href.startsWith('http') ? href : BASE + href;
  });

  const canonical = $('link[rel="canonical"]').attr('href')
                 || $('meta[property="og:url"]').attr('content')
                 || (investigationId ? `${BASE}/investigations/${investigationId.toLowerCase()}` : null);

  return {
    investigation_id:           investigationId,
    title,
    source_url:                 canonical,

    occurrence_date:            field('occurrence date'),
    normalized_date:            normalizeDate(field('occurrence date')),
    release_date:               field('report release date'),
    normalized_release_date:    normalizeDate(field('report release date')),

    location_text:              field('location'),
    state:                      field('state'),

    operator:                   field('aircraft operator'),
    aircraft_manufacturer:      field('manufacturer'),
    aircraft_model:             field('model'),
    aircraft_registration:      field('registration'),
    aircraft_serial:            field('serial number'),

    occurrence_category:        field('aviation occurrence category'),
    occurrence_class:           field('occurrence class'),
    highest_injury_level:       highestInjuryLevel,
    fatalities_parsed:          fatalitiesParsed,
    fatalities_estimated:       fatalitiesEstimated,

    sector:                     field('sector'),
    damage:                     field('damage'),
    operation_type:             field('operation type'),
    departure_point:            field('departure point'),
    destination:                field('destination'),

    investigation_status:       field('investigation status'),
    investigation_level:        field('investigation level'),
    investigation_type:         field('investigation type'),
    report_status:              field('report status'),

    summary_text:               summaryText,
    contributing_factors:       findings?.contributing_factors || null,
    other_factors:              findings?.other_factors        || null,
    other_findings:             findings?.other_findings       || null,
    findings_text:              findings?.text                 || null,
    safety_analysis_text:       safetyAnalysis,

    report_pdf_url:             pdfUrl,
  };
}

// ── parseListingPage ────────────────────────────────────────────────────

// Post-redesign aviation listing lives at
//   /investigations?atsb_sort=occurrence_date_desc&transport_mode=607&page=N
// and is JS-hydrated; once rendered, each result is a bare anchor of the
// form /investigations/ao-YYYY-NNN. We scan every <a> for that shape (the
// browser scraper passes us the rendered HTML). Aviation occurrence ids use
// the AO- prefix; rail (RO-) / marine (MO-) ids are ignored.
function parseListingPage(html) {
  const $ = cheerio.load(html);
  const seen = new Set();
  const rows = [];
  $('a[href]').each((_, a) => {
    const href = ($(a).attr('href') || '').trim();
    const m = href.match(/\/investigations\/(ao-\d{4}-\d{3}[a-z0-9-]*)/i);
    if (!m) return;
    const investigationId = m[1].toUpperCase();
    if (seen.has(investigationId)) return;
    seen.add(investigationId);
    const detailUrl = href.startsWith('http') ? href : BASE + href;
    const title = clean($(a).text());
    rows.push({ investigation_id: investigationId, title, detail_url: detailUrl, status: null, occurrence_date: null, release_date: null });
  });
  return rows;
}

// ── Bot-challenge / interstitial detection ─────────────────────────────────

// Markers emitted by Drupal/GovCMS itself in the server-rendered <head> and
// site chrome — present on EVERY genuine ATSB response (an empty search
// result page included) because they're part of the page template, not the
// JS-hydrated Views results. Verified against the three detail-page fixtures
// in test/fixtures/atsb/ (the listing page shares the same theme/layout).
// An Akamai challenge/interstitial page is served instead of hitting Drupal
// at all, so it carries neither marker.
const ATSB_SHELL_MARKERS = [
  /<meta\s+name="Generator"\s+content="Drupal[^"]*GovCMS/i,
  /data-component-id="civictheme:header"/i,
];

// True when `html` looks like a genuine server-rendered ATSB/GovCMS page
// (whether or not it happens to list any investigations). False means the
// response is very likely a bot-challenge / interstitial page instead of the
// real site — the caller should treat that as a scrape failure, not as "we
// reached the end of the listing".
function looksLikeAtsbPage(html) {
  const s = html || '';
  return ATSB_SHELL_MARKERS.some((re) => re.test(s));
}

module.exports = {
  parseDetail,
  parseListingPage,
  looksLikeAtsbPage,
  _internal: {
    clean, normalizeDate, buildFieldMap, flatten, sectionSlice, sliceToText,
    parseFatalitiesFromInjuries, parseFatalitiesFromNarrative, extractFindings,
  },
};
