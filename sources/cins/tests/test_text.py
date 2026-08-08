from cins_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Krilo &amp; motor</p>  <b>kvar</b>") == "Krilo & motor kvar"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Cessna 172G") == "cessna-172g"
    assert slugify("  YU-DOT!! ") == "yu-dot"


def test_slugify_deaccents_serbian():
    assert slugify("Niška Banja") == "niska-banja"
    assert slugify("Đorđe Šuma Žaba") == "djordje-suma-zaba"
    assert slugify("Pančevo") == "pancevo"


def test_make_site_slug_combines_parts():
    assert make_site_slug("Cessna 172G", "YU-DOT", "Beograd") == "crash-cessna-172g-yu-dot-beograd"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "YU-A299", None) == "crash-yu-a299"
    assert make_site_slug(None, None, None) == "crash-cins"
