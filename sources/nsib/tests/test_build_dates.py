"""build() must date the rows NSIB leaves dateless.

dates.py had 21 green unit tests while pipeline.py referenced `dates.` without
importing it — every one of those tests passed and the build step would have
raised NameError on the first dateless row. A unit test on the parser does not
prove the parser is wired in, so these go through build() itself.
"""

from nsib_ingest import db, pipeline


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def _seed_parsed(conn, case_id, narrative, date_of_occurrence=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO nsib_reports "
        "(case_id, report_url, narrative_text, date_of_occurrence, report_type, "
        " status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (case_id, "https://nsib.gov.ng/r/" + case_id.replace("/", "-"), narrative,
         date_of_occurrence, "Final Report", db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def _event_date(conn, case_id):
    row = conn.execute(
        "SELECT event_date FROM nsib_accidents WHERE case_id=?", (case_id,)
    ).fetchone()
    return row["event_date"] if row else None


# A real narrative opening from production, long enough to clear the floor.
_NARR = (
    "The Nigerian Airspace Management Agency (NAMA) notified the Nigerian Safety "
    "Investigation Bureau (NSIB) of the occurrence on {when}. Investigators were "
    "dispatched to the site the same day and commenced the field investigation. "
    "The aircraft sustained substantial damage and there were no fatalities among "
    "the persons on board at the time of the occurrence."
)


def test_build_recovers_the_date_from_id_and_prose():
    conn = _conn()
    _seed_parsed(conn, "UNA/2021/11/17/F", _NARR.format(when="17 November 2021"))
    assert pipeline.build(conn) == 1
    assert _event_date(conn, "UNA/2021/11/17/F") == "2021-11-17"


def test_build_prefers_the_case_id_over_a_notification_date():
    # The Bureau was told the next day; the case number is the occurrence.
    conn = _conn()
    _seed_parsed(conn, "DANAL/2019/01/23/F", _NARR.format(when="the evening of 24th January, 2019"))
    pipeline.build(conn)
    assert _event_date(conn, "DANAL/2019/01/23/F") == "2019-01-23"


def test_build_uses_prose_when_the_id_carries_no_date():
    conn = _conn()
    _seed_parsed(conn, "NSIB-FIN-5N-PAN", _NARR.format(when="3 March 2021"))
    pipeline.build(conn)
    assert _event_date(conn, "NSIB-FIN-5N-PAN") == "2021-03-03"


def test_build_leaves_a_row_dateless_rather_than_guessing():
    # Ambiguous id (9 Oct or 10 Sep) and no date in the prose.
    conn = _conn()
    narrative = (
        "The crew reported a bird strike during the take-off roll and rejected "
        "the take-off. The aircraft was inspected and returned to service after "
        "the affected components had been replaced by the maintenance organisation."
    )
    _seed_parsed(conn, "GAL/2020/09/10/F", narrative)
    pipeline.build(conn)
    assert _event_date(conn, "GAL/2020/09/10/F") is None


def test_build_does_not_touch_a_date_the_listing_already_supplied():
    conn = _conn()
    _seed_parsed(
        conn, "ATL/2023/07/10/F", _NARR.format(when="10 July 2023"),
        date_of_occurrence="2023-07-10",
    )
    pipeline.build(conn)
    assert _event_date(conn, "ATL/2023/07/10/F") == "2023-07-10"
