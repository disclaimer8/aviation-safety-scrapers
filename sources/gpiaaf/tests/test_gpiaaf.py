# tests/test_gpiaaf.py
from gpiaaf_ingest import gpiaaf

ROOT = gpiaaf.LISTING_ROOT


# ─── decade/year URL harvest ──────────────────────────────────────────────────

YEAR_LINKS = [
    ROOT + "/de-2020-a-2026/2024",
    ROOT + "/de-2020-a-2026/2026",          # ⚠️ current decade tail is -2026
    ROOT + "/de-2010-a-2019/2017",
    ROOT + "/de-1940-a-1949/1946",
    # junk sibling under the current decade (no 4-digit year) — must be dropped
    ROOT + "/de-2020-a-2026/teste",
    # a year OUTSIDE its decade bounds (defensive) — dropped
    ROOT + "/de-2010-a-2019/2099",
    # rail path (must be rejected — aviation only)
    gpiaaf.BASE + "/transporte-ferroviario/x/de-2010-a-2019/2015",
    # global nav chrome
    gpiaaf.BASE + "/institucional/contactos",
]


def test_harvest_year_urls_filters_and_orders():
    out = gpiaaf.harvest_year_urls(YEAR_LINKS)
    # 4 valid aviation year pages: 2024, 2026, 2017, 1946
    assert len(out) == 4
    assert all("/aviacao-civil-reservado/" in u for u in out)
    assert not any("teste" in u for u in out)
    assert not any("2099" in u for u in out)
    assert not any("transporte-ferroviario" in u for u in out)
    assert not any("contactos" in u for u in out)
    # newest-first
    assert out[0].endswith("/2026")
    assert out[-1].endswith("/1946")


def test_year_url_recognises_2026_tail():
    u = ROOT + "/de-2020-a-2026/2026"
    assert gpiaaf.is_year_url(u)
    assert gpiaaf.year_from_url(u) == "2026"
    assert gpiaaf.decade_bounds(u) == (2020, 2026)


def test_year_url_rejects_non_year_sibling():
    assert not gpiaaf.is_year_url(ROOT + "/de-2020-a-2026/teste")
    assert gpiaaf.year_from_url(ROOT + "/de-2020-a-2026/teste") is None


def test_harvest_dedup():
    u = ROOT + "/de-2010-a-2019/2017"
    assert gpiaaf.harvest_year_urls([u, u, u]) == [u]


# ─── case_id slugging ─────────────────────────────────────────────────────────

def test_normalize_case_id_accid():
    assert gpiaaf.normalize_case_id("08/ACCID/2017") == "08-accid-2017"


def test_normalize_case_id_incid():
    assert gpiaaf.normalize_case_id("06/INCID/2017") == "06-incid-2017"


def test_normalize_case_id_year_first_and_aval():
    # seen variants: YYYY/AVAL/NN, YYYY/ACCID/NN
    assert gpiaaf.normalize_case_id("2022/AVAL/13") == "2022-aval-13"
    assert gpiaaf.normalize_case_id("2022/ACCID/05") == "2022-accid-05"


def test_normalize_case_id_evento_registado_is_none():
    # bulletin-only rows publish 'evento registado' (no case number)
    assert gpiaaf.normalize_case_id("evento registado") is None
    assert gpiaaf.normalize_case_id("") is None
    assert gpiaaf.normalize_case_id(None) is None


# ─── event date ───────────────────────────────────────────────────────────────

def test_parse_event_date():
    assert gpiaaf.parse_event_date("2017/10/05") == "2017-10-05"
    assert gpiaaf.parse_event_date("2022/9/1") == "2022-09-01"
    assert gpiaaf.parse_event_date("") is None
    assert gpiaaf.parse_event_date("sem data") is None


# ─── registration (incl. Sem registo) ─────────────────────────────────────────

def test_parse_registration_cs():
    assert gpiaaf.parse_registration("CS-XCM") == "CS-XCM"
    assert gpiaaf.parse_registration("CS-DDO") == "CS-DDO"


def test_parse_registration_foreign():
    assert gpiaaf.parse_registration("G-MYOO") == "G-MYOO"
    assert gpiaaf.parse_registration("OE-XTM") == "OE-XTM"
    assert gpiaaf.parse_registration("HB-LTI") == "HB-LTI"


def test_parse_registration_sem_registo_is_none():
    assert gpiaaf.parse_registration("Sem registo") is None
    assert gpiaaf.parse_registration("Sem registo ") is None
    assert gpiaaf.parse_registration("") is None


def test_parse_registration_multi_aircraft_takes_first():
    # 'G-EZDX ; EI-EBD' multi-aircraft cell
    assert gpiaaf.parse_registration("G-EZDX ;\nEI-EBD") == "G-EZDX"


# ─── bulletin-row filtering / report pick ─────────────────────────────────────

def test_is_bulletin_label():
    assert gpiaaf.is_bulletin_label("Boletim de Divulgação Trimestral  04/2022")
    assert not gpiaaf.is_bulletin_label("Relatório")


def test_pick_report_doc_prefers_relatorio():
    links = [
        {"label": "Relatório", "href": "u?v=R", "title": "d055927.pdf"},
        {"label": "Boletim de Divulgação Trimestral  04/2022", "href": "u?v=B",
         "title": ""},
    ]
    pick = gpiaaf.pick_report_doc(links)
    assert pick["href"] == "u?v=R"


def test_pick_report_doc_bulletin_only_is_none():
    links = [
        {"label": "Boletim de Divulgação Trimestral  04/2022", "href": "u?v=B1",
         "title": ""},
        {"label": "Boletim de Divulgação Trimestral  04/2023", "href": "u?v=B2",
         "title": ""},
    ]
    assert gpiaaf.pick_report_doc(links) is None


def test_pick_report_doc_empty():
    assert gpiaaf.pick_report_doc([]) is None


# ─── pdf id ───────────────────────────────────────────────────────────────────

def test_pdf_id_from_title_and_s3():
    assert gpiaaf.pdf_id_from("d055927.pdf") == "d055927"
    assert gpiaaf.pdf_id_from(
        None,
        "https://x.s3.amazonaws.com/upload/processos/d123456.pdf?X-Amz-...",
    ) == "d123456"
    assert gpiaaf.pdf_id_from("01ACCID2017_RF.pdf") is None  # not a d-number
    assert gpiaaf.pdf_id_from(None, None) is None


# ─── SPA homepage-fallback detection ──────────────────────────────────────────

def test_homepage_fallback_detection():
    # correct render: year heading + a table → NOT a fallback
    assert gpiaaf.is_homepage_fallback("2017", True) is False
    # no table → fallback (homepage misfire)
    assert gpiaaf.is_homepage_fallback("2017", False) is True
    # table present but heading is not a 4-digit year → fallback
    assert gpiaaf.is_homepage_fallback("Investigações concluídas", True) is True
    assert gpiaaf.is_homepage_fallback("", True) is True


# ─── fallback case_id ─────────────────────────────────────────────────────────

def test_fallback_case_id_prefers_dnumber():
    assert gpiaaf.fallback_case_id("u?v=abc", "d055927") == "gpiaaf-d055927"


def test_fallback_case_id_deterministic_hash():
    a = gpiaaf.fallback_case_id("https://x/y?v=AAA")
    b = gpiaaf.fallback_case_id("https://x/y?v=AAA")
    assert a == b and a.startswith("gpiaaf-")
    assert gpiaaf.fallback_case_id("https://x/y?v=BBB") != a


# ─── full table parse ─────────────────────────────────────────────────────────

def _cell(text, links=None):
    return {"text": text, "links": links or []}


def _header_row():
    return [_cell(c) for c in
            ("Data", "Classificação", "Tipo", "Matrícula", "Local",
             "Documento", "Identificação do Processo")]


def test_parse_year_rows_full():
    rows = [
        _header_row(),
        # 1) accident with a report PDF + CS- reg
        [
            _cell("2017/08/02"), _cell("Acidente"), _cell("Cessna 152"),
            _cell("CS-AVA"), _cell("Caparica"),
            _cell("Relatório", [
                {"label": "Relatório", "href": ROOT + "/de-2010-a-2019/2017?v=AAA",
                 "title": "04ACCID2017_RF.pdf"},
            ]),
            _cell("04/ACCID/2017"),
        ],
        # 2) foreign reg accident w/ report (also has a stable d-number title)
        [
            _cell("2017/10/05"), _cell("Acidente"), _cell("Kolb Twinstar Mk IIIM"),
            _cell("G-MYOO"), _cell("Olhão"),
            _cell("Relatório", [
                {"label": "Relatório", "href": ROOT + "/de-2010-a-2019/2017?v=BBB",
                 "title": "d055927.pdf"},
            ]),
            _cell("08/ACCID/2017"),
        ],
        # 3) 'Sem registo' accident
        [
            _cell("2017/09/20"), _cell("Acidente"), _cell("Paramotor Ozone"),
            _cell("Sem registo "), _cell("Alcácer do Sal"),
            _cell("Relatório", [
                {"label": "Relatório", "href": ROOT + "/de-2010-a-2019/2017?v=CCC",
                 "title": "07ACCID2017_RF.pdf"},
            ]),
            _cell("07/ACCID/2017"),
        ],
        # 4) BULLETIN-ONLY row (no report) — kept as metadata, has_report False
        [
            _cell("2022/12/28"), _cell("Incidente"),
            _cell("Gulfstream American GA-7 Cougar"), _cell("D-GZBX"),
            _cell("Cascais"),
            _cell("Boletim de Divulgação Trimestral  04/2022", [
                {"label": "Boletim de Divulgação Trimestral  04/2022",
                 "href": ROOT + "/de-2020-a-2026/2022?v=DDD", "title": ""},
            ]),
            _cell("evento registado"),
        ],
        # 5) report row WITH a bulletin too — bulletin skipped, report taken
        [
            _cell("2022/11/16"), _cell("Incidente"), _cell("Tecnam P2006T"),
            _cell("D-GSEV"), _cell("Cascais"),
            _cell("Relatório\nBoletim de Divulgação Trimestral  04/2022", [
                {"label": "Relatório", "href": ROOT + "/de-2020-a-2026/2022?v=EEE",
                 "title": "d099999.pdf"},
                {"label": "Boletim de Divulgação Trimestral  04/2022",
                 "href": ROOT + "/de-2020-a-2026/2022?v=FFF", "title": ""},
            ]),
            _cell("2022/AVAL/13"),
        ],
    ]
    out = gpiaaf.parse_year_rows(rows, "2017")
    assert len(out) == 5  # header dropped, all 5 data rows kept

    r1 = out[0]
    assert r1["case_id"] == "04-accid-2017"
    assert r1["event_date"] == "2017-08-02"
    assert r1["classification"] == "Acidente"
    assert r1["aircraft"] == "Cessna 152"
    assert r1["registration"] == "CS-AVA"
    assert r1["location"] == "Caparica"
    assert r1["has_report"] is True
    assert r1["doc_url"].endswith("?v=AAA")

    r2 = out[1]
    assert r2["registration"] == "G-MYOO"
    assert r2["pdf_id"] == "d055927"        # from title attr

    r3 = out[2]
    assert r3["registration"] is None       # Sem registo

    r4 = out[3]                              # bulletin-only
    assert r4["case_id"] is None
    assert r4["has_report"] is False
    assert r4["doc_url"] is None
    assert r4["registration"] == "D-GZBX"   # metadata still captured

    r5 = out[4]                             # report + bulletin
    assert r5["case_id"] == "2022-aval-13"
    assert r5["has_report"] is True
    assert r5["doc_url"].endswith("?v=EEE")
    assert r5["pdf_id"] == "d099999"


def test_parse_year_rows_drops_pure_noise():
    # a row with no case number AND no report is dropped entirely
    rows = [
        _header_row(),
        [
            _cell("2022/10/20"), _cell("Incidente"), _cell("Airbus A319"),
            _cell("G-EZDX"), _cell("Faro"),
            _cell("Boletim de Divulgação Trimestral  04/2022", [
                {"label": "Boletim de Divulgação Trimestral  04/2022",
                 "href": "u?v=Z", "title": ""},
            ]),
            _cell("evento registado"),
        ],
    ]
    # bulletin-only WITH metadata is still kept (has_report False) — only rows
    # that are BOTH no-case AND no-report-link survive the drop; this one has a
    # bulletin link so it stays as no_report metadata.
    out = gpiaaf.parse_year_rows(rows)
    assert len(out) == 1
    assert out[0]["has_report"] is False


def test_parse_year_rows_empty():
    assert gpiaaf.parse_year_rows([], "2017") == []
    assert gpiaaf.parse_year_rows([_header_row()], "2017") == []


def test_extract_registration_from_text():
    assert gpiaaf.extract_registration("a aeronave CS-AVA despenhou-se") == "CS-AVA"
    assert gpiaaf.extract_registration("sem matrícula") is None
