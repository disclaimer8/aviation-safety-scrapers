# aviation-safety-scrapers

A collection of **38 independent scrapers** for public-record civil aviation
accident and incident reports, each targeting a different national **Safety
Investigation Authority** (SIA) — AAIB, BEA, BFU, JTSB, NTSB-equivalents, and
many more across six continents.

Every authority publishes its final reports differently: a server-rendered
table here, a JavaScript-hydrated accordion there, a Cloudflare-gated PDF
archive somewhere else. Each scraper encapsulates the quirks of one source
behind the **same four-verb pipeline** so they all feel identical to operate.

```
discover  →  fetch  →  parse  →  build
 (index)     (PDFs/    (extract   (normalised
             HTML)      text)      rows in SQLite)
```

> **Scope:** these scrapers collect **public-record safety data** — reports
> that national authorities publish specifically so the public can read them.
> They are deliberately slow and polite (single-threaded, paced, identifiable
> User-Agent). Respect each site's `robots.txt` and terms of use, run them at a
> low rate, and use the data for safety research and education. No harvested
> data is included in this repository — only the code that fetches it.

## Repository layout

```
sources/
  <code>/
    <code>_ingest/        # the Python package (discover/fetch/parse/build + CLI)
    tests/                # pytest unit tests with offline HTML/text fixtures
    deploy/               # systemd *-cycle.service + .timer + run-cycle.sh
    pyproject.toml        # standalone package; httpx + (optionally) a PDF/browser dep
    SMOKE.md              # real smoke-test results + source-shape notes
```

Each `sources/<code>/` is a **self-contained Python package** with its own
`pyproject.toml` and test suite. There is no shared runtime library — sources
are intentionally decoupled so one can be copied, run, or rewritten without
touching the others.

## Quickstart

```bash
cd sources/bea          # pick any source

python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# run the pipeline (writes a local SQLite DB; downloads PDFs into ./pdfs)
python -m bea_ingest.cli discover --db bea.db
python -m bea_ingest.cli fetch    --db bea.db --pdf-dir pdfs
python -m bea_ingest.cli parse    --db bea.db
python -m bea_ingest.cli build    --db bea.db

# run the tests (fully offline — fixtures are committed)
pytest
```

Each source's `SMOKE.md` documents its exact CLI, the shape of the upstream
site, and any source-specific gotchas (date formats, pagination tokens,
registration-number quirks, language handling).

## Transport & anti-bot

Most sources are plain, well-behaved **`httpx`** clients. A few sit behind bot
protection that a raw HTTP client can't pass, and use a **headed browser**
driven under a virtual display (`xvfb-run`) instead:

| Method | Sources |
|--------|---------|
| `httpx` (static HTML / direct PDF) | 35 sources |
| Headed Chromium via **patchright** (Cloudflare Turnstile) | `cenipa` |
| Headed Chromium via **Playwright** under Xvfb | `gpiaaf`, `otkes` |

Several `httpx` sources also accept an optional `--proxy` (SOCKS5, e.g. a
WARP endpoint) so requests can be issued from a rotating residential IP when an
authority rate-limits by address. Proxying is never required to run a scraper
locally against its own fixtures or at a gentle rate.

PDF text extraction (where reports are PDF-only) uses `pdftotext` (poppler) or
a pure-Python fallback, declared per-package in `pyproject.toml`.

## Source catalogue

| Code | Authority | Country | Report language | Transport |
|------|-----------|---------|-----------------|-----------|
| `aaib` | Air Accidents Investigation Branch | 🇬🇧 United Kingdom | English | httpx |
| `aaibmy` | Air Accident Investigation Bureau | 🇲🇾 Malaysia | English | httpx |
| `aaiu` | Air Accident Investigation Unit | 🇮🇪 Ireland | English | httpx |
| `aaiube` | Air Accident Investigation Unit | 🇧🇪 Belgium | EN / FR / NL | httpx |
| `aibdk` | Accident Investigation Board (Havarikommissionen) | 🇩🇰 Denmark | Danish / English | httpx |
| `ansv` | Agenzia Nazionale per la Sicurezza del Volo | 🇮🇹 Italy | Italian | httpx |
| `araib` | Aviation & Railway Accident Investigation Board | 🇰🇷 South Korea | Korean | httpx |
| `bea` | Bureau d'Enquêtes et d'Analyses | 🇫🇷 France | French | httpx |
| `bfu` | Bundesstelle für Flugunfalluntersuchung | 🇩🇪 Germany | German | httpx (+proxy) |
| `cenipa` | Centro de Investigação e Prevenção de Acidentes Aeronáuticos | 🇧🇷 Brazil | Portuguese | patchright |
| `ciaado` | Comisión Investigadora de Accidentes de Aviación | 🇩🇴 Dominican Republic | Spanish | httpx |
| `ciaape` | Comisión de Investigación de Accidentes de Aviación | 🇵🇪 Peru | Spanish | httpx |
| `ciaiac` | Comisión de Investigación de Accidentes e Incidentes de Aviación Civil | 🇪🇸 Spain | Spanish | httpx |
| `ciaiauy` | Comisión de Investigación de Accidentes e Incidentes de Aviación | 🇺🇾 Uruguay | Spanish | httpx |
| `dgaccl` | Dirección General de Aeronáutica Civil | 🇨🇱 Chile | Spanish | httpx |
| `dgacgt` | Dirección General de Aeronáutica Civil (UIA) | 🇬🇹 Guatemala | Spanish | httpx |
| `gcaa` | General Civil Aviation Authority | 🇦🇪 United Arab Emirates | English | httpx |
| `gpiaaf` | Gabinete de Prevenção e Investigação de Acidentes com Aeronaves | 🇵🇹 Portugal | Portuguese / English | Playwright/Xvfb |
| `griaa` | Grupo de Investigación de Accidentes Aéreos | 🇨🇴 Colombia | Spanish | httpx |
| `india` | Aircraft Accident Investigation Bureau | 🇮🇳 India | English | httpx |
| `jst` | Junta de Seguridad en el Transporte | 🇦🇷 Argentina | Spanish | httpx |
| `jtsb` | Japan Transport Safety Board | 🇯🇵 Japan | Japanese | httpx |
| `knkt` | Komite Nasional Keselamatan Transportasi (NTSC) | 🇮🇩 Indonesia | Indonesian / English | httpx |
| `nsia` | Norwegian Safety Investigation Authority | 🇳🇴 Norway | Norwegian / English | httpx |
| `otkes` | Onnettomuustutkintakeskus (Safety Investigation Authority) | 🇫🇮 Finland | Finnish / Swedish | Playwright/Xvfb |
| `ovv` | Onderzoeksraad voor Veiligheid (Dutch Safety Board) | 🇳🇱 Netherlands | Dutch / English | httpx |
| `pkbwl` | Państwowa Komisja Badania Wypadków Lotniczych | 🇵🇱 Poland | Polish | httpx |
| `rnsa` | Rannsóknarnefnd samgönguslysa | 🇮🇸 Iceland | Icelandic / English | httpx |
| `sacaa` | South African Civil Aviation Authority (AIID) | 🇿🇦 South Africa | English | httpx |
| `shk` | Statens haverikommission | 🇸🇪 Sweden | Swedish / English | httpx |
| `sub` | Sicherheitsuntersuchungsstelle des Bundes | 🇦🇹 Austria | German | httpx |
| `sust` | Schweizerische Sicherheitsuntersuchungsstelle (STSB) | 🇨🇭 Switzerland | DE / FR / IT / EN | httpx |
| `taic` | Transport Accident Investigation Commission | 🇳🇿 New Zealand | English | httpx |
| `tsb` | Transportation Safety Board | 🇨🇦 Canada | English / French | httpx |
| `tsib` | Transport Safety Investigation Bureau | 🇸🇬 Singapore | English | httpx |
| `ttsb` | Taiwan Transportation Safety Board | 🇹🇼 Taiwan | Chinese | httpx |
| `ueim` | Ulaştırma Emniyeti İnceleme Merkezi | 🇹🇷 Turkey | Turkish | httpx |
| `uzpln` | Ústav pro odborné zjišťování příčin leteckých nehod | 🇨🇿 Czech Republic | Czech | httpx |

## Scheduling

Each source ships a systemd `*-cycle.service` (one-shot, runs the pipeline) and
a `*-cycle.timer` (weekly) under `deploy/`. They assume an install root of
`/opt/<code>` and a dedicated `scraper` user — adjust the paths/user to your
host. The timer cadences in this repo are placeholders; stagger them to avoid
hammering any single authority.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the anatomy of a source package and
how to add a new authority.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE).

The code is Apache-2.0. The **reports** these scrapers retrieve remain the
property of their respective authorities and are subject to each authority's
own terms of use — this license covers the scraping software only, not the
upstream data.
