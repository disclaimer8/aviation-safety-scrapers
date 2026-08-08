# tests/test_text.py
"""Tests for text extraction helpers."""
from ntsbcarol_ingest.text import cap_narrative, extract_probable_cause, MAX_NARRATIVE


def test_extract_probable_cause_standard():
    sample = """
Some narrative content here about the investigation.

PROBABLE CAUSE

The National Transportation Safety Board determines the probable cause
of this accident was the pilot's failure to maintain adequate airspeed
while conducting a visual approach to the runway.

FINDINGS

1. The weather was VFR at the time.
"""
    pc = extract_probable_cause(sample)
    assert "pilot" in pc.lower()
    assert "FINDINGS" not in pc


def test_extract_probable_cause_mixed_case():
    long_pc = "The cause was ice accumulation on the wings due to the flight crew's failure to activate the anti-ice system prior to entering known icing conditions."
    sample = f"Analysis text. Probable Cause\n\n{long_pc}\n\nSAFETY RECOMMENDATIONS"
    pc = extract_probable_cause(sample)
    assert "ice" in pc.lower()


def test_extract_probable_cause_not_found():
    assert extract_probable_cause("") == ""
    assert extract_probable_cause("some text without PC section") == ""
    assert extract_probable_cause(None) == ""


def test_cap_narrative_within_limit():
    text = "a" * 5000
    assert len(cap_narrative(text)) == 5000


def test_cap_narrative_exceeds_limit():
    text = "b" * (MAX_NARRATIVE + 1000)
    capped = cap_narrative(text)
    assert len(capped) == MAX_NARRATIVE


def test_cap_narrative_empty():
    assert cap_narrative("") == ""
    assert cap_narrative(None) == ""
