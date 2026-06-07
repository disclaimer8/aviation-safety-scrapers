# Contributing

Thanks for your interest. This repo is a collection of **independent** scrapers
— the goal is that each source can be understood, run, tested, and rewritten on
its own, without a shared framework to learn first.

Sources live in three trees by language:

- **`sources/<code>/`** — Python packages (the bulk; national SIAs). Anatomy below.
- **`sources-node/<code>/`** — Node.js packages: `src/{parse,scrape,db,cli}.js`
  + `test/` (jest, offline fixtures) + `package.json`. Same four-verb contract.
- **`sources-go/`** — Go projects with their own `go.mod`.

Whatever the language, the contract is the same: one source per directory,
self-contained, offline tests, never commit harvested data. The Python anatomy
below is the reference shape; mirror its spirit in Node/Go.

## Anatomy of a source

```
sources/<code>/
  <code>_ingest/
    __init__.py
    cli.py            # argparse entrypoint: discover | fetch | parse | build
    <code>.py         # source-specific scraping (index + report pages)
    pdf.py / text.py  # extraction helpers (only where the source is PDF-based)
    db.py             # SQLite schema + upserts
    pipeline.py       # glue that wires the four verbs together
  tests/
    fixtures/         # committed offline HTML/text samples (NO live network in tests)
    test_*.py
  deploy/
    <code>-cycle.service
    <code>-cycle.timer
    run-cycle.sh
  pyproject.toml
  SMOKE.md
```

## The four-verb contract

Every CLI exposes the same subcommands, each idempotent and resumable:

| Verb | Responsibility |
|------|----------------|
| `discover` | Walk the authority's index → upsert one row per report (URL + metadata). |
| `fetch` | Download the report artifact (PDF or HTML) for rows missing it. |
| `parse` | Extract narrative / probable-cause / structured fields from the artifact. |
| `build` | Normalise into the canonical output rows. |

Sources that are HTML-only (no PDFs) may fold `parse` into `build`; that's fine
— keep the verbs that make sense for the source and document it in `SMOKE.md`.

## Adding a new authority

1. Copy the closest existing source as a template (a plain-`httpx` one like
   `sources/tsb`, or a browser-based one like `sources/cenipa` if the target
   has bot protection).
2. Rename the package directory and `pyproject.toml` `name` to `<code>_ingest`.
3. Implement `discover`/`fetch`/`parse`/`build` against the new site.
4. Save **offline fixtures** of the index and a report page into `tests/fixtures/`
   and write tests against them — no test may hit the network.
5. Record a real run in `SMOKE.md`: the CLI you ran, row counts, and any
   source-specific quirks.
6. Add a row to the catalogue table in `README.md`.

## Ground rules

- **Be polite.** Single-threaded, paced requests, an identifiable User-Agent.
  These are public safety archives run on modest budgets — don't degrade them.
- **Honour `robots.txt`** and each authority's terms of use.
- **Never commit harvested data** — `*.db`, `pdfs/`, and logs are gitignored.
- **Tests stay offline.** Commit fixtures, not network calls.
- Keep each source self-contained; resist factoring shared code across sources
  unless it's genuinely generic and stable.

## Style

- Python ≥ 3.11, standard library first; `httpx` for HTTP.
- Prefer small, single-purpose modules over a monolith.
- Match the surrounding source's existing conventions.
