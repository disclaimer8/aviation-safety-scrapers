# Code review: aviation-safety-scrapers

**Date:** 2026-09-09
**Scope:** whole repository on `main` @ `419ddc5` (`chore(deps): refresh Go modules and Node lockfiles (#33)`), not a PR diff.
**Method:** architecture pass + stratified source sample + four parallel deep reviews (control-plane, Python sources, Node/Go, security/CI). Line numbers below were re-read in the files, not copied from memory.

This document is a review artifact. It is not a commit proposal. Do not merge it unless you want it in the public repo.

---

## Bottom line

The repo already has a real engineering culture: four-verb ingest, offline fixtures, atomic crawl-job claims, transactional promotion, SSRF-hardened PDF download in the control plane, and a vendoring gate that actually tests adoption. The highest risk is not style. It is **silent success**: crawls that stop early on a 502, extract that credits any Wayback PDF as official AAI, and a CI that is green while half the tree is an AST parse of a monolith stub.

Two systems live in one repo and have drifted apart:

1. **Independent source packages** (`sources/`, `sources-node/`, `sources-go/`) — fetch public reports into per-source SQLite.
2. **Coverage control plane** (`control-plane/`) — country policy, crawl jobs, Wayback/regional/foreign/manufacturer acquisition, OCR + LLM extract into `events`/`reports`.

Root README still says the control plane “is not a scraper”. That stopped being true once `process-wayback` / `process-extract` landed.

---

## Repo map

| Area | What it is | Maturity |
|---|---|---|
| `sources/` | 90 Python packages | **44** `*_ingest/` packages with CLI + tests; **46** one-file `*_scraper.py` stubs with AST-only smoke tests |
| `_common/` | Canonical `http.py` / `pdf.py` / `text.py` + vendor sync | Solid, but **only 8** packages opted in |
| `sources-node/` | NTSB bulk dump, MAK, ATSB, Wikidata | Real ingest, **not** the four-verb CLI the README claims |
| `sources-go/aircrash/` | Second Wikidata scraper + Gin dashboard | Oldest layer; Docker/toolchain broken; geocoder leak |
| `control-plane/` | Coverage DB + workers + LLM extract | Best-tested Go in the repo; remaining holes are lifecycle and credit, not scaffolding |
| CI | `.github/workflows/tests.yml` | Runs, but Python job does not install per-package extras |

README catalogue lists ~42 Python SIA sources + 5 Node/Go. Disk has **90** `sources/*` directories. Dependabot comment still says “38 scraper packages”; the glob `/sources/*` actually covers all of them.

Contract from `CONTRIBUTING.md`:

```
discover → fetch → parse → build
```

That contract is real for the 44 ingest packages (26 fold `parse` into `fetch`, which CONTRIBUTING allows). The 46 stubs use `sys.argv[1]` in a monolith. Node CLIs only expose `build`. Go aircrash exposes `--wikidata` / `--serve`.

---

## What is already strong

Keep these. Improvements should extend them, not replace them.

- **Four-verb resumable pipelines** in the mature Python packages (`bea` refetch of frozen stubs, `aaib` GOV.UK API, `ovv` fail-closed discover).
- **`_common` vendoring with a drift gate** (`python -m _common.sync --check` in CI) and `_common/tests/test_adoption.py` that imports each opted-in CLI and asserts `RetryTransport`.
- **Control-plane crawl jobs:** `claimJob` is a compare-and-set (`pending` or stale `running` → `running`), planner `ON CONFLICT DO NOTHING`, WAL + `busy_timeout` + `MaxOpenConns(1)` per process.
- **`PromoteDocument` is one transaction** with `MarkExtractedTx`. Crash mid-promote re-selects the doc instead of duplicating a half-written event.
- **Dedup key-2 corroboration** and placeholder-reg handling (`N/A`, `б/н`) — GO-CP-5 — are real and tested.
- **Extract PDF download:** http(s) only, private-IP dial guard on every hop, 64 MiB cap, atomic write (`control-plane/internal/worker/extract/download.go`).
- **GO-CP-1 country attribution:** body-wide listings no longer inherit the job’s country; manufacturer docs stay country-less unless the LLM can name an ISO2.
- **Offline tests** for CDX parse, planner enqueue, ICAO importers, most adapters. Node NTSB streaming CSV chunk-boundary tests are genuine.
- **No `shell=True`**, no pickle, no tracked secrets. Harvested `*.db` / `pdfs/` are gitignored.
- **ATSB** upsert refusal + `COALESCE` and challenge-page tripwire. **Wikidata Node** pagination + `SILENT_FAIL_SUSPECT`. **OVV** is the Python reference for “a 502 is not end-of-listing”.

---

## Findings

Severity:

- **P0** — data loss or host compromise in the current operator setup; fix first.
- **P1** — should fix before the next wave of sources / before trusting extract output.
- **P2** — real, not blocking this week.
- **P3** — polish / follow-up.

### P0 — Silent truncated crawls reported as success

`_common/http.py:14-20` already documents this as a bug the project has paid for: a 502 on page 30 ends the listing, discover returns normally, missing reports look like “source has nothing new”.

**1. `pkbwl` stops the walk on any listing exception**

File: `sources/pkbwl/pkbwl_ingest/pipeline.py:39-45`

```python
try:
    status, listing_html = pkbwl.fetch_listing(client, page)
except Exception as e:
    print(f"[pkbwl discover] page {page}: failed: {e}", file=sys.stderr)
    break
```

A 404 is a clean stop (line 44). A 502 is not distinguished. `tests/test_pipeline.py` even asserts “stop after a failed page”.

**2. `aaibmy` hub failure returns 0**

File: `sources/aaibmy/aaibmy_ingest/pipeline.py:39-43`

Hub GET exception → `return 0`. Weekly timer logs a clean discover of nothing.

**3. Page failures are skipped, not raised**

- `sources/nsib/nsib_ingest/pipeline.py:63-69` — log + `continue`
- `sources/cenipa/cenipa_ingest/pipeline.py:49-54` — same
- `sources/ansv/ansv_ingest/pipeline.py:57-60` — non-200: continue
- `sources/sacaa/sacaa_ingest/pipeline.py:42-46` — listing exception: continue
- `sources/ghana/ghana_scraper.py:40-42` — `except Exception: continue`

**4. Reference implementation already exists**

File: `sources/ovv/ovv_ingest/pipeline.py:41-52`

OVV re-raises `RuntimeError` after retries so the cycle is loud, and treats an empty page 1 as a markup change, not an empty source. Copy that, do not invent a third policy.

**Suggested fix:** one shared rule — retry, then fail the process; never `break`/`return 0` on transport errors. Empty page 1 is a hard failure for any source that is not actually empty. Add one resilience test per paginated source, modelled on `ovv/tests/test_resilience.py`.

---

### P0 — Response-level HTTP retries exist, and almost nobody uses them

File: `_common/http.py:9-13`

`httpx.HTTPTransport(retries=N)` retries **connect errors only**. 502 / read timeout / origin 503 still fail on the first attempt.

**Measured `RetryTransport` adopters: 7** (plus `rosap`, which vendors pdf/text only because it is browser-only):

`aaibmy`, `ahac`, `ciaiauy`, `nsib`, `ntsbaar`, `ovv`, `sacaa`.

Flagship sources still build a raw client:

```python
# sources/bea/bea_ingest/cli.py:10-15
def _make_client():
    return httpx.Client(
        timeout=60,
        follow_redirects=True,
        headers={"User-Agent": "bea-ingest/1.0"},
    )
```

Same shape in `aaib`, `bfu`, `tsb`, `india`. `bea.iter_events` then `raise_for_status()` on every paginator page (`sources/bea/bea_ingest/bea.py:143-162`) — so a single 502 aborts the whole discover. That is better than truncating silently, but it is still a one-shot walk of a large TYPO3 list with **no delay**.

**Poster child:** `sources/cins/cins_ingest/cli.py:11-15` documents a previous bug (retries only applied when a proxy was set), then “fixes” it with `HTTPTransport(retries=3)` — the exact footgun `_common/http.py` exists to replace.

**Suggested fix:** add every HTTP `*_ingest` package to `VENDORED` in `_common/sync.py`. The adoption test already exists. `cins` is the first PR.

---

### P1 — Control plane: extract lifecycle and credit

**5. LLM/OCR client timeouts abort the whole extract queue**

File: `control-plane/internal/worker/extract/infra.go:44-54`

`isInfraError` is true for **any** `net.Error`. `http.Client` timeouts unwrap to that. GO-CP-3 correctly wanted “Ollama is down → do not burn every document’s attempt budget”. The inversion: one slow PDF times out, `extractOne` does not increment `extraction_attempts`, `ProcessExtractPending` returns, and the same high-priority document is first on the next run. Later docs starve.

`TestIsInfraErrorRejectsApplicationError` does not cover `*url.Error` + deadline exceeded.

**Fix:** treat `ECONNREFUSED` / `ENETUNREACH` / DNS not-found as infra. Treat request timeouts as per-document failures (or a bounded skip that still advances the queue).

**6. Wayback download failures are terminal**

File: `control-plane/internal/worker/wayback/download.go:45-48, 20-25`

Failed fetch sets `download_status='failed'`. Next `process-wayback` only selects `pending`. Staging is `ON CONFLICT DO NOTHING`, so the row is never recreated. Regional/foreign extract **does** retry `failed` downloads. Wayback does not. A transient archive.org 5xx permanently drops that capture.

**7. Accidents missing LLM-critical fields are `skipped` forever**

File: `control-plane/internal/worker/extract/core.go:72-77`

`is_aviation_accident=false` and “accident but empty date/type” both call `MarkSkipped`. Skipped is not in `PendingDocs`. `reset-failed` only resets `failed`. The JSON schema requires keys but allows `""`, which is how Ollama already omitted registration in production (comment on `llm.go:40-46`).

**Fix:** non-accident → `skipped`; accident without fields → `failed` / `manual_review`.

**8. `process-extract` has no document claim**

File: `control-plane/internal/worker/extract/core.go:113-158`

Crawl jobs have `claimJob`. Extract selects pending docs and processes them with no CAS. Two overlapping cron runs can both `FindDuplicateEvent` on an empty snapshot and both insert. `MaxOpenConns(1)` is per process, not global.

**9. One Airbus Safety First issue → one accident**

File: `control-plane/internal/worker/manufacturer/parse.go:89-98`

Listing parse emits one row per magazine PDF. Extract promotes one `ExtractedEvent`. Remaining articles in that issue are dropped; the model will pick one story or mix fields.

**10. Wayback PDFs are credited as official AAI (tier 1, confidence up to 100)**

File: `control-plane/internal/worker/extract/wayback_source.go:80-90`

If the country has a `national_aai`/`caa` row, **every** extracted Wayback PDF becomes `source_type='official_aai'`, tier 1, `copyright_status='official_public'`. `ConfidenceScore` then adds +20. Four presence-only fields (date, any location string, type or reg, `fatalities != nil` including 0) yield 100.

CDX is “all PDFs under this domain”: forms, newsletters, procurement. Downstream will treat them as primary official reports.

**Fix:** credit `official_aai` only when `original_url` host matches the authority website/archive host. Score extraction quality separately from source tier.

**11. LLM input is head-truncated; schema has no narrative**

File: `control-plane/internal/worker/wayback/llm.go:79-86`

Default `--max-input-chars` is 24000 from the **start** of OCR text. Probable cause in a long report lives at the end. `ExtractedEvent` has no narrative / probable-cause field — metadata only. Python scrapers extract `narrative_text`; the control plane does not. Those two pipelines will never agree on “do we have the report text”.

Prompt is `template + text` with no fence (`prompts/extract.txt` ends at `REPORT TEXT:`). A PDF can instruct the model to emit a famous registration+date and **key-1 auto-link** into another accident (`promote.go:239-252`, global, not country-scoped). No `temperature: 0` / seed. Dates are not regex-validated (`"yesterday"` passes `HasCriticalFields`).

---

### P1 — Copy-paste leftovers in flagship packages

**12. TSB Canada and BFU Germany still ship BEA France**

| File | What it is |
|---|---|
| `sources/tsb/tsb_ingest/bea.py` | `"""TYPO3 HTML scraper for bea.aero …"""` |
| `sources/tsb/tsb_ingest/govuk.py` | GOV.UK client leftover |
| `sources/tsb/tests/test_bea.py` | CI still runs BEA parser tests inside TSB |
| `sources/tsb/SMOKE.md` | “BEA Phase-1 smoke (15 events, page ~200 of global list)” |
| `sources/bfu/bfu_ingest/bea.py` | same clone |
| `sources/bfu/tests/test_bea.py` | same |
| `sources/bfu/SMOKE.md` | BEA smoke |
| `sources/bea/bea_ingest/govuk.py` | unused AAIB client inside BEA |
| `sources/bea/bea_ingest/pdf.py:1` | header is `# aaib_ingest/pdf.py` |

This is the cleanest “delete it” PR in the tree. It also makes TSB/BFU look like they ingest bea.aero.

**13. 46 stubs, 48 AST smokes**

`sources/malta/tests/test_smoke.py` (and the same template in ~47 other files) `ast.parse`s the script and checks that the docstring mentions the country or `http`. It does not import the module, does not parse a fixture, does not assert a row. Several comments still say “importing would start scraping” even when `if __name__ == "__main__"` exists (`malta_scraper.py:161`).

`sources/cipaa/cipaa_scraper.py` `sys.exit(1)` at runtime (FOI is 401) and is green in CI.

**14. Malta fetch marks failures as fetched**

File: `sources/malta/malta_scraper.py:117-119`

If the in-page `fetch` fails, `pdf_path` stays `None` but status is still `'fetched'`. Parse then extracts empty text; build skips. The row never retries.

**15. `aaiuz` reuses Israel’s Cloudflare profile, tilde not expanded**

File: `sources/aaiuz/aaiuz_patchright_helper.py:8-14`

```python
PROFILE = "~/israel-ingest/.cf-profile"
ctx = pw.chromium.launch_persistent_context(PROFILE, headless=False, ...)
```

Chromium gets a directory literally named `~` under CWD, or, if someone expands it, Israel’s CF cookies. Helper is launched from CENIPA’s venv (`aaiuz_scraper.py`).

---

### P1 — Node / Go sources

**16. Geocoder leaks response bodies forever**

File: `sources-go/aircrash/geocoder.go:61-75`

`defer resp.Body.Close()` inside an infinite `for` in a goroutine that never returns. Every Nominatim response leaks an FD until the process dies.

Failed geocode writes `lat=lon=0.000001` (a real point near null island). Location is **country only** from Wikidata (`countryLabel`); Nominatim then searches `"United States"`. Wikidata `P625` coordinates are never queried.

**17. Go Wikidata SPARQL is a cartesian `LIMIT 10000`**

File: `sources-go/aircrash/scraper_wikidata.go`

OPTIONAL date × fatalities × country × aircraft, no `GROUP BY`, no pagination. Multi-valued properties fill the cap with duplicate events; extras are dropped with no warning. Missing fatalities become `"0"`. HTTP failure is logged and `main` still prints “Scraping finished.” and exits 0.

**18. Aircrash API: negative LIMIT + Docker publishes :80**

Files: `sources-go/aircrash/server.go:33-41`, `db.go:219-220`, `docker-compose.yml:7-8`

`GET /api/accidents?limit=-1` is passed to SQLite. Negative `LIMIT` means unlimited. Compose maps `80:8080`. No auth. Dashboard interpolates DB strings with `innerHTML` (`static/index.html`) — Wikidata vandalism → stored XSS.

**19. Dockerfile cannot build the module**

`sources-go/aircrash/Dockerfile:2` is `golang:1.21-bookworm`. `go.mod` requires `go 1.26.2`.

**20. NTSB tests cover a function production does not call**

- Production mapper: `mapToAccidents` in `sources-node/ntsb/src/cli.js:175`
- Tests: `joinNtsbTables` in `src/parse.js:26`
- Production **drops** empty `narr_accp`; tests **keep** them
- Production keeps **first** aircraft; `joinNtsbTables` keeps last (`parse.js:48`)
- `buildFactorsJson` is untested dead code relative to the real CLI

A join bug in the real dump will not fail `npm test`.

**21. ATSB / MAK silent end-of-archive**

`sources-node/atsb/src/scrape.js:203-206` — a later listing page with 0 rows is treated as past the last page, even if it is an Akamai interstitial that still looks like ATSB chrome. `assertNotChallengePage` only fires when `looksLikeAtsbPage` is false.

MAK year listing failure is logged and skipped (`sources-node/mak/src/cli.js`); process exits 0 if every year failed. No `SILENT_FAIL_SUSPECT`.

**22. Two Wikidata ingests, no shared parser**

Node `sources-node/wikidata` vs Go `sources-go/aircrash`. Different SPARQL, different schemas, different coordinates. They will disagree on counts. The Go copy is the weaker one.

---

### P1 — Security on the ingest host

**23. ROSAP Chromium `--no-sandbox`**

File: `sources/rosap/rosap_ingest/cli.py:47-48`

Headed Patchright, unsandboxed renderer, live US DOT site, systemd `User=scraper`. A renderer bug + hostile page is host compromise.

**24. Persistent browser profiles are credentials**

Israel / Malta / Mexico / AIAS store Cloudflare cookies under `~/…/.cf-profile`. ATSB stores Akamai cookies under `tmp/atsb-chromium-profile`. Units do not lock those directories. See also finding 15.

**25. Ghana disables TLS verification for the whole site**

File: `sources/ghana/ghana_scraper.py:37`

```python
cl=httpx.Client(headers=H, timeout=25, follow_redirects=True, verify=False)
```

`ojk` does the same for one Solr host with a comment about an incomplete chain (`ojk_scraper.py:14-15,76`) — still unpinned. Ghana has no such comment.

**26. Python/Node follow redirects with no SSRF allow-list**

`_common/http.py:101` `follow_redirects=True`. Control-plane extract **does** block RFC1918/loopback. Python/Node do not. A listing PDF href or 302 to `http://169.254.169.254/` is fetched from the mini-PC.

Go extract’s own guard (`download.go:50-63`) is still DNS-rebinding TOCTOU: resolve, then dial **by hostname**. Wayback `httpFetcher.Get` (`wayback/fetcher.go:51-79`) has no scheme check and no private-IP dial; default client follows redirects off archive.org.

**27. systemd: login shell, no sandbox**

Typical unit (`sources/bea/deploy/bea-cycle.service`):

```
ExecStart=/bin/bash -lc "deploy/run-cycle.sh"
TimeoutStartSec=7200
```

`-lc` reads `~/.profile`. No `ProtectSystem`, `NoNewPrivileges`, `PrivateTmp`, `ProtectHome`. Writable `scraper` profile → code exec every timer.

**28. `OCR_REMOTE` is passed to `ssh` unvalidated**

File: `_common/pdf.py:32-44`

`lang` is `shlex.quote`d; host is not. `ssh` will take `-oProxyCommand=…` as extra options if the env var is attacker-controlled.

---

### P2 — Politeness, contract, CI, data quality

**29. `robots.txt` is documentation only**

README and CONTRIBUTING say honour robots. Code: `robots_policy` is seed metadata and an export column. `CrawlErrorTypeRobotsBlocked` exists in tests only. No fetcher, no parser, no skip.

**30. Identifiable User-Agent is mostly a README fiction**

Good: `bea-ingest/1.0`, `aaib-ingest/1.0`, `aviation-coverage/1.0`, Wikidata Node with GitHub URL.

Majority of Python stubs and many full packages send a generic Chrome 124 UA with no bot token or contact (`ghana_scraper.py:8`, `aaibmy.py` HEADERS, ATSB/MAK). `_common/tests/test_adoption.py:82-88` only asserts UA is non-empty and not `python-httpx`.

CENIPA documents that a custom UA **breaks** Cloudflare bypass — so the exception needs to be named, not implicit.

**31. BEA and AAIB are unpaced; Wikidata enrich is ~20 rps**

No `time.sleep` under `sources/bea`. AAIB walks GOV.UK search in `page_size=100` bursts. BFU has `DELAY = 3.0` specifically because of captchas. Wikidata Node `FETCH_DELAY_MS = 50` (`sources-node/wikidata/src/cli.js:24`) vs WMF ~1 req/s.

**32. Four-verb CLI is not what Node/Go expose**

Root README (`README.md:178`) shows `cd sources-node/ntsb && npm run selftest`. ATSB/MAK comments describe discover→fetch→parse→build internally, but the CLI is a single `build`. Either add the verbs or stop claiming them.

**33. CI does not install package extras**

`.github/workflows/tests.yml:39` — `pip install "httpx[socks]>=0.27" certifi "pytest>=8"`. Not `pip install -e ".$dir"`. Patchright/Playwright/`requests` (ASRS) never install. Tests that only AST-parse stay green. `cins` and `ntsbcarol` pyprojects do not even declare `httpx`; they work in CI because the job injects it globally. `npm ci --no-audit --no-fund`. No `pytest-socket`. No Python version matrix.

**34. Narrative admission bars have already drifted**

`_NARRATIVE_FLOOR` is 80 (`bea`/`aaib`/`bfu`/`tsb`/`cenipa`/`nsib`/`cins`) vs 300 (`ovv`/`india`/`aaibmy`/`sacaa`/`rosap`) vs 200 (`ntsbcarol`). Same corpus, different “this is a report” threshold.

**35. Path names from HTML**

Go extract writes `<storeDir>/<iso2>/<sha256>.pdf` — good. Wayback writes `<digest>.pdf` with **unsanitized CDX digest** (`wayback/download.go:55`); `filepath.Join` + `Clean` can escape `store-dir` if digest contains `..`. Python often does `os.path.join(pdf_dir, slug + ".pdf")` (`bea_ingest/pipeline.py`, `aaib_ingest/pipeline.py:59`). BFU has `_safe_filename`; most do not.

**36. OCR HTTP body is unbounded**

`control-plane/internal/worker/wayback/ocr.go` — `io.ReadAll(resp.Body)` vs 64 MiB caps on PDF download.

**37. Dedup key-2 is case-sensitive on `operator_name`**

File: `promote.go:254-260`. Type/location use `EqualFold`. `"Aeroflot"` vs `"AEROFLOT"` creates a second event. On key-1 link, empty fields on the existing event are not upgraded from the new report.

**38. CDX query string is unescaped**

`wayback/fetcher.go:35-45` concatenates `url=` + domain. `ResolveTarget` may fall back to `authorities.archive_url`, which is a URL field, not a host. A `?` in that value silently truncates the CDX query (`found=0` / `SILENT_FAIL_SUSPECT`).

**39. NTSB `toInt` maps missing injuries to 0**

`sources-node/ntsb/src/cli.js:199` — unknown fatality counts become “zero dead”, same class of bug as Go Wikidata `"0"`.

**40. Zip-slip on `unzip -o` of `avall.zip`**

`sources-node/ntsb/src/cli.js` — no member path check. Dump is official NTSB; still worth sanitizing.

---

### P3 — Docs, gitignore, leftovers

- README: “47 independent scrapers” / “42 Python packages” vs 90 directories. Control-plane README vs root README (“not a scraper”).
- Dependabot comment: “38 scraper packages”.
- `sources/cins/cins_ingest/cli.py.bak-20260803` backup in tree.
- `cenipa/tests/test_ansv.py` is an empty placeholder (“no ANSV code in this repo”).
- NOTICE does not say harvested PDFs remain © the authority; Airbus reprint-with-attribution lives only in `control-plane/README.md:302-304`.
- Personal mailbox in Go Wikidata UA (`disclaimer8@gmail.com`) — required by Wikidata policy, but it is a public abuse inbox.
- Compiled `aircrash-parser` can sit in the working tree; `.gitignore` intends to exclude it.
- Root `.gitignore` does **not** list `wayback-store/` / `coverage.json` / `aviation-coverage`; **`control-plane/.gitignore` does**. That is fine if operators always run from `control-plane/`.

---

## Architecture: how to improve

Three options, ranked by reversibility.

### Option A — smallest change (recommended next 2–4 weeks)

Do not introduce a shared runtime package. Keep the “copy a source away and run it” promise.

1. **Fail closed on discover.** Lift OVV’s raise-after-retries into a short `_common` note + one test per paginated source. Change `pkbwl` `break` → re-raise; `aaibmy` `return 0` → raise.
2. **Vendor `httpc` into all 44 HTTP ingest packages.** The generator and the adoption test are already there. Flagship first: `bea`, `aaib`, `bfu`, `tsb`, `cins`, `india`.
3. **Delete clone leftovers** (finding 12). One PR, no behaviour change for TSB/BFU/BEA ingest.
4. **Extract lifecycle:** split skip vs missing-fields; retry Wayback `failed` downloads; classify LLM timeouts as per-document; CAS extract rows.
5. **Stop lying in docs:** catalogue the 46 stubs as “prototype / not scheduled”; Node/Go as single-shot `build`; control plane as an acquisition+extract worker.

This is the only option that pays down the bug `_common/http.py` already named without a rewrite.

### Option B — modular boundary (1–2 months)

Introduce a **canonical output schema** and a **source registry**, still without a shared runtime.

Today every package has its own `*_reports` / `*_accidents` SQLite. Downstream (FlightFinder) must know 90 shapes. Control-plane `events`/`reports` is a third shape, and it has no narrative.

Proposal:

- Keep per-source working DBs as now.
- Add `build` output as a documented table (or parquet/jsonl) with a shared column set: `source_code`, `case_id`, `event_date`, `registration`, `aircraft`, `location`, `country`, `narrative_text`, `probable_cause`, `source_url`, `report_type`, `lang`, `checksum`.
- A tiny `registry.yaml` (code, country, transport, UA, delay, verbs, smoke status) generated into README so the catalogue cannot drift.
- Stub → package: one cookie-cutter (`sources/rosap` or `sources/ovv` as template) and a script that wraps a monolith’s four functions. Do **not** rewrite all 46 in one PR. Convert when a stub is next touched; refuse new AST-only smokes.

### Option C — larger redesign (only if B is not enough)

A single worker binary that runs all sources. Reject for now. The repo’s explicit goal is “each source can be copied away”. A monolith worker would fight that, and Cloudflare/Akamai sources still need headed browsers on residential egress.

Control-plane extract **should** stay Go. Do not port it to Python. Do:

- Shared hardened HTTP (scheme, SSRF, size, redirect host allow-list) used by Wayback **and** extract.
- Multi-event extract for manufacturer issues and long HTML listings.
- Confidence = source_tier × extraction_quality × corroboration, not presence-only + official bonus.
- Fence + `temperature: 0` + date/ISO2 regex for the LLM.
- Prefer header+tail (or chunked) OCR text over head-truncation; persist narrative, not only metadata.

**Pick A, then B.** Do not start C.

---

## Ranked backlog

Impact vs effort. First items are the ones I would open as PRs.

| # | Item | Impact | Effort | Notes |
|---|---|---|---|---|
| 1 | Fail-closed discover (pkbwl, aaibmy, nsib, sacaa, ansv, ghana) | Stops silent data loss | S | Copy OVV |
| 2 | Vendor `httpc` into bea/aaib/bfu/tsb/cins | Retries the paid-for 502 | S | Adoption test already there |
| 3 | Delete TSB/BFU/BEA clone leftovers | Stops CI testing the wrong source | S | Pure delete |
| 4 | Clamp aircrash `limit`; bind 127.0.0.1; fix Dockerfile Go version | Stops unauth dump + broken image | S | |
| 5 | Geocoder: close body in-loop; query P625; never write 0.000001 | Stops FD leak + fake coords | S | |
| 6 | BEA/AAIB `DELAY` ≥ 1–2s; Wikidata enrich ≥ 1s | Ban risk | S | |
| 7 | Drop ROSAP `--no-sandbox` | Host compromise | S | |
| 8 | Wayback: retry `failed` downloads; sanitize digest | Permanent capture loss | S | |
| 9 | Extract: skip vs missing-fields; timeout ≠ infra; document CAS | Queue stall + lost accidents | M | |
| 10 | NTSB: test `mapToAccidents`; `toInt` → null | Tests lie today | S | |
| 11 | ATSB: don’t treat empty later page as EOF if pager has next | Silent truncation | S | |
| 12 | MAK: non-zero exit when every year failed; COALESCE like ATSB | Silent empty DB | S | |
| 13 | CI: `pip install -e ".$dir"` + `pytest-socket`; stop `--no-audit` | False green | M | |
| 14 | systemd: no `bash -lc`; `ProtectSystem=strict`; `PrivateTmp` | Persistence | S | Template one unit, copy |
| 15 | Sanitize PDF filenames (`_safe_filename` everywhere) | Path join from HTML | S | BFU already has it |
| 16 | Credit Wayback as official only on host match | Bad events as tier-1 | M | |
| 17 | Fence LLM input; validate dates; temperature 0 | Hallucinated merges | S | |
| 18 | Registry + README generated from disk | 90 vs 47 | S | |
| 19 | Stub conversion template; no new AST smokes | 46 untested scrapers | M | One stub per PR |
| 20 | Python/Node SSRF allow-list | LAN ingest box | M | Reuse Go guard |
| 21 | Real robots.txt or drop the claim | Legal/docs | M | |
| 22 | Canonical `build` schema | Downstream glue | M | Option B |
| 23 | Unify Wikidata (keep Node, retire Go SPARQL or share query) | Two truths | M | Keep Go as dashboard only |
| 24 | Manufacturer multi-event extract | Dropped articles | M | |
| 25 | NOTICE: harvested PDFs © authority; Airbus attribution | Legal | S | |

---

## Suggested first three PRs

1. **`fix(discover): fail closed on listing transport errors`** — pkbwl, aaibmy, and any other `except: break`/`return 0`. Tests that a 502 does not look like end-of-data. No retry policy change yet.
2. **`fix(http): vendor RetryTransport into bea, aaib, bfu, tsb, cins`** — `_common.sync` + adoption test goes green for those five. Behaviour change is “502 is retried”, which is the documented intent.
3. **`chore: remove BEA leftovers from tsb and bfu`** — delete `bea.py` / `govuk.py` / `tests/test_bea.py`, rewrite `SMOKE.md`, drop unused `bea/bea_ingest/govuk.py`. CI must still pass TSB/BFU’s own tests.

After those: aircrash leak+API clamp, extract skip/timeout, Wayback download retry.

---

## What I did not verify

- Did not run `pytest`, `go test ./...`, or `npm test` in this pass (read-only review). CI on `main` is assumed green from the last commit message, not re-executed here.
- Did not hit live SIA sites, ICAO, archive.org, or Ollama.
- Did not audit git history for leaked secrets.
- 46 stubs were sampled (malta, ghana, israel, mexico, ojk, cipaa, aaiuz, plus grep across `*_scraper.py`), not line-read in full.
- FlightFinder consumption of these SQLite files is out of repo and was not opened.

---

## Residual risk after the first three PRs

Cloudflare/Akamai sources will still only fail in production (CI never launches Chromium). Stub scrapers will still be untested beyond `ast.parse`. Control-plane extract will still promote LLM metadata without narrative. Two Wikidata pipelines will still disagree. Those are Option B, not a weekend.
)
