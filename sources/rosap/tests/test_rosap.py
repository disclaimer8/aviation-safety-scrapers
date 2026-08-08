"""Listing and title parsing. Fixtures are real rows from the collection."""
import pytest

from rosap_ingest import rosap


class TestTheTitleCarriesTheFacts:
    def test_it_splits_operator_location_and_date(self):
        out = rosap.parse_title(
            "Investigation of Aircraft Accident: SLICK AIRWAYS: "
            "BOSTON, MASSACHUSETTS: 1964-03-10")
        assert out == {"operator": "SLICK AIRWAYS",
                       "location": "BOSTON, MASSACHUSETTS",
                       "event_date": "1964-03-10", "doc_kind": "report"}

    def test_a_location_may_contain_commas(self):
        out = rosap.parse_title(
            "Investigation of Aircraft Accident: PAN AMERICAN AIRWAYS: "
            "JOHN F. KENNEDY INTERNATIONAL AIRPORT JAMAICA, NEW YORK: 1964-04-07")
        assert out["location"] == "JOHN F. KENNEDY INTERNATIONAL AIRPORT JAMAICA, NEW YORK"
        assert out["event_date"] == "1964-04-07"

    def test_an_operator_may_contain_a_comma(self):
        out = rosap.parse_title(
            "Investigation of Aircraft Accident: TRANS WORLD AIRLINES, EASTERN "
            "AIRLINES: CARMEL, NEW YORK: 1965-12-04")
        assert out["operator"] == "TRANS WORLD AIRLINES, EASTERN AIRLINES"

    def test_a_title_of_another_shape_yields_nothing(self):
        assert rosap.parse_title("Annual Report of the Board 1958") == {
            "operator": None, "location": None, "event_date": None,
            "doc_kind": None}

    def test_empty_in_nothing_out(self):
        assert rosap.parse_title("")["event_date"] is None
        assert rosap.parse_title(None)["event_date"] is None


class TestSupplementsAreMarked:
    @pytest.mark.parametrize("suffix,kind", [
        ("[Amendment]", "Amendment"),
        ("[Hearing Notice]", "Hearing Notice"),
        ("[Letter from Leon Tanguay]", "Letter from Leon Tanguay"),
        ("[Memo: John M. Chamberlain]", "Memo: John M. Chamberlain"),
    ])
    def test_the_bracketed_label_becomes_the_doc_kind(self, suffix, kind):
        out = rosap.parse_title(
            "Investigation of Aircraft Accident: AMERICAN AIRLINES AND NAVY "
            f"BEECHCRAFT: COLUMBUS, OHIO: 1954-07-27 {suffix}")
        assert out["doc_kind"] == kind
        assert out["event_date"] == "1954-07-27"

    def test_five_supplements_share_one_accident(self):
        # Real: this 1954 accident has five attached documents. Reading them
        # as accidents would invent four that never happened.
        base = ("Investigation of Aircraft Accident: AMERICAN AIRLINES AND NAVY "
                "BEECHCRAFT: COLUMBUS, OHIO: 1954-07-27 ")
        kinds = {rosap.parse_title(base + s)["doc_kind"] for s in
                 ("[Carnahan: Hearing Report]", "[Representation Memo]",
                  "[Hearing Notice]", "[CAB Hearing Notice]",
                  "[Letter from W. K. Andrews]")}
        assert len(kinds) == 5
        assert "report" not in kinds


class TestCountryIsDerivedNotAssumed:
    @pytest.mark.parametrize("location", [
        "BOSTON, MASSACHUSETTS", "SALT LAKE CITY, UTAH",
        "NEAR BAINBRIDGE, MD", "LOOKOUT ROCK, WEST VIRGINIA",
        "NEW YORK INTERNATIONAL AIRPORT, NEW YORK",
    ])
    def test_a_us_state_gives_us(self, location):
        assert rosap.country_of(location) == "US"

    def test_the_state_may_follow_a_space_instead_of_a_comma(self):
        # These titles write it both ways, and six real locations —
        # "DETROIT MICHIGAN", "GLENDALE CALIFORNIA" — have no comma.
        assert rosap.country_of("DETROIT MICHIGAN") == "US"
        assert rosap.country_of("GLENDALE CALIFORNIA") == "US"

    def test_a_water_body_named_after_a_state_is_still_us(self):
        # "LAKE MICHIGAN" matches on the state name rather than on a place in
        # the state, and the answer is right anyway: a 1965 United Airlines
        # accident in Lake Michigan happened in US waters. Worth stating
        # outright, because the match is looser than the reasoning.
        assert rosap.country_of("LAKE MICHIGAN") == "US"

    @pytest.mark.parametrize("location", [
        "SHANNON, IRELAND", "MAYADINE, SYRIA", "GANDER, NEWFOUNDLAND",
        "DITCHING IN THE NORTH ATLANTIC", "CHICAGO MIDWAY AIRPORT",
    ])
    def test_anything_else_stays_unset(self, location):
        # 67 of the 791 locations are foreign, oceanic, or a US city with no
        # state. The collection follows US operators worldwide, so stamping US
        # is the same mistake that put ZA on foreign reports in sacaa — an
        # unset country is honest, a wrong one is not.
        assert rosap.country_of(location) is None

    def test_no_location_no_country(self):
        assert rosap.country_of(None) is None
        assert rosap.country_of("") is None


class TestListingPairing:
    """Each title must come from its own anchor, never from position."""

    def test_link_and_title_stay_together(self):
        pairs = [
            ("/view/dot/33723", "Investigation of Aircraft Accident: JAPAN "
                                "AIRLINES: SAN FRANCISCO, CALIFORNIA: 1965-12-25"),
            ("/view/dot/33722", "Investigation of Aircraft Accident: UNITED "
                                "AIRLINES: SALT LAKE CITY, UTAH: 1965-11-11"),
        ]
        rows = rosap.parse_listing(pairs)
        assert [r["pid"] for r in rows] == ["33723", "33722"]
        assert rows[0]["operator"] == "JAPAN AIRLINES"
        assert rows[1]["operator"] == "UNITED AIRLINES"

    def test_an_extra_link_cannot_shift_the_titles(self):
        # The failure this guards against, from the first run: pids were
        # collected with one sweep and titles with another, then zipped. A
        # page with one unexpected link shifted 13 rows onto a neighbour's
        # operator and date. Pairing per anchor makes that impossible.
        pairs = [
            ("/view/dot/99999", ""),  # an item link with no title text
            ("/view/dot/33723", "Investigation of Aircraft Accident: JAPAN "
                                "AIRLINES: SAN FRANCISCO, CALIFORNIA: 1965-12-25"),
        ]
        rows = {r["pid"]: r for r in rosap.parse_listing(pairs)}
        assert rows["33723"]["operator"] == "JAPAN AIRLINES"
        assert rows["99999"]["operator"] is None

    def test_the_same_item_twice_yields_one_row(self):
        pairs = [("/view/dot/33723", "x"), ("/view/dot/33723", "x")]
        assert len(rosap.parse_listing(pairs)) == 1

    def test_non_item_anchors_are_ignored(self):
        assert rosap.parse_listing([("/help", "Help"), ("", "")]) == []

    def test_the_pdf_url_is_derived_from_the_pid(self):
        rows = rosap.parse_listing([("/view/dot/33704", "x")])
        assert rows[0]["pdf_url"].endswith("/view/dot/33704/dot_33704_DS1.pdf")


class TestListingUrls:
    def test_the_first_page_carries_no_offset(self):
        assert "start=" not in rosap.listing_url(0)

    def test_later_pages_do(self):
        assert rosap.listing_url(40).endswith("&start=40")
