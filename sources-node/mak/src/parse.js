'use strict';
//
// makParse.js — pure parsers for Interstate Aviation Committee
// (mak-iac.org) HTML detail pages and the attached final-report PDFs.
// No network I/O — input is bytes (HTML string or PDF buffer), output is
// structured data. The worker handles fetching, retry, rate-limiting,
// and persistence; this module is fully unit-testable against fixtures.
//
// Two key empirical findings drive the heuristics here, verified against
// the 2018 year sample of 42 reports:
//   1. The HTML detail page is a fixed 22-row metadata table — labels are
//      stable Russian strings; we map each to a camelCase key and ignore
//      any unrecognised rows.
//   2. The PDF table-of-contents spells section headings in ALL-CAPS
//      ("ЗАКЛЮЧЕНИЕ"), while the actual section heading uses Title-Case
//      ("Заключение"). Title-Case occurs EXACTLY ONCE in the document —
//      a plain indexOf() lands on the real heading without false hits.

const cheerio = require('cheerio');
const { PDFParse } = require('pdf-parse');

// ── HTML side ─────────────────────────────────────────────────────────────

const LABEL_MAP = {
  'Дата события':                              'eventDate',
  'Регистрационный номер ВС':                  'registration',
  'Место вылета ВС':                           'departureCity',
  'Аэропорт вылета':                           'departureAirport',
  'Планируемый пункт назначения':              'destinationCity',
  'Планируемый аэропорт прилета':              'destinationAirport',
  'Место события':                             'locationText',
  'Широта':                                    'latText',
  'Долгота':                                   'lonText',
  'ВС':                                        'aircraftModel',
  'Заводской №':                               'serialNumber',
  'Эксплуатант ВС':                            'operator',
  'Владелец ВС':                               'owner',
  'Дата завершения расследования (отчета)':    'investigationClosedDate',
  'Количество погибших':                       'fatalitiesText',
  'Точность данных':                           'dataAccuracy',
  'Степень разрушения ВС':                     'damageLevel',
  'Отчет':                                     'reportCellRaw',
  'Вид авиации':                               'aviationKind',
  'Тип работ':                                 'workType',
  'Примечание':                                'remark',
  'Статус расследования':                      'investigationStatus',
};

const BASE = 'https://mak-iac.org';

function isHullLoss(damageLevel) {
  if (!damageLevel) return null;
  return /разрушен|уничтожен/i.test(damageLevel) ? 1 : 0;
}

function statusFlag(text) {
  if (!text) return 'unknown';
  if (/заверш/i.test(text)) return 'final';
  if (/ведется|ведётся/i.test(text)) return 'interim';
  return 'unknown';
}

// MAK renders coordinates as `55°17'57.3'' СШ` (DMS with Cyrillic
// hemisphere markers). Decimal-degrees output, six-digit precision.
// Returns null when the format is unrecognised so we never persist garbage.
function parseLatLon(s, negChars) {
  if (!s) return null;
  const m = s.match(/(\d+)\s*°\s*(\d+)\s*'?\s*([\d.,]+)?\s*''?\s*([A-Za-zА-Яа-я]+)?/);
  if (!m) return null;
  const deg = parseFloat(m[1]);
  const min = parseFloat(m[2]);
  const sec = parseFloat((m[3] || '0').replace(',', '.'));
  if (!Number.isFinite(deg)) return null;
  let dec = deg + (min || 0) / 60 + (sec || 0) / 3600;
  if (m[4] && new RegExp(negChars, 'i').test(m[4])) dec = -dec;
  return Math.round(dec * 1e6) / 1e6;
}
const parseLat = s => parseLatLon(s, 'Ю|S');
const parseLon = s => parseLatLon(s, 'З|W');

function normalizeDate(s) {
  if (!s) return null;
  const m = s.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/);
  if (!m) return null;
  return `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
}

function parseFatalities(s) {
  if (!s || !s.trim()) return null;
  const m = s.match(/\d+/);
  return m ? parseInt(m[0], 10) : null;
}

/**
 * Parse a MAK detail page (`/rassledovaniya/<slug>/`) into a flat record.
 *
 * @param {string} html — raw HTML of the detail page
 * @param {string} slug — URL slug (used to fill source_url + the record's key)
 * @returns {object} fact-record (snake_case keys, ready for makAccidents.upsert)
 */
function parseDetail(html, slug) {
  const $ = cheerio.load(html);
  const fields = {};
  const reportLinks = { interim: null, final: null, finalEn: null };

  $('table').first().find('tr').each((_, tr) => {
    const cells = $(tr).find('td, th');
    if (cells.length < 2) return;
    const label = cells.eq(0).text().replace(/\s+/g, ' ').trim();
    const valueCell = cells.eq(1);
    const text = valueCell.text().replace(/\s+/g, ' ').trim();
    const key = LABEL_MAP[label];
    if (key) fields[key] = text || null;

    if (label === 'Отчет' || label === 'Отчёт') {
      valueCell.find('a[href]').each((_, a) => {
        const href = $(a).attr('href') || '';
        const linkText = $(a).text().trim().toLowerCase();
        if (!/\.pdf(\?|$)/i.test(href)) return;
        const absUrl = href.startsWith('http') ? href : BASE + href;
        // Order matters — _pr.pdf and _en.pdf are subsets of the generic
        // /report_*.pdf URL pattern, so the interim and English branches
        // must run BEFORE the generic-final fallback.
        if (/промежуточн/.test(linkText) || /_pr\.pdf$/i.test(href)) {
          reportLinks.interim = absUrl;
        } else if (/(en|англ)/i.test(linkText) || /_en\.pdf$/i.test(href)) {
          reportLinks.finalEn = absUrl;
        } else if (
          /окончательн/.test(linkText) ||
          /\/report_[^/]+\.pdf$/i.test(href)
        ) {
          // Two equivalent shapes for "this is the final report PDF":
          //   - 2018+ pages label the link "Окончательный отчёт";
          //   - pre-2014 pages just render "report_<reg>.pdf (N MB)" with
          //     no Russian-language disambiguator. Both end in the
          //     canonical /report_<...>.pdf path (no _pr or _en suffix),
          //     and that's enough to identify them. Without this branch
          //     65 of 65 pre-2014 reports landed with report_pdf_final
          //     null even though the link was right there on the page.
          reportLinks.final = absUrl;
        }
      });
    }
  });

  return {
    slug,
    source_url:                  `${BASE}/rassledovaniya/${slug}/`,
    event_date:                  fields.eventDate || null,
    normalized_date:             normalizeDate(fields.eventDate),
    aircraft_model:              fields.aircraftModel || null,
    registration:                fields.registration || null,
    serial_number:               fields.serialNumber || null,
    operator:                    fields.operator || null,
    owner:                       fields.owner || null,
    departure_city:              fields.departureCity || null,
    departure_airport:           fields.departureAirport || null,
    destination_city:            fields.destinationCity || null,
    destination_airport:         fields.destinationAirport || null,
    location_text:               fields.locationText || null,
    lat:                         parseLat(fields.latText),
    lon:                         parseLon(fields.lonText),
    fatalities:                  parseFatalities(fields.fatalitiesText),
    damage_level:                fields.damageLevel || null,
    hull_loss:                   isHullLoss(fields.damageLevel),
    aviation_kind:               fields.aviationKind || null,
    work_type:                   fields.workType || null,
    remark:                      fields.remark || null,
    data_accuracy:               fields.dataAccuracy || null,
    status_flag:                 statusFlag(fields.investigationStatus),
    investigation_status:        fields.investigationStatus || null,
    investigation_closed_date:   fields.investigationClosedDate || null,
    report_pdf_final:            reportLinks.final,
    report_pdf_interim:          reportLinks.interim,
    report_pdf_en:               reportLinks.finalEn,
  };
}

/**
 * Pull every /rassledovaniya/<slug>/ link from a year-listing page.
 * Skips the three institutional siblings (bezopasnost-poletov, o-komissii,
 * tekhnicheskaya-laboratoriya) so we don't try to parse them as reports.
 *
 * @param {string} html
 * @returns {string[]} sorted unique slug list
 */
function parseYearListing(html) {
  const $ = cheerio.load(html);
  const skip = new Set(['bezopasnost-poletov', 'o-komissii', 'tekhnicheskaya-laboratoriya', '']);
  const slugs = new Set();
  $('a[href]').each((_, a) => {
    const href = $(a).attr('href') || '';
    const m = href.match(/^\/rassledovaniya\/([^/?#]+)\/?$/);
    if (m && !skip.has(m[1])) slugs.add(m[1]);
  });
  return [...slugs].sort();
}

// ── PDF side ──────────────────────────────────────────────────────────────

/**
 * Slice a Cyrillic section out of a flat PDF-extracted text. Uses the
 * Title-Case heading anchor — see module header for the empirical rule.
 *
 * @param {string} text — full text from pdf-parse
 * @param {string} heading — Title-Case heading string ("Заключение")
 * @param {string[]} endHeadings — first-match wins; section ends here
 * @param {number} maxChars — hard cap on returned length
 */
// Collapse ё→е so a heading written with either letter matches. pdf-parse
// renders the same MAK heading as "История полёта" (pre-2014 reports) or
// "История полета" (newer) inconsistently; normalizing both sides makes the
// anchor robust. The replace is length-preserving, so indices found in the
// normalized haystack map 1:1 back onto the original `text` for slicing.
const deyo = (s) => s.replace(/ё/g, 'е').replace(/Ё/g, 'Е');

function extractSection(text, heading, endHeadings, maxChars, accept) {
  if (!text) return null;
  const hay = deyo(text);
  const needle = deyo(heading);
  // The Title-Case anchor recurs in older reports — in the table of
  // contents' detailed listing, and (for "Заключение") as cross-references
  // to certificate documents ("Заключение No 02/…") that precede the real
  // section. A plain indexOf-first then lands on noise. `accept(after)` lets
  // the caller pick the occurrence whose body looks like the genuine section
  // (see CONCLUSION_OPENER / NOT_TOC_LINE). Fall back to the first match so
  // behaviour is identical when no predicate is supplied or none qualifies.
  const positions = [];
  for (let i = hay.indexOf(needle); i >= 0; i = hay.indexOf(needle, i + 1)) positions.push(i);
  if (!positions.length) return null;
  let start = positions[0];
  if (accept) {
    const good = positions.find((p) => {
      let a = p + needle.length;
      while (a < hay.length && /\s/.test(hay[a])) a++;
      return accept(hay.slice(a, a + 40));
    });
    if (good != null) start = good;
  }
  let after = start + needle.length;
  while (after < text.length && /\s/.test(text[after])) after++;
  let endPos = -1;
  for (const eh of endHeadings) {
    const ei = hay.indexOf(deyo(eh), after + 50);
    if (ei > 0 && (endPos < 0 || ei < endPos)) endPos = ei;
  }
  const body = endPos > 0 ? text.slice(after, endPos) : text.slice(after, after + maxChars);
  return body
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .replace(/\s{2,}/g, ' ')
    .trim()
    .slice(0, maxChars);
}

// The real "Заключение" section opens with the accident outcome; a stray
// "Заключение No …" certificate cross-reference does not. Input is deyo-
// normalized, so "Серьёзный" is matched via its е-form "Серьезн".
const CONCLUSION_OPENER = (s) =>
  /^(Катастроф|Авари|Авиационн|Серьезн|Инцидент|Происшеств)/.test(s);

// A TOC / detailed-contents line reads "<heading> <pageNo> <nextSectionNo>."
// — i.e. a bare page number followed by another section number. The real
// section body never starts that way (it opens with a date or prose).
const NOT_TOC_LINE = (s) => !/^\d{1,3}\s+\d/.test(s);

/**
 * Extract probable_cause + narrative_text from a final-report PDF buffer.
 *
 * Returns `{ probable_cause, narrative_text, page_count }` — any field can
 * be null if the heading is missing (e.g. an interim report that uses a
 * different chapter layout). Page count helps the worker decide whether
 * a re-extract is needed after PDF replacement.
 */
async function extractNarrative(pdfBuffer) {
  // pdf-parse v2: class API replaces the v1 `pdfParse(buffer)` function.
  const parser = new PDFParse({ data: pdfBuffer });
  let data;
  try {
    data = await parser.getText();
  } finally {
    await parser.destroy();
  }
  const text = data.text || '';
  return {
    page_count:      data.total || 0,
    probable_cause:  extractSection(text, 'Заключение',
                                    ['Рекомендации', 'Недостатки', 'Приложения'], 2500,
                                    CONCLUSION_OPENER),
    narrative_text:  extractSection(text, 'История полета',
                                    ['Телесные повреждения', '1.2.', '1 . 2 .'], 1800,
                                    NOT_TOC_LINE),
  };
}

module.exports = {
  parseYearListing,
  parseDetail,
  extractNarrative,
  // exported for tests
  _internal: { extractSection, normalizeDate, parseLat, parseLon, statusFlag, isHullLoss,
               CONCLUSION_OPENER, NOT_TOC_LINE, deyo },
};
