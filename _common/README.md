# `_common/` — canonical modules, vendored into the source packages

Three modules are the same in every scraper that needs them:

| canon | vendored as | what it is |
|---|---|---|
| `pdf.py` | `<code>_ingest/pdf.py` | `pdftotext` extraction, local/remote OCR fallback, the `MIN_NARRATIVE` / `SCANNED_MAX` thresholds |
| `text.py` | `<code>_ingest/text.py` | `strip_html`, `slugify`, `make_site_slug` |
| `http.py` | `<code>_ingest/httpc.py` | the `httpx` client factory and its retry policy |

## Why vendoring and not a shared package

Every source is deployed as its own directory with its own virtualenv, and the
repository README promises that a source can be copied away and run on its own.
A real import dependency would break that promise and add one more package to
install into every environment — for a benefit a generator and a test already
give us.

So each package keeps a real file. It is simply **generated**, and drift is a
test failure rather than something you find out about years later.

## Using it

```bash
python -m _common.sync            # rewrite the vendored copies
python -m _common.sync --check    # report drift and exit 1 (what CI runs)
```

Change a canonical file, run the sync, commit both. Editing a vendored copy
directly is the one thing that does not work: the next sync overwrites it and
the drift test fails in the meantime.

## Adding a package to the list

`VENDORED` in `sync.py` is an explicit opt-in map, not a glob over `sources/`.
Not every package's `pdf.py` shares this lineage — several carry per-source
logic — and syncing the canon over one of those would silently delete real
code. Read the package's variant first, confirm it is the canonical one, then
add it.

Where a package has extra source-specific helpers in the same file, the canon
lands under a different name and the package re-exports it. `nsib` works this
way: its `text.py` owns Nigerian registration parsing on top of the shared
three functions, so the canon is vendored as `_textbase.py` and `text.py`
starts with `from ._textbase import make_site_slug, slugify, strip_html`.

## What the tests cover

`_common/tests/` is not only unit tests for the canon:

- `test_sync.py` — the template renderer, and the drift gate itself (including
  a test that deliberately dirties a vendored file to prove the gate fails).
- `test_adoption.py` — imports each opted-in package for real and asserts its
  CLI actually builds a retrying client, and that the vendored copy still
  retries. A canonical module nobody imports reads like a fix while every
  package keeps its old behaviour.
- `test_http.py` — the retry policy: which statuses are retried, which are
  not, the backoff, and that a persistent 5xx is *returned* rather than raised
  so one bad report cannot end a whole crawl.
