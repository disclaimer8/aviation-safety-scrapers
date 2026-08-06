"""Every fixture here is a real (case_id, narrative) pair from production.

The Node twin (server/src/__tests__/nsibDateRecovery.test.js) asserts the same
behaviours against the same strings; if one changes, change both.
"""

from nsib_ingest.dates import from_case_id, from_narrative, recover_event_date


class TestCaseId:
    def test_third_component_over_12_proves_ymd(self):
        assert from_case_id("UNA/2021/11/17/F") == {"year": 2021, "a": 11, "b": 17, "order": "ymd"}
        assert from_case_id("NCAT/2022/12/31/F") == {"year": 2022, "a": 12, "b": 31, "order": "ymd"}

    def test_second_component_over_12_proves_ydm(self):
        assert from_case_id("NPF/2022/26/01/F") == {"year": 2022, "a": 26, "b": 1, "order": "ydm"}

    def test_both_under_13_is_ambiguous_not_a_default(self):
        assert from_case_id("ATL/2023/07/10/F") == {"year": 2023, "a": 7, "b": 10, "order": "ambiguous"}

    def test_the_other_scheme_carries_no_date(self):
        assert from_case_id("NSIB-FIN-5N-PAN") is None
        assert from_case_id("NSIB-FIN-FK-NAF") is None
        assert from_case_id("") is None
        assert from_case_id(None) is None


class TestNarrative:
    def test_plain_day_month_year(self):
        assert from_narrative(
            "NAMA notified the Accident Investigation Bureau (AIB) of the incident on 17 November 2021."
        ) == "2021-11-17"

    def test_the_comma_nsib_writes(self):
        assert from_narrative("NAMA notified NSIB of the occurrence on 31 December, 2022.") == "2022-12-31"

    def test_ordinals_are_the_house_style(self):
        # Each of these left a row dateless until the regex stopped demanding
        # whitespace straight after the day number.
        assert from_narrative(
            "(AIB-N) was notified of the incident by Gyro Air Limited (GAL) on 10th September, 2020."
        ) == "2020-09-10"
        assert from_narrative("On the 25th November, 1998 at about 0730 hours") == "1998-11-25"
        assert from_narrative("on 6th March 2018. The Bureau only became") == "2018-03-06"

    def test_the_nth_of_month_form(self):
        assert from_narrative("via a phone call of the occurrence on the 3rd of August 2019.") == "2019-08-03"
        assert from_narrative("at about 22:15hrs on 6th of July, 2015, investigators") == "2015-07-06"

    def test_american_order(self):
        assert from_narrative("NSIB was notified of the occurrence on July 10, 2023 and dispatched") == "2023-07-10"

    def test_no_date_is_none_not_a_guess(self):
        assert from_narrative("The aircraft was on a scheduled flight when the crew reported a fault.") is None
        assert from_narrative("") is None
        assert from_narrative(None) is None


class TestCombined:
    def test_agreement_on_an_unambiguous_id(self):
        assert recover_event_date(
            "UNA/2021/11/17/F",
            "NAMA notified the Bureau of the incident on 17 November 2021.",
        ) == ("2021-11-17", "narrative+id")

    def test_prose_resolves_an_ambiguous_id(self):
        # 07/10 is 10 July or 7 October. The report says 10 July.
        assert recover_event_date(
            "ATL/2023/07/10/F",
            "NAMA notified NSIB of the occurrence on 10 July 2023.",
        ) == ("2023-07-10", "narrative+id")

    def test_prose_overturns_the_obvious_reading_of_an_inverted_id(self):
        assert recover_event_date(
            "NPF/2022/26/01/F",
            "NAMA notified NSIB of the occurrence on 26 January 2022.",
        ) == ("2022-01-26", "narrative+id")

    def test_notification_date_does_not_override_the_id(self):
        # The Bureau was told the next day; the case number is the occurrence.
        assert recover_event_date(
            "DANAL/2019/01/23/F",
            "AIB was notified of the serious incident by the Operator in the evening of 24th January, 2019.",
        ) == ("2019-01-23", "id")

    def test_a_two_week_notification_gap_is_still_the_id(self):
        assert recover_event_date(
            "SEA/2019/11/19/F",
            "AIB was notified of the occurrence by the operator on the 2nd December, 2019. "
            "On 19th November, 2019, at about 08:45",
        ) == ("2019-11-19", "id")

    def test_unambiguous_id_with_no_prose_date(self):
        assert recover_event_date("NCAT/2022/12/31/F", "The aircraft sustained damage.") == ("2022-12-31", "id")

    def test_ambiguous_id_with_no_prose_stays_dateless(self):
        assert recover_event_date("GAL/2020/09/10/F", "The crew reported a bird strike.") == (None, None)

    def test_prose_alone_carries_the_rows_whose_id_has_no_date(self):
        assert recover_event_date(
            "NSIB-FIN-5N-PAN",
            "NSIB was notified of the occurrence on 3 March 2021 and began the investigation",
        ) == ("2021-03-03", "narrative")

    def test_a_real_disagreement_is_reported_not_resolved(self):
        # WESTLINK/2014/08/11 reads as 11 Aug or 8 Nov; the report opens 31 July.
        assert recover_event_date(
            "WESTLINK/2014/08/11/F",
            "Westlink Airlines Limited final-report accident On 31st July, 2014, 5N-BGZ, a PA 23-250",
        ) == (None, "conflict")

    def test_impossible_and_implausible_dates_are_not_dates(self):
        assert recover_event_date("XXX/2022/02/31/F", "")[0] is None
        assert recover_event_date("XXX/1899/05/20/F", "")[0] is None
        assert recover_event_date("XXX/2099/05/20/F", "")[0] is None

    def test_nothing_in_nothing_out(self):
        assert recover_event_date("", "") == (None, None)
        assert recover_event_date(None, None) == (None, None)
