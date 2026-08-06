from nsib_ingest import text


def test_slugify_and_site_slug():
    assert text.slugify("Air Peace 5N-BQQ") == "air-peace-5n-bqq"
    assert text.make_site_slug("ERJ 145", "5N-BQQ", "Lagos") == "crash-erj-145-5n-bqq-lagos"
    assert text.make_site_slug(None, None, None) == "crash-nsib"


def test_clean_registration_nigerian():
    assert text.clean_registration("5N-BQQ") == "5N-BQQ"
    assert text.clean_registration("5NBQQ") == "5N-BQQ"
    # concatenated operator suffix in filename -> takes 3-letter mark only
    assert text.clean_registration("INTERIM-STATEMENT-01-5N-BRQOVERLAND.pdf") == "5N-BRQ"
    assert text.clean_registration("INTERIM-STATEMENT-01-5N-SHECAVERTON.pdf") == "5N-SHE"


def test_clean_registration_foreign():
    assert text.clean_registration("EK-74798") == "EK-74798"
    assert text.clean_registration("TC-LOL") == "TC-LOL"
    assert text.clean_registration("Preliminary_Report_Mattini_N580KR.pdf") == "N580KR"


def test_clean_registration_rejects_garbage():
    assert text.clean_registration("Limited") is None
    assert text.clean_registration("Helicopters") is None
    assert text.clean_registration("NIGERIA") is None
    assert text.clean_registration("") is None
    assert text.clean_registration(None) is None


def test_struct_ref_from_path():
    assert text.struct_ref_from_path(
        "https://nsib.gov.ng/wp-content/uploads/ninja-forms/3/AAL/2024/12/11/INTR/01.pdf"
    ) == "AAL/2024/12/11/INTR/01"
    assert text.struct_ref_from_path(
        "https://nsib.gov.ng/wp-content/uploads/ninja-forms/3/FAMSL/2024/12/06/INTR/01-.pdf"
    ) == "FAMSL/2024/12/06/INTR/01"
    assert text.struct_ref_from_path(
        "https://nsib.gov.ng/wp-content/uploads/2024/04/Preliminary-Report.pdf"
    ) is None


def test_make_case_id_priority():
    # 1) structured ref wins
    assert text.make_case_id("AAL/2024/12/11/INTR/01", "5N-JRT", "2025-12-14",
                             "Interim Statement") == "AAL/2024/12/11/INTR/01"
    # 2) reg + date, namespaced by type
    assert text.make_case_id(None, "5N-BQQ", "2025-09-14",
                             "Preliminary Report") == "NSIB-PRE-5N-BQQ-2025-09-14"
    assert text.make_case_id(None, "5N-BUQ", "2023-11-22",
                             "Interim Statement") == "NSIB-INT-5N-BUQ-2023-11-22"
    # 3) reg only / date only fallbacks
    assert text.make_case_id(None, "5N-XXX", None, "Final Report") == "NSIB-FIN-5N-XXX"
    assert text.make_case_id(None, None, "2023-06-15", None) == "NSIB-RPT-2023-06-15"
    # nothing intrinsic
    assert text.make_case_id(None, None, None, None) is None


def test_make_case_id_intrinsic_not_url_derived():
    # case_id must never be the post URL/slug
    cid = text.make_case_id(None, "5N-BQQ", "2025-09-14", "Preliminary Report")
    assert "http" not in cid and "/air-reports" not in cid
