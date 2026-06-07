'use strict';
const fs = require('fs');
const path = require('path');
const makParse = require('../src/parse');

const FIXTURES = path.join(__dirname, 'fixtures', 'mak');
const yearHtml      = fs.readFileSync(path.join(FIXTURES, 'year-2018.html'), 'utf8');
const detailHtml    = fs.readFileSync(path.join(FIXTURES, 'an-148-saratov.html'), 'utf8');
const oldStyleHtml  = fs.readFileSync(path.join(FIXTURES, 'old-style-sm-92t.html'), 'utf8');

describe('makParse.parseYearListing', () => {
  it('extracts the 42 known 2018 slugs from the live year-filter page', () => {
    const slugs = makParse.parseYearListing(yearHtml);
    // Empirically MAK published 42 investigations in 2018. The fixture is a
    // verbatim curl of /rassledovaniya/?YEAR=2018, so this number is a
    // stable contract — a regression here means our scraper-discovery
    // missed records or grabbed institutional pages by mistake.
    expect(slugs.length).toBe(42);
    expect(slugs).toContain('an-148-100b-ra-61704-11-02-2018');
    expect(slugs).toContain('boeing-737-800-vq-bji-01-09-2018');
  });

  it('excludes the three institutional sibling slugs', () => {
    const slugs = makParse.parseYearListing(yearHtml);
    // These appear in the global nav and live under /rassledovaniya/ but
    // are NOT accident reports — the scraper must skip them.
    expect(slugs).not.toContain('bezopasnost-poletov');
    expect(slugs).not.toContain('o-komissii');
    expect(slugs).not.toContain('tekhnicheskaya-laboratoriya');
  });
});

describe('makParse.parseDetail', () => {
  // The Saratov An-148 is the highest-profile MAK case in the fixture
  // (71 fatalities, full final report posted, DMS coordinates present).
  // Exercising every field on this single record covers the vast majority
  // of the parser surface.
  let rec;
  beforeAll(() => {
    rec = makParse.parseDetail(detailHtml, 'an-148-100b-ra-61704-11-02-2018');
  });

  it('captures the canonical identifying fields', () => {
    expect(rec.slug).toBe('an-148-100b-ra-61704-11-02-2018');
    expect(rec.source_url).toBe('https://mak-iac.org/rassledovaniya/an-148-100b-ra-61704-11-02-2018/');
    expect(rec.event_date).toBe('11.02.2018');
    expect(rec.normalized_date).toBe('2018-02-11');
    expect(rec.aircraft_model).toBe('Ан-148-100В');
    expect(rec.registration).toBe('RA-61704');
    expect(rec.operator).toBe('АО "Саратовские авиалинии"');
  });

  it('parses DMS coordinates into decimal degrees with six-digit precision', () => {
    expect(rec.lat).toBeCloseTo(55.299, 3);
    expect(rec.lon).toBeCloseTo(38.404, 3);
  });

  it('parses fatalities + flags hull_loss when damage indicates destruction', () => {
    expect(rec.fatalities).toBe(71);
    expect(rec.damage_level).toBe('ВС разрушено');
    expect(rec.hull_loss).toBe(1);
  });

  it('flags status_flag=final and pulls both interim + final report PDFs', () => {
    expect(rec.status_flag).toBe('final');
    expect(rec.investigation_status).toBe('Расследование завершено');
    expect(rec.investigation_closed_date).toBe('11.06.2019');
    expect(rec.report_pdf_final).toMatch(/\/upload\/iblock\/[a-f0-9]+\/report_ra-61704\.pdf$/);
    expect(rec.report_pdf_interim).toMatch(/\/upload\/iblock\/[a-f0-9]+\/report_ra-61704_pr\.pdf$/);
    // No English version for this report — confirms the parser doesn't
    // silently fabricate URLs when none exist on the page.
    expect(rec.report_pdf_en).toBeNull();
  });

  it('null-fills truly empty fields rather than emitting empty strings', () => {
    // Saratov page leaves these fields blank in MAK's CMS template.
    expect(rec.serial_number).toBeNull();
    expect(rec.remark).toBeNull();
    expect(rec.work_type).toBeNull();
  });
});

describe('makParse.parseDetail — pre-2014 layout (bare-filename PDF anchor)', () => {
  // First prod ingest revealed a parser regression on every record from
  // 2004-2009: those pages render the PDF anchor as "report_<reg>.pdf
  // (N MB)" with no "Окончательный отчёт" label, so the original regex
  // missed them and dropped the link into reportLinks.otherPdfs (which
  // the worker doesn't read). 65 of 65 pre-2014 records landed with
  // report_pdf_final = null and zero narratives got written. The fix
  // recognises any /upload/iblock/.../report_*.pdf URL with no _pr or
  // _en suffix as the final report.
  let rec;
  beforeAll(() => {
    rec = makParse.parseDetail(oldStyleHtml, '13-dekabrya-eevs-sm-92t-ra-0257g');
  });

  it('captures the bare-filename PDF anchor as report_pdf_final', () => {
    expect(rec.report_pdf_final).toBe('https://mak-iac.org/upload/iblock/38b/report_ra-0257g.pdf');
    expect(rec.report_pdf_interim).toBeNull();
    expect(rec.report_pdf_en).toBeNull();
  });

  it('still parses core metadata from the older slug pattern', () => {
    expect(rec.slug).toBe('13-dekabrya-eevs-sm-92t-ra-0257g');
    expect(rec.normalized_date).toBe('2009-12-13');
    expect(rec.registration).toBe('RA-0257G');
    expect(rec.aircraft_model).toBe('ЕЭВС СМ-92Т');
    expect(rec.fatalities).toBe(8);
    expect(rec.damage_level).toBe('ВС уничтожено');
    expect(rec.hull_loss).toBe(1);
    expect(rec.status_flag).toBe('final');
  });
});

describe('makParse._internal helpers', () => {
  const i = makParse._internal;

  it('normalizeDate handles DD.MM.YYYY and rejects garbage', () => {
    expect(i.normalizeDate('11.02.2018')).toBe('2018-02-11');
    expect(i.normalizeDate('1.5.2026')).toBe('2026-05-01');
    expect(i.normalizeDate('')).toBeNull();
    expect(i.normalizeDate('11/02/2018')).toBeNull();
    expect(i.normalizeDate(null)).toBeNull();
  });

  it('statusFlag maps Russian status phrases to canonical labels', () => {
    expect(i.statusFlag('Расследование завершено')).toBe('final');
    expect(i.statusFlag('Ведется расследование')).toBe('interim');
    expect(i.statusFlag('Ведётся расследование')).toBe('interim');
    expect(i.statusFlag('Полная чушь')).toBe('unknown');
    expect(i.statusFlag(null)).toBe('unknown');
  });

  it('isHullLoss recognises "разрушено" and "уничтожено" as destruction', () => {
    expect(i.isHullLoss('ВС разрушено')).toBe(1);
    expect(i.isHullLoss('ВС уничтожено')).toBe(1);
    expect(i.isHullLoss('Значительные повреждения ВС')).toBe(0);
    expect(i.isHullLoss(null)).toBeNull();
  });

  it('parseLat / parseLon flip sign for southern / western hemispheres', () => {
    expect(i.parseLat(`55°17'57.3'' СШ`)).toBeCloseTo(55.299, 3);
    expect(i.parseLat(`33°51'31.0'' ЮШ`)).toBeCloseTo(-33.859, 3); // Sydney-ish
    expect(i.parseLon(`38°24'15.5'' ВД`)).toBeCloseTo(38.404, 3);
    expect(i.parseLon(`73°56'00.0'' ЗД`)).toBeCloseTo(-73.933, 3); // NYC-ish
    expect(i.parseLat(null)).toBeNull();
    expect(i.parseLon('garbage')).toBeNull();
  });
});

describe('makParse.extractNarrative section-anchor heuristic', () => {
  // We don't ship a fixture PDF (these are 2-8 MB and the parser is
  // covered against live data already). Instead, exercise extractSection
  // against synthetic text that reproduces MAK's TOC-vs-real-heading
  // pattern: ALLCAPS in the table of contents, Title-Case at the real
  // section start.
  const synthText = [
    'Содержание',
    'ЗАКЛЮЧЕНИЕ ............................................. 140',
    'РЕКОМЕНДАЦИИ ........................................... 143',
    '',
    '… много вступительного текста …',
    '',
    'Заключение',
    'Катастрофа произошла из-за обледенения ППД и потери ',
    'контроля экипажем за параметрами полёта.',
    '',
    'Рекомендации',
    'Доработать тренажёр; пересмотреть программу подготовки.',
  ].join('\n');

  it('skips the ALLCAPS TOC entry and lands on the real Title-Case heading', () => {
    const out = makParse._internal.extractSection(
      synthText,
      'Заключение',
      ['Рекомендации'],
      500,
    );
    expect(out).toMatch(/^Катастрофа произошла/);
    expect(out).not.toMatch(/140/);            // TOC page-number leak
    expect(out).not.toMatch(/Рекомендации/);   // end-heading leak
  });

  it('returns null when the heading is absent entirely', () => {
    expect(makParse._internal.extractSection('лорем ипсум', 'Заключение', [], 100)).toBeNull();
  });

  // ── pre-2014 layout: the Title-Case anchor recurs ───────────────────────
  // Old MAK reports spell the flight-history heading with ё ("История
  // полёта") and repeat "Заключение" as a certificate cross-reference
  // ("Заключение No 02/…") before the real conclusion. indexOf-first then
  // grabs garbage (verified live on report_ra-0880g / ra-0257g / ra-20413,
  // all scored qs=20 because narrative_text came back null and
  // probable_cause was a certificate blurb).

  it('picks the real conclusion over an earlier "Заключение No …" certificate cross-reference', () => {
    const text = [
      'СОДЕРЖАНИЕ',
      'ЗАКЛЮЧЕНИЕ ............................................. 98',
      '',
      'Заключение No 02/0391 по оценке соответствия единичного экземпляра ВС',
      'имеет ряд ошибочных данных, перенесённых в карту данных самолёта.',
      '',
      'Заключение',
      'Катастрофа самолёта произошла в результате сваливания в штопор после взлёта.',
      '',
      'Рекомендации',
      'Доработать программу подготовки.',
    ].join('\n');
    const out = makParse._internal.extractSection(
      text, 'Заключение', ['Рекомендации', 'Недостатки', 'Приложения'], 2500,
      makParse._internal.CONCLUSION_OPENER,
    );
    expect(out).toMatch(/^Катастрофа самолёта произошла/);
    expect(out).not.toMatch(/02\/0391/);     // certificate cross-ref leak
  });

  it('matches "История полёта" (ё) for the е-spelled heading and skips the TOC contents line', () => {
    const text = [
      'ИСТОРИЯ ПОЛЁТА ............ 6',
      '1.1. История полёта 6 1.2. Телесные повреждения 8',   // detailed-contents line
      '',
      'Фактическая информация',
      'История полёта',
      '05.03.2009 КВС выполнял учебно-тренировочный полёт по маршруту, днём.',
      '',
      '1.2. Телесные повреждения',
      'нет',
    ].join('\n');
    const out = makParse._internal.extractSection(
      text, 'История полета', ['Телесные повреждения', '1.2.', '1 . 2 .'], 1800,
      makParse._internal.NOT_TOC_LINE,
    );
    expect(out).toMatch(/^05\.03\.2009 КВС/);
  });

  it('falls back to the first occurrence when no occurrence satisfies the predicate', () => {
    const text = 'Заключение\nтекст без ключевого слова исхода продолжается здесь.';
    const out = makParse._internal.extractSection(
      text, 'Заключение', ['Рекомендации'], 500, makParse._internal.CONCLUSION_OPENER,
    );
    expect(out).toMatch(/^текст без ключевого слова/);   // unchanged legacy behaviour
  });
});
