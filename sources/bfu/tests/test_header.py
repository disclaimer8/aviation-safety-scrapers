# tests/test_header.py
"""
Tests for the BFU German Identifikation header parser (parse_header).

Three real BFU pdftotext fixtures are used:
  bfu1.txt  – Learjet 35 A, Unfall, 16.01.2023, Rendsburg      (BFU23-0022-1X)
  bfu2.txt  – A321+B737-8, Schwere Störung, 23.10.2023, Stuttgart (BFU23-1010-EX)
  bfu3.txt  – TL 232 CONDOR plus UL, Unfall, 19.03.2024, Hohebach (BFU24-0173-3X)

Key calibration finding: BFU Identifikation blocks do NOT contain a "Kennzeichen:"
(registration) field in any examined report (2011–2024).  BFU anonymises aircraft
identities; registration is always None from BFU PDFs.
"""
from pathlib import Path
import pytest
from bfu_ingest.header import parse_header

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── Fixture-based tests ───────────────────────────────────────────────────────

class TestBfu1LearjetRendsburg:
    """bfu1: Learjet 35A, Unfall, 16.01.2023, Rendsburg, BFU23-0022-1X"""

    def setup_method(self):
        self.h = parse_header(_load("bfu1.txt"))

    def test_event_class(self):
        assert self.h["event_class"] == "Accident"

    def test_date_iso(self):
        assert self.h["date_iso"] == "2023-01-16"

    def test_location(self):
        assert self.h["location"] == "Rendsburg"

    def test_aircraft_contains_model(self):
        assert self.h["aircraft"] is not None
        assert "learjet" in self.h["aircraft"].lower()

    def test_case_id(self):
        assert self.h["case_id"] == "BFU23-0022-1X"

    def test_registration_none(self):
        # BFU Identifikation blocks never include Kennzeichen/registration
        assert self.h["registration"] is None


class TestBfu2A321StuttgartSchwereStorung:
    """bfu2: Airbus A321 + Boeing 737, Schwere Störung, 23.10.2023, Stuttgart, BFU23-1010-EX"""

    def setup_method(self):
        self.h = parse_header(_load("bfu2.txt"))

    def test_event_class_serious_incident(self):
        assert self.h["event_class"] == "Serious incident"

    def test_date_iso(self):
        assert self.h["date_iso"] == "2023-10-23"

    def test_location(self):
        assert self.h["location"] == "Stuttgart"

    def test_aircraft_contains_first_aircraft(self):
        # Multi-aircraft report; parser takes first aircraft (A321)
        assert self.h["aircraft"] is not None
        ac = self.h["aircraft"].lower()
        # Either Airbus or A321 should appear
        assert "airbus" in ac or "a321" in ac

    def test_case_id(self):
        assert self.h["case_id"] == "BFU23-1010-EX"

    def test_registration_none(self):
        assert self.h["registration"] is None


class TestBfu3TL232UltraLight:
    """bfu3: TL 232 CONDOR plus UL, Unfall, 19.03.2024, near Hohebach, BFU24-0173-3X"""

    def setup_method(self):
        self.h = parse_header(_load("bfu3.txt"))

    def test_event_class(self):
        assert self.h["event_class"] == "Accident"

    def test_date_iso(self):
        assert self.h["date_iso"] == "2024-03-19"

    def test_location_contains_hohebach(self):
        assert self.h["location"] is not None
        assert "hohebach" in self.h["location"].lower()

    def test_aircraft_contains_model(self):
        assert self.h["aircraft"] is not None
        ac = self.h["aircraft"].lower()
        assert "tl" in ac or "condor" in ac or "ultralight" in ac

    def test_case_id(self):
        assert self.h["case_id"] == "BFU24-0173-3X"

    def test_registration_none(self):
        assert self.h["registration"] is None


# ── Inline-text unit tests (no fixtures) ─────────────────────────────────────

class TestParseHeaderInlineValues:
    """Tests against synthetic strings with inline "Label: value" layout."""

    INLINE_TEXT = """Identifikation
Art des Ereignisses: Unfall
Datum: 16.01.2023
Ort: Rendsburg
Luftfahrzeug: Flugzeug
Hersteller: Learjet Corporation
Muster: Learjet 35 A
Aktenzeichen: BFU23-0022-1X
"""

    def test_event_class_inline(self):
        h = parse_header(self.INLINE_TEXT)
        assert h["event_class"] == "Accident"

    def test_date_iso_inline(self):
        h = parse_header(self.INLINE_TEXT)
        assert h["date_iso"] == "2023-01-16"

    def test_location_inline(self):
        h = parse_header(self.INLINE_TEXT)
        assert h["location"] == "Rendsburg"

    def test_aircraft_inline(self):
        h = parse_header(self.INLINE_TEXT)
        assert h["aircraft"] == "Learjet Corporation Learjet 35 A"

    def test_case_id_inline(self):
        h = parse_header(self.INLINE_TEXT)
        assert h["case_id"] == "BFU23-0022-1X"


class TestParseHeaderNextLineValues:
    """Tests against synthetic strings with values on next non-empty line (2-column layout)."""

    NEXT_LINE_TEXT = """Identifikation
Art des Ereignisses:

Unfall

Datum:

19.03.2024

Ort:

Nahe Sonderlandeplatz Hohebach

Hersteller:

TL Ultralight

Muster:

TL 232 CONDOR plus

Aktenzeichen:

BFU24-0173-3X
"""

    def test_event_class_next_line(self):
        h = parse_header(self.NEXT_LINE_TEXT)
        assert h["event_class"] == "Accident"

    def test_date_iso_next_line(self):
        h = parse_header(self.NEXT_LINE_TEXT)
        assert h["date_iso"] == "2024-03-19"

    def test_location_next_line(self):
        h = parse_header(self.NEXT_LINE_TEXT)
        assert "hohebach" in (h["location"] or "").lower()

    def test_aircraft_next_line(self):
        h = parse_header(self.NEXT_LINE_TEXT)
        assert h["aircraft"] == "TL Ultralight TL 232 CONDOR plus"

    def test_case_id_next_line(self):
        h = parse_header(self.NEXT_LINE_TEXT)
        assert h["case_id"] == "BFU24-0173-3X"


class TestEventClassMapping:
    """Tests for the event-class German→English mapping."""

    def _make(self, event_val: str) -> str:
        return f"Identifikation\nArt des Ereignisses: {event_val}\nAktenzeichen: BFU99-0000-0X\n"

    def test_unfall_maps_to_accident(self):
        h = parse_header(self._make("Unfall"))
        assert h["event_class"] == "Accident"

    def test_schwere_storung_maps_to_serious_incident(self):
        h = parse_header(self._make("Schwere Störung"))
        assert h["event_class"] == "Serious incident"

    def test_schwere_storung_ascii_fallback(self):
        h = parse_header(self._make("schwere storung"))
        assert h["event_class"] == "Serious incident"

    def test_storung_maps_to_incident(self):
        h = parse_header(self._make("Störung"))
        assert h["event_class"] == "Incident"

    def test_unknown_passes_through(self):
        h = parse_header(self._make("Havarie"))
        assert h["event_class"] == "Havarie"


class TestDateParsing:
    """Tests for numeric and German-month-name date formats."""

    def _make(self, date_val: str) -> str:
        return f"Identifikation\nDatum:\n\n{date_val}\n\nAktenzeichen: BFU99-0000-0X\n"

    def test_numeric_date(self):
        h = parse_header(self._make("16.01.2023"))
        assert h["date_iso"] == "2023-01-16"

    def test_german_month_name(self):
        h = parse_header(self._make("15. April 2018"))
        assert h["date_iso"] == "2018-04-15"

    def test_german_month_name_januar(self):
        h = parse_header(self._make("18. Januar 2011"))
        assert h["date_iso"] == "2011-01-18"

    def test_german_month_maerz(self):
        h = parse_header(self._make("03. März 2022"))
        assert h["date_iso"] == "2022-03-03"

    def test_unparseable_returns_none(self):
        h = parse_header(self._make("unbekannt"))
        assert h["date_iso"] is None


class TestEdgeCases:
    """Edge cases: empty input, None input, missing fields."""

    def test_empty_string_returns_all_none(self):
        h = parse_header("")
        assert set(h.keys()) == {"event_class", "aircraft", "registration", "date_iso", "location", "case_id"}
        assert all(v is None for v in h.values())

    def test_none_input_returns_all_none(self):
        h = parse_header(None)
        assert set(h.keys()) == {"event_class", "aircraft", "registration", "date_iso", "location", "case_id"}
        assert all(v is None for v in h.values())

    def test_missing_muster_uses_hersteller_only(self):
        text = "Identifikation\nHersteller: SomeManufacturer\nAktenzeichen: BFU99-0000-0X\n"
        h = parse_header(text)
        assert h["aircraft"] == "SomeManufacturer"

    def test_missing_hersteller_uses_muster_only(self):
        text = "Identifikation\nMuster: FancyType 123\nAktenzeichen: BFU99-0000-0X\n"
        h = parse_header(text)
        assert h["aircraft"] == "FancyType 123"

    def test_registration_always_none(self):
        # Even if Kennzeichen somehow appears (hypothetical), verify handling
        text = "Identifikation\nKennzeichen: D-XXXX\nAktenzeichen: BFU99-0000-0X\n"
        h = parse_header(text)
        # registration is always None from BFU parser; Kennzeichen is not extracted
        assert h["registration"] is None

    def test_older_combined_hersteller_muster_label(self):
        """Older BFU reports use 'Hersteller / Muster: Piper / PA-34-220T Seneca V'"""
        text = "Identifikation\nHersteller / Muster:\n\nPiper / PA-34-220T Seneca V\n\nAktenzeichen: BFU 3X001-11\n"
        h = parse_header(text)
        assert h["aircraft"] is not None
        assert "piper" in h["aircraft"].lower() or "pa-34" in h["aircraft"].lower()

    def test_multi_aircraft_uses_first(self):
        """Multi-aircraft report (Luftfahrzeug 1/2) — first aircraft is used."""
        text = """Identifikation
Art des Ereignisses: Schwere Störung
Datum: 23.10.2023
Ort: Stuttgart
Luftfahrzeug 1: Flugzeug
Hersteller: Airbus
Muster: A321-231
Luftfahrzeug 2: Flugzeug
Hersteller: Boeing
Muster: 737-8AS
Aktenzeichen: BFU23-1010-EX
"""
        h = parse_header(text)
        assert h["aircraft"] is not None
        assert "airbus" in h["aircraft"].lower() or "a321" in h["aircraft"].lower()
