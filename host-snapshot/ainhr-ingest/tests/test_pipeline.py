# tests/test_pipeline.py
import os

from ainhr_ingest import ainhr, db, pipeline

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fix(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _routes_for(make_client):
    cat = _fix("ainhr_category.html")
    cessna = _fix("ainhr_post_cessna182.html")
    nopdf = _fix("ainhr_post_nopdf.html")
    multi = _fix("ainhr_post_multi_pdf.html")

    class R:
        def __init__(self, body):
            self.content = body.encode("utf-8")
            self.status_code = 200

        def raise_for_status(self):
            pass

    routes = {ainhr.INDEX_URL: R(cat)}
    # map every discovered post URL; serve the cessna body for known slug,
    # nopdf body for the airbus slug, multi for the krizevci slug, else a
    # minimal generic post page with no pdf.
    for slug in ainhr.iter_post_slugs(cat):
        url = ainhr.post_url(slug)
        if slug == "nesreca-zrakoplova-tipa-cessna-182-brocanac-slunj-29-05-2022":
            routes[url] = R(cessna)
        elif slug.startswith("ozbiljna-nezgoda-zrakoplova-airbus-220-300"):
            routes[url] = R(nopdf)
        elif slug.startswith("ozljedivanje-vratima"):
            routes[url] = R(multi)
        else:
            routes[url] = R("<h1>" + slug + "</h1>")
    return make_client(routes)


def test_discover_inserts_all_slugs(make_client, tmp_path, monkeypatch):
    monkeypatch.setattr(ainhr, "DELAY", 0)
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    client = _routes_for(make_client)
    n = pipeline.discover(conn, client)
    total = conn.execute("SELECT COUNT(*) FROM ainhr_reports").fetchone()[0]
    assert n == total
    assert 55 <= total <= 75


def test_discover_idempotent(make_client, tmp_path, monkeypatch):
    monkeypatch.setattr(ainhr, "DELAY", 0)
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    pipeline.discover(conn, _routes_for(make_client))
    first = conn.execute("SELECT COUNT(*) FROM ainhr_reports").fetchone()[0]
    second = pipeline.discover(conn, _routes_for(make_client))
    assert second == 0
    assert conn.execute("SELECT COUNT(*) FROM ainhr_reports").fetchone()[0] == first


def test_discover_sets_hr_pdf_and_metadata(make_client, tmp_path, monkeypatch):
    monkeypatch.setattr(ainhr, "DELAY", 0)
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    pipeline.discover(conn, _routes_for(make_client))
    row = conn.execute(
        "SELECT * FROM ainhr_reports WHERE case_id=?",
        ("nesreca-zrakoplova-tipa-cessna-182-brocanac-slunj-29-05-2022",),
    ).fetchone()
    assert row["lang"] == "hr"
    assert row["pdf_url"].endswith("zavrsno_izvjesce.pdf")
    assert row["date_of_occurrence"] == "2022-05-29"
    assert row["status"] == db.STATUS_NEW
    assert row["pdf_url_en"] is not None


def test_parse_tiers_and_build_skip(tmp_path):
    """parse() classifies tier; build() skips scanned/none and short narratives."""
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    ts = db.now_ms()
    # Three fetched rows with no pdf_path → narrative '' → tier 'none' → skipped.
    for cid in ("a-1-2020", "b-2-2020", "c-3-2020"):
        conn.execute(
            "INSERT INTO ainhr_reports (case_id, status, pdf_path, date_of_occurrence, "
            "event_class, discovered_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (cid, db.STATUS_FETCHED, None, "2020-01-01", "Accident", ts, ts),
        )
    conn.commit()
    pipeline.parse(conn)
    tiers = {r["case_id"]: r["source_tier"] for r in
             conn.execute("SELECT case_id, source_tier FROM ainhr_reports").fetchall()}
    assert all(t == "none" for t in tiers.values())
    built = pipeline.build(conn)
    assert built == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM ainhr_reports WHERE status=?", (db.STATUS_SKIPPED,)
    ).fetchone()[0] == 3


def test_build_emits_accident_row(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    ts = db.now_ms()
    narrative = "Z" * 1200
    conn.execute(
        "INSERT INTO ainhr_reports (case_id, status, narrative_text, source_tier, "
        "date_of_occurrence, event_class, registration, pdf_url, report_url, "
        "discovered_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("cessna-zadar-26-08-2013", db.STATUS_PARSED, narrative, "pdf",
         "2013-08-26", "Accident", "9A-DLZ",
         "https://ain.hr/x.pdf", "https://ain.hr/istrage/cessna-zadar-26-08-2013/",
         ts, ts),
    )
    conn.commit()
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM ainhr_accidents").fetchone()
    assert acc["country"] == "HR"
    assert acc["site_slug"] == "cessna-zadar-26-08-2013"
    assert acc["registration"] == "9A-DLZ"
    assert acc["report_type"] == "Accident"
    assert acc["source_url"] == "https://ain.hr/x.pdf"
