# tests/test_text.py
from ainhr_ingest import text


def test_fold_diacritics():
    assert text.fold_diacritics("Broćanac žđšč") == "Brocanac zdsc"


def test_slugify_folds_and_lowercases():
    assert text.slugify("Broćanac, Slunj") == "brocanac-slunj"


def test_make_site_slug_normalises_case_id():
    cid = "nesreca-zrakoplova-tipa-cessna-182-brocanac-slunj-29-05-2022"
    assert text.make_site_slug(cid) == cid


def test_make_site_slug_strips_junk():
    assert text.make_site_slug("Foo--Bar 2022") == "foo-bar-2022"


def test_strip_html():
    assert text.strip_html("<p>hello &amp; bye</p>") == "hello & bye"
