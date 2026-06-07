# tests/conftest.py
import pytest

from gpiaaf_ingest import db


# ─── DB fixture ───────────────────────────────────────────────────────────────

@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


# ─── Fake browser (mirrors GpiaafBrowser's public API, no Playwright) ─────────

class FakeBrowser:
    """Stand-in for GpiaafBrowser. NO network, NO browser.

    year_urls:   list returned by harvest_year_urls().
    year_rows:   dict year_url -> list of parsed report rows (get_year_rows()).
    pdfs:        dict doc_url -> (s3_url, pdf_id) returned by capture_pdf();
                 the bytes written to dest are ``pdf_bytes`` (default a stub).
                 A doc_url mapped to an Exception instance is raised.
    """

    def __init__(self, year_urls=None, year_rows=None, pdfs=None,
                 pdf_bytes=b"%PDF-1.4 fake report body"):
        self.year_urls = year_urls or []
        self.year_rows = year_rows or {}
        self.pdfs = pdfs or {}
        self.pdf_bytes = pdf_bytes
        self.captured = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def harvest_year_urls(self):
        return list(self.year_urls)

    def get_year_rows(self, year_url, year=None):
        return [dict(r) for r in self.year_rows.get(year_url, [])]

    def capture_pdf(self, doc_url, dest_path):
        self.captured.append(doc_url)
        mapped = self.pdfs.get(doc_url)
        if isinstance(mapped, Exception):
            raise mapped
        with open(dest_path, "wb") as f:
            f.write(self.pdf_bytes)
        if mapped is None:
            return (f"https://s3/{doc_url[-6:]}.pdf", None)
        return mapped


@pytest.fixture
def make_browser():
    return lambda **kw: FakeBrowser(**kw)
