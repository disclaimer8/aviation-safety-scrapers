"""Occurrence-date recovery for OVV.

205 of the 386 projected rows had no event_date, because the only place the
scraper looked was the page title — and OVV titles are descriptive ("Tail
strike during take-off, Boeing 737-800, Rotterdam Airport"), not dated.

Two other places carry a date, and neither is trustworthy on its own:

  * the investigation page has a labelled "Investigation start date" field.
    Calibrated against 14 rows whose occurrence date is already known, it was
    exact for 12 — and wrong by +39 and +73 days for the other two, where it
    is the day the investigation was opened rather than the day of the
    occurrence.

  * the report text states the occurrence date, but in half a dozen layouts
    and mixed Dutch/English, and the same page footer carries a publication
    date ("The Hague, may 24, 2007") right next to it.

So neither is used alone. The start date is accepted only when the very same
date also appears in the report text — the report states what happened and
when, and it does not state when a file was opened. On the calibration sample
that rule rejected both wrong dates and kept three of the four right ones:
no false positives, one conservative miss.
"""
import pytest

from ovv_ingest.dates import corroborated_date, parse_start_date


META = ('Download report Source: ANP Investigation start date 18 July 2014 '
        'Publish date report 13 October 2015 Status Closed')


class TestReadingTheFieldOffThePage:
    def test_it_reads_the_investigation_start_date(self):
        assert parse_start_date(META) == "2014-07-18"

    def test_the_publish_date_is_not_taken(self):
        # It sits in the same block, eight words later.
        assert parse_start_date(META) != "2015-10-13"

    def test_a_page_without_the_field_yields_nothing(self):
        assert parse_start_date("Status Closed Download report") is None

    def test_html_tags_do_not_matter(self):
        html = ('<dl><dt>Investigation start date</dt>'
                '<dd>3 April 2019</dd></dl>')
        assert parse_start_date(html) == "2019-04-03"

    def test_empty_in_nothing_out(self):
        assert parse_start_date("") is None
        assert parse_start_date(None) is None


class TestCorroboration:
    def test_a_date_the_report_also_states_is_accepted(self):
        narrative = ("Loss of steering on a slippery taxiway of the Easyjet "
                     "Boeing B737-700 at Amsterdam Airport Schiphol on "
                     "22 December 2003. The Hague, (investigation number 2003133)")
        assert corroborated_date("2003-12-22", narrative) == "2003-12-22"

    def test_the_dutch_spelling_counts_as_the_same_date(self):
        # Half the corpus is Dutch: "22 december 1999, nabij Etten-Leur".
        narrative = "EINDRAPPORT 1999142 Botsing in de lucht 22 december 1999, nabij Etten-Leur"
        assert corroborated_date("1999-12-22", narrative) == "1999-12-22"

    def test_the_american_order_counts_too(self):
        narrative = "Amsterdam Airport Schiphol, June 29, 2005 The Hague, may 24, 2007"
        assert corroborated_date("2005-06-29", narrative) == "2005-06-29"

    def test_a_zero_padded_day_counts(self):
        assert corroborated_date("2008-02-08", "op 08 februari 2008 te Lelystad") == "2008-02-08"

    def test_a_date_the_report_never_mentions_is_refused(self):
        # Both real: the investigation was opened 39 and 73 days after the
        # occurrence, and the report says so nowhere.
        narrative = ("Collision during taxi, Diamond DA-40, Lelystad. "
                     "The occurrence took place on 9 November 2013.")
        assert corroborated_date("2013-12-18", narrative) is None

    def test_no_start_date_means_no_answer(self):
        assert corroborated_date(None, "anything at all") is None

    def test_no_narrative_means_no_answer(self):
        assert corroborated_date("2013-12-18", "") is None
        assert corroborated_date("2013-12-18", None) is None


class TestItWillNotInventDates:
    @pytest.mark.parametrize("bad", ["", None, "not-a-date", "2013-13-45"])
    def test_a_malformed_start_date_is_refused(self, bad):
        assert corroborated_date(bad, "22 December 2003 somewhere") is None

    def test_a_near_miss_is_not_a_match(self):
        # One day out is exactly the error mode being guarded against, so it
        # must not be smoothed over.
        assert corroborated_date("2003-12-23", "on 22 December 2003 at Schiphol") is None
