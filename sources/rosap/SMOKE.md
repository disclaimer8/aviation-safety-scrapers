# rosap — smoke test

US DOT National Transportation Library, ROSA P repository, collection
**"Investigations of Aircraft Accidents 1934-1965"** — 791 digitized Civil
Aeronautics Board accident reports. DOI [10.21949/1530839](https://doi.org/10.21949/1530839).
US government work, public domain.

## What is different about this source

**It only answers a real browser.** Akamai fronts `rosap.ntl.bts.gov` and
returns 403 to curl, to httpx with any User-Agent, and to Playwright's own
`page.request`. Only browser navigation gets through, so this package depends
on `patchright` rather than `httpx` and takes PDFs through Chrome's download
path. Run it under `xvfb-run` on a headless box.

**Every PDF is an image-only scan.** `pdftotext` returns zero characters from
all of them — checked across the 1930s, 1950s and 1960s. `parse` goes straight
to OCR; there is no text layer to try first. OCR quality is good: a sample
page yielded the operator, type, registration, weather and probable cause with
only cosmetic errors ("Wandsor" for Windsor).

**The listing title carries the facts.** 782 of 791 match
`Investigation of Aircraft Accident: <OPERATOR>: <LOCATION>: <YYYY-MM-DD>`.
The other 9 are supplements attached to a case (`[Amendment]`,
`[Hearing Notice]`, `[Letter from …]`); five of those belong to one 1954
accident, so `build` marks them skipped rather than inventing four accidents.

**Country is derived, never assumed.** The collection follows US *operators*
worldwide — Shannon, Gander, Damascus and mid-Atlantic ditchings are in it.
724 of 791 locations resolve to US; the remaining 67 are left unset rather
than stamped.

## Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]' && patchright install chromium
pytest -q                     # 46 tests, offline

xvfb-run -a python -m rosap_ingest.cli discover --db rosap.db --max-pages 2
xvfb-run -a python -m rosap_ingest.cli fetch    --db rosap.db --pdf-dir pdfs
python -m rosap_ingest.cli parse --db rosap.db          # OCR; no browser needed
python -m rosap_ingest.cli build --db rosap.db
```

`parse` and `build` deliberately never launch a browser, so a re-parse works
on a box with no display.

## Expected shape

```
discovered: 791          40 listing pages at 20 per page
fetched:    791          ~400 MB, about two pages per report
parsed:     791          all via OCR
built:      782          the 9 supplements are skipped
```

```sql
SELECT COUNT(*), MIN(event_date), MAX(event_date) FROM rosap_accidents;
-- 782 | 1934-08-07 | 1965-12-25
SELECT SUBSTR(event_date,1,3)||'0s', COUNT(*) FROM rosap_accidents GROUP BY 1;
-- 1930s 81 | 1940s 375 | 1950s 242 | 1960s 84
```
