# caaccn — mainland-China accident reports

Builds `caaccn.db` (table `caaccn_accidents`), the source database the
FlightFinder narrative pipeline reads with
`build-source-narratives.js --source caaccn`.

Nine reports: CAAC regional bureaus (HD, ZN, XJ, XN), MEM special-major
investigations (Yichun 2010, Baotou 2004), and the CAAC-central MU5735
preliminary and progress notices. All 官方文件 — PRC Copyright Law Art. 5 puts
official documents outside copyright.

## This is a curated set, not a scraper

There is no listing to walk. Each report was located by hand, its text
extracted to `docs/<key>.txt`, and its metadata recorded in `src/meta.js`.
Adding a report means adding both. The tests check the two stay in step in
**both** directions — a record with no text file, and a text file no record
claims.

    npm ci
    npm test      # jest, no network, no database
    npm run build # writes ./caaccn.db

`CAACCN_DOCS` and `CAACCN_DB` override the input and output paths.

## What this package fixed on arrival

On the host this lived in two directories. `~/china-ingest` held the builder
and the texts; `~/caaccn-ingest` held only a sync script, which looked for the
database at `~/caaccn-ingest/caaccn.db` — a path nothing wrote to. Neither
half ran on its own, and the arrangement worked only because somebody
remembered the trick. Paths now resolve from the package.

## probable_cause is null, deliberately

Every row has a narrative of 1,771–14,620 characters, which scores 30 of the
50 prod needs to mark a page indexable. The remaining 20 come from a
`probable_cause` over 100 characters, so all nine pages are currently
noindex — the same gap that four other sources had, and the reason parsers
were written for them.

It is left null here rather than filled badly. All nine documents do carry a
cause section (直接原因 / 事故原因 / 原因分析), but a prototype extractor got
three of nine right: 事故原因 is an ordinary noun phrase in Chinese as well as
a heading, so it matches mid-sentence, and the first hit is often the
contents page — Word's `PAGEREF`/`HYPERLINK` field codes survive the text
extraction and mark those. Writing a plausible-but-wrong cause into a safety
database is worse than leaving the field empty: it would make five pages
indexable carrying text that is not the cause.

Doing it properly needs someone who reads Chinese well enough to tell a
correct extraction from a convincing one.
