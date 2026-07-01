# Aviation Safety Explorer ✈️

A global aviation accident database and analytics dashboard built in **Go**.
This project aggregates aviation accident data from Wikidata's open,
CC0-licensed dataset and exposes it via a REST API and a dark-mode web
dashboard.

## Scope 🚫

This aggregator intentionally does **not** scrape copyrighted third-party
aggregator databases (e.g. commercial accident-archive sites that assert
copyright over their compiled record sets). Earlier revisions of this project
included stealth-mode scrapers for two such aggregators; those have been
removed. The only data source wired up is **Wikidata**, whose accident data
is dedicated to the public domain (CC0) — see [License](#license-) below.

## Features 🚀

- **Wikidata Aggregation**: Pulls global aviation accident records from
  Wikidata via its public SPARQL endpoint (CC0 data).
- **Smart Deduplication**: Normalizes dates across different formats (e.g.,
  `1 Jan 1980` vs `1980-01-01`) and uses fuzzy matching on aircraft models and
  operators to prevent duplicate entries, merging source URLs into a single
  unified record.
- **RESTful API**: Built with the Gin framework, offering lightning-fast JSON
  endpoints for raw data and calculated statistics.
- **Analytics Dashboard**: A responsive, zero-dependency (vanilla HTML/CSS/JS)
  frontend that dynamically renders interactive charts (using Chart.js) to
  display the most dangerous aircraft models and operators.

## Tech Stack 🛠

- **Backend**: Go (Golang)
- **Data source**: SPARQL API (Wikidata)
- **Database**: SQLite (using `mattn/go-sqlite3`)
- **API Framework**: [Gin](https://github.com/gin-gonic/gin)
- **Frontend**: Vanilla JavaScript, CSS3 (Dark Theme), Chart.js

## Installation 📦

Ensure you have Go 1.21+ installed. This project lives under
`sources-go/aircrash` in the
[aviation-safety-scrapers](https://github.com/disclaimer8/aviation-safety-scrapers)
monorepo.

```bash
cd sources-go/aircrash

# Install dependencies
go mod tidy

# Build the executable
go build -o aircrash-parser
```

## Usage 💻

The application operates in two primary modes: **Scraping Mode** and **Server Mode**.

### 1. Web Server & API Mode
To view the analytics dashboard and expose the REST API, simply run:
```bash
./aircrash-parser --serve
```
Then, open your browser and navigate to [http://localhost:8080](http://localhost:8080).

### 2. Scraping Mode
```bash
# Pull the latest global accident data from Wikidata
./aircrash-parser --wikidata=true
```

#### CLI Flags:
- `--serve`: Start the Gin web server instead of the scraper.
- `--port`: Specify the server port (default: `:8080`).
- `--wikidata`: Scrape Wikidata via SPARQL (boolean).
- `--db`: Path to the SQLite database (default: `accidents.db`).

## API Endpoints 🔌

- `GET /api/accidents?limit=100&offset=0` - Retrieve paginated accident records.
- `GET /api/stats/aircrafts` - Retrieve the top 10 most dangerous aircraft models.
- `GET /api/stats/operators` - Retrieve the top 10 operators with the most accidents.

## License 📄
This software is available under the **Apache License 2.0** (see the repository
`LICENSE`). The scraped **data** belongs to the Wikimedia Foundation / Wikidata
contributors; Wikidata's structured data is dedicated to the public domain
under **CC0**, so no additional data-rights review is needed to redistribute
it. This repository does not include scrapers for copyrighted aggregator
databases — see [Scope](#scope-).
