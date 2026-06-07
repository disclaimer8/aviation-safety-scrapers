from gcaa_ingest import gcaa


# ── OData shape handling (verbose d.results, d-list, value, bare) ─────────────

def test_odata_results_verbose(items_payload):
    rows = gcaa.odata_results(items_payload)
    assert isinstance(rows, list)
    assert len(rows) == 8


def test_odata_results_tolerates_shapes():
    assert gcaa.odata_results({"d": {"results": [1, 2]}}) == [1, 2]
    assert gcaa.odata_results({"d": [1, 2]}) == [1, 2]
    assert gcaa.odata_results({"value": [3]}) == [3]
    assert gcaa.odata_results([9]) == [9]
    assert gcaa.odata_results(None) == []
    assert gcaa.odata_results({}) == []


def test_attachment_list_handles_wrap_and_missing():
    assert gcaa.attachment_list({"AttachmentFiles": {"results": [1]}}) == [1]
    assert gcaa.attachment_list({"AttachmentFiles": []}) == []
    assert gcaa.attachment_list({}) == []


# ── case_id normalization + null fallback ────────────────────────────────────

def test_case_id_normalizes_reference():
    assert gcaa.case_id_from_reference("AIFN/0007/2013") == "aifn-0007-2013"
    assert gcaa.case_id_from_reference("AIFN0009/2020") == "aifn0009-2020"


def test_case_id_null_falls_back_to_item_id():
    assert gcaa.case_id_from_reference(None, 901) == "gcaa-901"
    assert gcaa.case_id_from_reference("", 12) == "gcaa-12"


def test_case_id_null_ref_and_no_id_is_none():
    assert gcaa.case_id_from_reference(None, None) is None


# ── attachment URL encoding (spaces/commas → percent-encoded, absolute) ───────

def test_attachment_url_percent_encodes_and_absolutizes():
    srv = ("/en/departments/airaccidentinvestigation/Lists/"
           "Incidents Investigation Reports/Attachments/1/"
           "2013-2013 - Summary Report B737-800, A6-FDE.pdf")
    url = gcaa.attachment_url(srv)
    assert url.startswith("https://www.gcaa.gov.ae/")
    assert " " not in url
    assert "%20" in url
    assert "," not in url or "%2C" in url
    # path separators preserved
    assert "/Lists/" in url


def test_attachment_url_passthrough_and_none():
    assert gcaa.attachment_url(None) is None
    assert gcaa.attachment_url("https://x/y") == "https://x/y"


def test_pick_attachment_prefers_final():
    atts = [
        {"FileName": "Preliminary Report.pdf", "ServerRelativeUrl": "/p.pdf"},
        {"FileName": "Final Report.pdf", "ServerRelativeUrl": "/f.pdf"},
    ]
    name, srv = gcaa.pick_attachment(atts)
    assert srv == "/f.pdf"


def test_pick_attachment_empty():
    assert gcaa.pick_attachment([]) == (None, None)


# ── item field mapping (SharePoint _x0020_ internal names) ───────────────────

def test_parse_item_maps_sharepoint_fields(items):
    m = gcaa.parse_item(items[0])  # Id 1, AIFN/0007/2013, A6-FDE
    assert m["case_id"] == "aifn-0007-2013"
    assert m["reference_no"] == "AIFN/0007/2013"
    assert m["registration"] == "A6-FDE"
    assert m["aircraft"] == "Boeing 737-800"
    assert m["report_status"] == "Final"
    assert m["occurrence_category"] == "Incident"
    assert m["date"] == "2013-04-05"
    assert m["year"] == "2013"
    assert "Dubai" in m["location"]
    assert m["has_attachment"] is True
    assert m["pdf_url"].startswith("https://www.gcaa.gov.ae/")
    assert "%20" in m["pdf_url"]


def test_parse_item_foreign_registration(items):
    foreign = next(i for i in items if i.get("Registration_x0020_No") == "UP-A3003")
    m = gcaa.parse_item(foreign)
    assert m["registration"] == "UP-A3003"


def test_parse_item_null_registration(items):
    m = next(gcaa.parse_item(i) for i in items if i.get("Id") == 900)
    assert m["registration"] is None
    assert m["case_id"] == "aifn-0099-2020"  # ref present, reg null
    assert m["has_attachment"] is True


def test_parse_item_null_reference_uses_item_id(items):
    m = next(gcaa.parse_item(i) for i in items if i.get("Id") == 901)
    assert m["reference_no"] is None
    assert m["case_id"] == "gcaa-901"


def test_parse_item_stub_has_no_attachment(items):
    m = next(gcaa.parse_item(i) for i in items if i.get("Id") == 136)
    assert m["has_attachment"] is False
    assert m["pdf_url"] is None


def test_parse_item_multi_attachment_picks_final(items):
    m = next(gcaa.parse_item(i) for i in items if i.get("Id") == 902)
    assert m["has_attachment"] is True
    assert "Final" in (m["filename"] or "")
