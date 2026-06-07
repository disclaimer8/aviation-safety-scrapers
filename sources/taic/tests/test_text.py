from taic_ingest.text import make_site_slug, slugify, strip_html


def test_strip_html():
    assert strip_html("<b>Bell&nbsp;206</b>  x") == "Bell 206 x"
    assert strip_html(None) == ""


def test_slugify():
    assert slugify("Robinson R44, ZK-HTB!") == "robinson-r44-zk-htb"
    assert slugify("") == ""


def test_make_site_slug():
    assert make_site_slug("Bell 206B", "ZK-HDI", "Mt Stevenson") == \
        "crash-bell-206b-zk-hdi-mt-stevenson"
    assert make_site_slug(None, None, None) == "crash-taic"
