from jst_ingest import jst


# ── expediente → case_id (zero-pad join, '/YY' suffix) ────────────────────────

def test_case_id_strips_year_suffix_and_pads():
    assert jst.case_id_from_expediente("41546464/26") == "41546464"
    assert jst.case_id_from_expediente("123456/19") == "00123456"
    assert jst.case_id_from_expediente("12016494") == "12016494"


def test_case_id_handles_empty():
    assert jst.case_id_from_expediente(None) is None
    assert jst.case_id_from_expediente("") is None
    assert jst.case_id_from_expediente("/26") is None


def test_case_id_already_eight_digits_unchanged():
    # an event nro joins to the same 8-digit manifest key
    assert jst.case_id_from_expediente("00934360/26") == "00934360"


# ── document preference ISO > IB > INC > IPROV > IP ───────────────────────────

def test_pick_doc_prefers_iso_over_ib():
    docs = [
        {"tipo": "IB", "path": "x/IB.pdf"},
        {"tipo": "ISO", "path": "x/ISO.pdf"},
    ]
    path, tipo = jst.pick_doc(docs)
    assert tipo == "ISO"
    assert path == "x/ISO.pdf"


def test_pick_doc_full_order():
    order = ["IP", "IPROV", "INC", "IB", "ISO"]
    docs = [{"tipo": t, "path": f"x/{t}.pdf"} for t in order]
    _, tipo = jst.pick_doc(docs)
    assert tipo == "ISO"
    # drop ISO → IB wins
    _, tipo = jst.pick_doc(docs[:-1])
    assert tipo == "IB"
    # only IP left
    _, tipo = jst.pick_doc([{"tipo": "IP", "path": "x/IP.pdf"}])
    assert tipo == "IP"


def test_pick_doc_unknown_tipo_ranks_last():
    docs = [{"tipo": "RSOA", "path": "x/r.pdf"}, {"tipo": "IB", "path": "x/ib.pdf"}]
    _, tipo = jst.pick_doc(docs)
    assert tipo == "IB"


def test_pick_doc_empty():
    assert jst.pick_doc([]) == (None, None)
    assert jst.pick_doc(None) == (None, None)


def test_pdf_url():
    assert jst.pdf_url("AE/2022/x/ISO-1.pdf") == (
        "https://so.jst.gob.ar/static/informes/AE/2022/x/ISO-1.pdf")
    assert jst.pdf_url(None) is None


# ── event field mapping (registration / fatalities from vehiculos) ────────────

def test_parse_event_maps_vehiculos_fields(events_page):
    e = events_page[0]  # BOEING B-737-800 LV-FVN, Aviación Comercial
    m = jst.parse_event(e)
    assert m["case_id"] == "41546464"
    assert m["nro_expediente"] == "41546464/26"
    assert m["registration"] == "LV-FVN"
    assert m["aircraft"] == "BOEING B-737-800"
    assert m["operator"] == "Aviación Comercial"
    assert m["occurrence_type"] == "Accidente"
    assert m["date"] == "2026-04-23"
    assert m["status"] == "En Curso"
    assert "Santiago" in m["location"]
    assert m["summary"] and len(m["summary"]) > 50  # reseña paragraph


def test_parse_event_fatalities_sum_across_vehiculos():
    e = {"nro_expediente": "1/26", "vehiculos": [
        {"matricula": "LV-A", "victimas_fatales": 2},
        {"matricula": "LV-B", "victimas_fatales": 3},
    ]}
    assert jst.parse_event(e)["fatalities"] == 5


def test_parse_event_resena_accent_key(events_page):
    # every live event in the page carries a non-empty 'reseña'
    parsed = [jst.parse_event(e) for e in events_page]
    assert sum(1 for p in parsed if p["summary"]) >= len(parsed) * 0.8


def test_parse_event_no_vehiculos():
    m = jst.parse_event({"nro_expediente": "1/26", "vehiculos": []})
    assert m["registration"] is None
    assert m["aircraft"] is None
    assert m["fatalities"] is None
