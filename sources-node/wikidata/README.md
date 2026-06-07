# wikidata-ingest

Ingests **Wikidata** aviation-accident entities into a standalone SQLite.

Wikidata catalogues thousands of aircraft accidents and aviation occurrences as
structured entities (subclasses of `wd:Q744913`, *aviation accident*). This
package queries the public SPARQL endpoint for them, optionally enriches each
with the lead text of its English Wikipedia article, composes a narrative +
probable-cause summary, and writes one row per entity.

## Usage

```bash
npm install

# offline check — parse → compose → SQLite on a sample record, no network
npm run selftest

# query Wikidata and build the SQLite (default ./wikidata-accidents.sqlite)
npm run build
node src/cli.js build --db ./out.sqlite --enrich   # also pull Wikipedia lead text (slower, paced)
```

`--enrich` issues one extra English-Wikipedia request per entity (rate-limited
with a short delay and a descriptive User-Agent). Without it, narratives are
composed from the Wikidata description/cause fields alone.

## Layout

| File | Role |
|------|------|
| `src/parse.js` | Pure SPARQL-results parsing (`parseWikidataResponse`, `extractQId`, `parseFactors`) |
| `src/enrich.js` | English Wikipedia REST/Action API helpers (lead text / summary) |
| `src/compose.js` | Narrative + probable-cause text composition |
| `src/db.js` | Standalone SQLite schema + upsert (one row per `q_id`) |
| `src/cli.js` | SPARQL query → parse → enrich → compose → write |
| `test/parse.test.js` | Offline unit tests for the parser |

## Tests

```bash
npm test       # jest, fully offline
```

## Data

Wikidata content is published under **CC0** (public domain). English Wikipedia
text pulled via `--enrich` is **CC BY-SA** — attribute Wikipedia if you
redistribute composed narratives that include it. This package is the ingest
software only (Apache-2.0).
