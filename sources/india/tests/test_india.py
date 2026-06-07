from india_ingest import india


# ── index ─────────────────────────────────────────────────────────────────────

def test_parse_index_skips_preliminary(index_html):
    rows = india.parse_index(index_html)
    assert len(rows) > 150
    assert not any("prelim" in r["pdf_url"].lower() for r in rows)
    assert not any("interim" in r["pdf_url"].lower() for r in rows)


def test_parse_index_fields(index_html):
    rows = india.parse_index(index_html)
    by_path = {r["rel_path"]: r for r in rows}
    r = by_path["Reports/2025/Accident/VT-SLH_Final Investigation Report.pdf"]
    assert r["year"] == "2025"
    assert r["report_kind"] == "Accident"
    assert r["registration"] == "VT-SLH"
    # absolute, URL-encoded
    assert r["pdf_url"].startswith("https://aaib.gov.in/Reports/2025/")
    assert "%20" in r["pdf_url"]


def test_parse_index_normalizes_kind_dirs(index_html):
    rows = india.parse_index(index_html)
    kinds = {r["report_kind"] for r in rows}
    # 'SeriousIncident' / 'Serious Incident' collapse; 'INCIDENT' → Incident
    assert "Serious Incident" in kinds
    assert "SeriousIncident" not in kinds
    assert "INCIDENT" not in kinds
    # lowercase 'accident' dirs normalize too
    assert "accident" not in kinds


def test_parse_index_registration_variants():
    html = '''
    <a href="Reports/2019/Accident/VT_RGF_Sultanpur_Telangana.pdf">x</a>
    <a href="Reports/2019/Accident/Accepted Report  VT-TEH.pdf">x</a>
    <a href="Reports/2019/Accident/NoRegHere_Multi.pdf">x</a>
    '''
    rows = india.parse_index(html)
    regs = [r["registration"] for r in rows]
    assert regs == ["VT-RGF", "VT-TEH", None]


# ── case_id ───────────────────────────────────────────────────────────────────

def test_make_case_id_with_registration():
    assert india.make_case_id("2022", "VT-SLH", "x.pdf") == "2022_VT-SLH"


def test_make_case_id_collision_suffix():
    taken = {"2022_VT-SLH"}
    assert india.make_case_id("2022", "VT-SLH", "x.pdf", taken=taken) == "2022_VT-SLH_2"


def test_make_case_id_no_registration_uses_filename():
    cid = india.make_case_id("2019", None, "Reports/2019/Accident/NCR840_KLM875 incident.pdf")
    assert cid.startswith("2019_ncr840-klm875")


# ── PDF metadata: new title-phrase era ────────────────────────────────────────

def test_parse_pdf_meta_new_format(title_new):
    m = india.parse_pdf_meta(title_new)
    assert m["registration"] == "VT-SLH"
    assert m["event_date"] == "2022-05-01"
    assert m["aircraft"] == "B-737-800"
    assert m["operator"] == "Spice Jet"
    assert m["location"] and "Durgapur" in m["location"]


# ── PDF metadata: old labeled-table era ───────────────────────────────────────

def test_parse_pdf_meta_old_format(title_old):
    m = india.parse_pdf_meta(title_old)
    assert m["registration"] == "VT-DAR"
    assert m["event_date"] == "2014-11-28"
    assert m["aircraft"] == "PC-12/45"
    assert m["operator"] and "Deccan" in m["operator"]
    assert m["location"] and "GUWAHATI" in m["location"].upper()


def test_parse_pdf_meta_empty():
    m = india.parse_pdf_meta("")
    assert all(v is None for v in m.values())


def test_parse_pdf_meta_2012_to_format():
    head = ("Final Investigation Report on Accident to\n"
            "Pawan Hans Helicopters Limited (PHHL)\n"
            "Bell 407 Helicopter VT-PHH on 30-12-2012\n"
            "at Katra Valley, Jammu & Kashmir\n")
    m = india.parse_pdf_meta(head)
    assert m["registration"] == "VT-PHH"
    assert m["event_date"] == "2012-12-30"
    assert m["operator"] == "Pawan Hans Helicopters Limited (PHHL)"
    assert m["aircraft"] == "Bell 407"
    assert m["location"] and "Katra" in m["location"]
