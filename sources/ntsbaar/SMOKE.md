# ntsbaar — smoke test

**NTSB Aircraft Accident Reports (AAR)** — the Board's full written reports on
major US accidents, 1969–2021. US government work, public domain.

Not to be confused with [`sources-node/ntsb`](../../sources-node/ntsb), which
ingests the avall.zip bulk dump: that carries structured fields for every US
occurrence and no investigation text. This package fetches the narratives —
analysis, findings, probable cause.

## Where the index comes from

NTSB's own reports page builds its list in the browser (SharePoint); the file
listing returns 401 and the site's REST list contains no AAR entries. So the
index is read from Embry-Riddle Hunt Library's static year-by-year page, which
links straight to `ntsb.gov`, and every PDF is fetched from the NTSB itself —
one request to the library, 401 to the primary source.

That is a dependency on a third party for discovery, so `discover` raises when
the index yields no reports rather than reporting an empty run.

## Two kinds of document

Sampled 36 of the 401: 12 carry a real text layer (~200,000 characters on
average), 24 are scans that `pdftotext` reads as a few dozen characters of page
furniture. Reports from about 1995 on tend to have text; the 1970s and 80s are
images. `parse` therefore tries the text layer first and falls back to OCR —
the opposite balance to `rosap`, where OCR is the only path.

The collection is weighted to the scan era:

| adopted | reports |
|---|---|
| 1960s | 4 |
| 1970s | 177 |
| 1980s | 95 |
| 1990s | 48 |
| 2000s | 36 |
| 2010s | 34 |
| 2020s | 7 |

Budget roughly **1.1 GB** of PDFs on the first run, and OCR for about two
thirds of them.

## Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q                        # 21 tests, offline

python -m ntsbaar_ingest.cli discover --db ntsbaar.db      # -> 401
python -m ntsbaar_ingest.cli fetch    --db ntsbaar.db --pdf-dir pdfs
OCR_REMOTE=user@ocr-host python -m ntsbaar_ingest.cli parse --db ntsbaar.db
python -m ntsbaar_ingest.cli build    --db ntsbaar.db
```

## What is deliberately not extracted

`event_date`, `aircraft`, `registration` and `operator` are all left null.

The report number encodes the year the Board *adopted* the report, which is
typically a year or two after the accident — writing it as the event date
would misdate most of the collection. The real values are inside the prose and
need their own extractor built against the OCR text, not a regex written on a
guess. Registration additionally feeds the occurrence dedup key, where a wrong
value merges unrelated occurrences.

One note for whoever writes that extractor: US registrations before 1948 are
written `NC-93044` / `NC 16933`, not `N93044`. On the sibling CAB collection a
bare `N[0-9]{2,5}` pattern found registrations in 76 of 791 files; including
the `NC/NX/NR/NL` prefixes found them in 649.
