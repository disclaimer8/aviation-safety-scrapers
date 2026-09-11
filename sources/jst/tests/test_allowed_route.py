"""jst reaches its data through the door that is open.

discover() used to enumerate events from intranet.jst.gob.ar, whose robots.txt
is a blanket `Disallow: /` — a host named intranet telling crawlers to stay
out. Everything it needed is on the public host: Index.json lists every
published report, and each report prints its own metadata on page one.

The fixtures are the front matter of five real reports, one per layout the
JST actually emits. Coverage measured over 25 live reports when this landed:
date 100%, registration 96%, location 96%, aircraft 84%, occurrence 80%.
"""
import pathlib

import pytest

from jst_ingest import jst, pipeline

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _fx(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── the manifest ─────────────────────────────────────────────────────────────

MANIFEST = {
    "00934360": [{"tipo": "IP", "path": "AE/2026/010426-00934360/IP-00934360-26.pdf"}],
    "00201220": [{"tipo": "IB", "path": "AE/2022/021922-00201220/IB-00201220-22.pdf"},
                 {"tipo": "ISO", "path": "AE/2022/021922-00201220/ISO-00201220-22.pdf"}],
    "00252353": [{"tipo": "IB", "path": "AU/2023/122823-00252353/IB-00252353-23.pdf"}],
    "nointranet1": [{"tipo": "ISO", "path": "AE/2005/060205-nointranet/ISO-nointranet-05.pdf"}],
}


class TestOnlyAviationIsTaken:
    def test_other_transport_modes_are_left_alone(self):
        codes = {case_id for case_id, _, _ in jst.aviation_docs(MANIFEST)}
        # AU/ is the JST's road file; it is not ours.
        assert "00252353" not in codes
        assert {"00934360", "00201220", "nointranet1"} <= codes

    def test_an_empty_manifest_yields_nothing_rather_than_raising(self):
        assert list(jst.aviation_docs({})) == []
        assert list(jst.aviation_docs(None)) == []


class TestTheDateComesOffThePath:
    @pytest.mark.parametrize("path,want", [
        ("AE/2026/010426-00934360/IP-00934360-26.pdf", "2026-01-04"),
        ("AE/2022/021922-00201220/IB-00201220-22.pdf", "2022-02-19"),
        ("AE/2023/123023-00404649/ISO-00404649-23.pdf", "2023-12-30"),
        # The case-id half varies — old entries say "nointranet" or a serial —
        # but the MMDDYY prefix is there, which is why only that is read.
        ("AE/2005/060205-nointranet/ISO-nointranet-05.pdf", "2005-06-02"),
        ("AE/2010/122910-1/ISO-1-10.pdf", "2010-12-29"),
    ])
    def test_mmddyy_is_read_not_ddmmyy(self, path, want):
        assert jst.date_from_path(path) == want

    @pytest.mark.parametrize("path", [
        "AU/2023/122823-00252353/IB.pdf",   # not aviation
        "AE/2023/999923-1/x.pdf",           # month 99
        "AE/2023/nodate-1/x.pdf",
        "",
        None,
    ])
    def test_an_unreadable_path_gives_none_rather_than_a_guess(self, path):
        assert jst.date_from_path(path) is None


class TestDocumentPreference:
    def test_the_final_report_wins_over_the_basic_one(self):
        docs = [("IB", "a.pdf"), ("ISO", "b.pdf"), ("IP", "c.pdf")]
        assert jst.pick_manifest_doc(docs) == ("ISO", "b.pdf")

    def test_an_unknown_tipo_ranks_last_rather_than_first(self):
        assert jst.pick_manifest_doc([("MYSTERY", "a.pdf"), ("IP", "b.pdf")]) == ("IP", "b.pdf")

    def test_no_documents_is_none(self):
        assert jst.pick_manifest_doc([]) is None


# ── the report's own front matter ────────────────────────────────────────────

class TestTheLabelledLayout:
    """IP / IPROV / INC / ISO: Suceso: / Título: / Fecha y hora del suceso:"""

    def test_it_reads_every_field(self):
        got = jst.parse_report_header(_fx("header_labelled_ip.txt"))
        assert got["registration"] == "LV-YYL"
        assert got["date_of_occurrence"] == "2026-01-04"
        assert got["aircraft"]
        assert got["location"]


class TestTheCommaSeparatedTitle:
    """INT titles put a comma where IP puts a full stop:
    '…de grupo motor, Embraer ERJ-190, matrícula LVCMB, …'"""

    def test_the_aircraft_is_still_found(self):
        got = jst.parse_report_header(_fx("header_int_comma.txt"))
        assert got["aircraft"] == "Embraer ERJ-190"
        # pdftotext folds "LV-CMB" across a line break into "LVCMB".
        assert got["registration"] == "LV-CMB"
        assert got["date_of_occurrence"] == "2022-01-12"


class TestTheTableLayout:
    """INFORME BÁSICO is a form; pdftotext emits label and value as
    consecutive lines with no colon."""

    def test_date_place_and_mark_are_recovered(self):
        got = jst.parse_report_header(_fx("header_table_ib.txt"))
        assert got["date_of_occurrence"] == "2024-01-21"
        assert got["registration"] == "LV-MGT"
        assert "Morón" in (got["location"] or "")
        assert (got["occurrence_type"] or "").lower() == "accidente"

    def test_the_aircraft_is_left_none_rather_than_guessed(self):
        # The wider columns are shuffled by pdftotext — `Marca` ends up six
        # lines from `Piper`. A guess here would be worse than a gap.
        assert jst.parse_report_header(_fx("header_table_ib.txt"))["aircraft"] is None


class TestTheAdrepLayout:
    """The short IP/INT forms are an ADREP table with English field names."""

    def test_manufacturer_place_and_class_are_read(self):
        got = jst.parse_report_header(_fx("header_adrep_ip.txt"))
        assert got["aircraft"] == "ROBINSON"
        assert got["registration"] == "LV-GUI"
        assert "Cipolletti" in (got["location"] or "")
        assert got["occurrence_type"] == "Accident"
        assert got["date_of_occurrence"] == "2026-06-17"


class TestTheOldFreeFormCover:
    def test_what_can_be_read_is_read(self):
        got = jst.parse_report_header(_fx("header_freeform_old.txt"))
        assert got["registration"] == "LV-NDQ"
        assert got["date_of_occurrence"] == "2020-02-29"


class TestRegistrationShapes:
    @pytest.mark.parametrize("text,want", [
        ("matrícula LV-IUG,", "LV-IUG"),
        ("matrícula LVCMB,", "LV-CMB"),          # dash eaten by the line break
        ("matrícula LV-S114,", "LV-S114"),       # experimental / ultralight
        ("Indicativo\nLV-GUI\n", "LV-GUI"),
    ])
    def test_they_all_normalise_to_one_form(self, text, want):
        assert jst.parse_report_header(text)["registration"] == want

    def test_a_report_with_no_mark_gives_none(self):
        assert jst.parse_report_header("no marks here at all")["registration"] is None


# ── discover, end to end against a fake client ───────────────────────────────

class _ManifestClient:
    def __init__(self, manifest=MANIFEST):
        self._manifest = manifest
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)

        class _R:
            status_code = 200

            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                return self._manifest
        return _R()


def _conn():
    from jst_ingest import db
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


class TestDiscoverUsesOnlyTheAllowedHost:
    def test_it_never_touches_the_intranet(self):
        client = _ManifestClient()
        pipeline.discover(_conn(), client)
        assert client.urls, "discover made no request at all"
        assert not any("intranet" in u for u in client.urls), (
            "discover reached the host whose robots.txt refuses everything"
        )
        assert all(u.startswith(jst.PDF_BASE.rsplit("/", 2)[0]) or "so.jst.gob.ar" in u
                   for u in client.urls)

    def test_it_inserts_one_row_per_aviation_case(self):
        conn = _conn()
        n = pipeline.discover(conn, _ManifestClient())
        assert n == 3          # the AU/ road case is excluded
        rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM jst_reports")}
        assert set(rows) == {"00934360", "00201220", "nointranet1"}
        assert rows["00201220"]["doc_tipo"] == "ISO"       # preferred over IB
        assert rows["00934360"]["date_of_occurrence"] == "2026-01-04"

    def test_it_is_idempotent(self):
        conn = _conn()
        assert pipeline.discover(conn, _ManifestClient()) == 3
        assert pipeline.discover(conn, _ManifestClient()) == 0

    def test_an_empty_manifest_is_a_failure_not_an_empty_source(self):
        with pytest.raises(RuntimeError, match="empty"):
            pipeline.discover(_conn(), _ManifestClient(manifest={}))

    def test_a_manifest_with_no_aviation_is_a_failure_too(self):
        only_road = {"1": [{"tipo": "IB", "path": "AU/2023/010123-1/IB.pdf"}]}
        with pytest.raises(RuntimeError, match="no AE/"):
            pipeline.discover(_conn(), _ManifestClient(manifest=only_road))
