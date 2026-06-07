from dgaccl_ingest import dgaccl


# ── hardcoded year-page list (incl. singular 2023) ─────────────────────────


def test_year_pages_count_and_singular_2023():
    pages = dgaccl.YEAR_PAGES
    assert len(pages) == 7
    # ⚠️ 2023 is SINGULAR 'informe-2023', the rest plural 'informes-YYYY'.
    assert "https://www.dgac.gob.cl/informe-2023/" in pages
    assert "https://www.dgac.gob.cl/informes-2023/" not in pages
    assert "https://www.dgac.gob.cl/informes-2025/" in pages
    assert "https://www.dgac.gob.cl/informes-2019/" in pages


def test_year_pages_span_2019_2025():
    years = {dgaccl.year_from_url(u) for u in dgaccl.YEAR_PAGES}
    assert years == {"2019", "2020", "2021", "2022", "2023", "2024", "2025"}


def test_year_from_url_handles_singular_and_plural():
    assert dgaccl.year_from_url(
        "https://www.dgac.gob.cl/informe-2023/") == "2023"
    assert dgaccl.year_from_url(
        "https://www.dgac.gob.cl/informes-2024/") == "2024"


# ── Spanish month-abbrev date → ISO ────────────────────────────────────────


def test_parse_spanish_date_basic():
    assert dgaccl.parse_spanish_date("15 ENE 2024") == "2024-01-15"
    assert dgaccl.parse_spanish_date("01 FEB 2024") == "2024-02-01"
    assert dgaccl.parse_spanish_date("31 DIC 2019") == "2019-12-31"


def test_parse_spanish_date_all_months():
    abbr = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN",
            "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]
    for i, mon in enumerate(abbr, start=1):
        assert dgaccl.parse_spanish_date(f"07 {mon} 2022") == f"2022-{i:02d}-07"


def test_parse_spanish_date_unparseable():
    assert dgaccl.parse_spanish_date("") is None
    assert dgaccl.parse_spanish_date("sin fecha") is None
    assert dgaccl.parse_spanish_date("15 XXX 2024") is None


# ── chrome-PDF filter ──────────────────────────────────────────────────────


def test_is_report_pdf_keeps_informes():
    assert dgaccl._is_report_pdf("Informe-final-2044-24.pdf")
    assert dgaccl._is_report_pdf("Informe-Preliminar-12-meses-2056-24.pdf")
    # No 'Informe' prefix but mentions preliminar.
    assert dgaccl._is_report_pdf("preliminar-12-meses-2066-24.pdf")


def test_is_report_pdf_drops_site_chrome():
    assert not dgaccl._is_report_pdf("Presupuesto_2018.pdf")
    assert not dgaccl._is_report_pdf("PoliticaPrivacidad.pdf")
    assert not dgaccl._is_report_pdf(
        "Nuevo-Listado-de-Articulos-Prohibidos-nov-204.pdf")


def test_is_report_pdf_keeps_on_case_number_match():
    # Odd filename without keywords but embedding the row's case number.
    assert dgaccl._is_report_pdf("2071-24.pdf", case_number="2071")


# ── staged-PDF preference: Final > latest Preliminar ───────────────────────


def test_stage_rank_final_beats_preliminar():
    assert dgaccl._stage_rank("Informe-final-2044-24") > \
        dgaccl._stage_rank("Informe-Preliminar-24-meses-2050-24")


def test_stage_rank_latest_preliminar_wins():
    r36 = dgaccl._stage_rank("Informe-preliminar-36-meses-2071-24")
    r24 = dgaccl._stage_rank("Informe-Preliminar-24-meses-2050-24")
    r12 = dgaccl._stage_rank("Informe-Preliminar-12-meses-2056-24")
    r30d = dgaccl._stage_rank("Informe-Preliminar-30-dias-2059-24")
    assert r36 > r24 > r12 > r30d


def test_stage_rank_unqualified_preliminar_below_meses():
    assert dgaccl._stage_rank("Informe-preliminar-12-meses-x") > \
        dgaccl._stage_rank("Informe-preliminar-x") > \
        dgaccl._stage_rank("random-doc")


# ── case_id construction ───────────────────────────────────────────────────


def test_make_case_id_format():
    assert dgaccl.make_case_id("2044", "24") == "2044-24"


def test_make_case_id_collision_suffix():
    assert dgaccl.make_case_id("2044", "24", taken={"2044-24"}) == "2044-24-2"
    assert dgaccl.make_case_id(
        "2044", "24", taken={"2044-24", "2044-24-2"}) == "2044-24-3"


# ── registration from PDF text (best-effort) ───────────────────────────────


def test_extract_registration_found():
    text = "La aeronave de matrícula CC-PHQ realizaba un vuelo local."
    assert dgaccl.extract_registration(text) == "CC-PHQ"


def test_extract_registration_none_for_foreign():
    text = "aeronave con matrícula extranjera (España), sin registro chileno"
    assert dgaccl.extract_registration(text) is None
    assert dgaccl.extract_registration("") is None


# ── live-fixture table parse ───────────────────────────────────────────────


def test_parse_year_page_rows(year2024_html):
    recs = dgaccl.parse_year_page(
        year2024_html, "https://www.dgac.gob.cl/informes-2024/")
    # 18 data rows in the trimmed fixture, all distinct case numbers.
    assert len(recs) == 18
    by_num = {r["case_number"]: r for r in recs}
    r = by_num["2044"]
    assert r["event_date"] == "2024-01-15"
    assert r["year"] == "2024"
    assert r["yy"] == "24"
    assert "TRUSH" in r["aircraft"].upper()  # site's own typo for 'THRUSH'
    assert "PANGUILEMU" in r["location"].upper()
    assert r["pdf_url"].lower().endswith("/informe-final-2044-24.pdf")
    assert r["report_kind"] == "Final"


def test_parse_year_page_filters_chrome(year2024_html):
    recs = dgaccl.parse_year_page(
        year2024_html, "https://www.dgac.gob.cl/informes-2024/")
    urls = [r["pdf_url"] for r in recs if r["pdf_url"]]
    # Header/footer chrome PDFs must not leak into any case row.
    assert not any("Presupuesto" in u for u in urls)
    assert not any("PoliticaPrivacidad" in u for u in urls)
    assert not any("Prohibidos" in u for u in urls)
    assert all("uploads" in u for u in urls)
    # A case row with no report PDF surfaces as pdf_url=None (e.g. 2058).
    assert any(r["pdf_url"] is None for r in recs)


def test_parse_year_page_preliminar_kind(year2024_html):
    recs = dgaccl.parse_year_page(
        year2024_html, "https://www.dgac.gob.cl/informes-2024/")
    by_num = {r["case_number"]: r for r in recs}
    # 2048 is published only as a 24-month preliminar.
    assert by_num["2048"]["report_kind"] == "Preliminar"
    assert "preliminar-24-meses" in by_num["2048"]["pdf_url"].lower()
