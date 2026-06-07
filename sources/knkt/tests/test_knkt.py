from knkt_ingest import knkt


# ── Keterangan parsing ────────────────────────────────────────────────────────

def test_parse_keterangan_full():
    m = knkt.parse_keterangan(
        "Loss of Control-Inflight, Adam Air (Boeing 737-400/PK-KKW); "
        "Selat Makassar / KNKT.07.01.01.04"
    )
    assert m["occurrence_type"] == "Loss of Control-Inflight"
    assert m["operator"] == "Adam Air"
    assert m["aircraft"] == "Boeing 737-400"
    assert m["registration"] == "PK-KKW"
    assert m["location"] == "Selat Makassar"
    assert m["case_id"] == "KNKT.07.01.01.04"


def test_parse_keterangan_old_case_format():
    m = knkt.parse_keterangan("Hard Landing, X (Y-12/PK-ABC); Z / KNKT/07.01/08.01.36")
    assert m["case_id"] == "KNKT.07.01.08.01.36"


def test_parse_keterangan_no_case_number():
    m = knkt.parse_keterangan(
        "Runway Excursion, Aviastar Mandiri (Casa 212-200/PK-BRM); Sangata, Kalimantan"
    )
    assert m["case_id"] is None
    assert m["registration"] == "PK-BRM"
    assert m["location"] == "Sangata, Kalimantan"


def test_parse_keterangan_foreign_reg():
    m = knkt.parse_keterangan("Fire, AirCo (A320/4L-IFE); Jakarta / KNKT.18.05.14.04")
    assert m["registration"] == "4L-IFE"


def test_parse_keterangan_empty():
    m = knkt.parse_keterangan(None)
    assert all(v is None for v in m.values())


def test_parse_keterangan_live_rows(listing_rows):
    parsed = [knkt.parse_keterangan(r.get("Keterangan")) for r in listing_rows]
    assert sum(1 for p in parsed if p["registration"]) >= len(parsed) * 0.7


# ── report pick + years + case_id ─────────────────────────────────────────────

def test_pick_report_prefers_final():
    f, kind = knkt.pick_report({"Final_Report": "a.pdf", "Preliminary_Report": "b.pdf"})
    assert (f, kind) == ("a.pdf", "Final")
    f, kind = knkt.pick_report({"Preliminary_Report": "b.pdf"})
    assert (f, kind) == ("b.pdf", "Preliminary")
    assert knkt.pick_report({}) == (None, None)


def test_candidate_years_occurrence_first_then_case_year():
    # the verified trap row: 2008 occurrence, KNKT.22.* case → folder /2022/
    years = knkt.candidate_years(
        "2008-03-10", "KNKT.22.07.11.04-Preliminary-Report.pdf", "KNKT.22.07.11.04"
    )
    assert years == ["2008", "2022"]


def test_candidate_pdf_urls_encoded():
    urls = knkt.candidate_pdf_urls("2007-01-01", "PK-KKW Final Report.pdf", None)
    assert urls == [
        "https://knkt.go.id/Repo/Files/Laporan/Penerbangan/2007/PK-KKW%20Final%20Report.pdf"
    ]


def test_canonical_case_id():
    assert knkt.canonical_case_id("KNKT.22.07.11.04") == "KNKT.22.07.11.04"
    assert knkt.canonical_case_id("KNKT/07.01/08.01.36") == "KNKT.07.01.08.01.36"
    assert knkt.canonical_case_id("KNKT 18.02.06.04") == "KNKT.18.02.06.04"
    assert knkt.canonical_case_id(None) is None


def test_make_case_id_fallbacks():
    assert knkt.make_case_id("KNKT.07.01.01.04", "PK-KKW", "2007-01-01") == "KNKT.07.01.01.04"
    assert knkt.make_case_id(None, "PK-KKW", "2007-01-01") == "PK-KKW-2007-01-01"
    taken = {"PK-KKW-2007-01-01"}
    assert knkt.make_case_id(None, "PK-KKW", "2007-01-01", taken=taken) == "PK-KKW-2007-01-01-2"
