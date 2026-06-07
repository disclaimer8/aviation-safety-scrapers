# Aviation Safety Explorer ✈️

A powerful, high-performance global aviation accident database and analytics dashboard built in **Go**. This project scrapes, deduplicates, and aggregates aviation accident data from multiple global sources, exposing it via a robust REST API and a beautiful, dark-mode web dashboard.

## Features 🚀

- **Multi-Source Aggregation**: Seamlessly collects data from the world's largest aviation databases:
  - Aviation Safety Network (ASN)
  - Wikidata (SPARQL Global Query)
  - Bureau of Aircraft Accidents Archives (B3A)
- **Smart Deduplication**: Normalizes dates across different formats (e.g., `1 Jan 1980` vs `1980-01-01`) and uses fuzzy matching on aircraft models and operators to prevent duplicate entries, merging source URLs into a single unified record.
- **RESTful API**: Built with the Gin framework, offering lightning-fast JSON endpoints for raw data and calculated statistics.
- **Analytics Dashboard**: A responsive, zero-dependency (vanilla HTML/CSS/JS) frontend that dynamically renders interactive charts (using Chart.js) to display the most dangerous aircraft models and operators.

## Tech Stack 🛠

- **Backend**: Go (Golang)
- **Scraping**: [Colly](https://github.com/gocolly/colly) (HTML) & SPARQL API (Wikidata)
- **Database**: SQLite (using `mattn/go-sqlite3`)
- **API Framework**: [Gin](https://github.com/gin-gonic/gin)
- **Frontend**: Vanilla JavaScript, CSS3 (Dark Theme), Chart.js

> **Scraping note:** ASN and B3A are fetched through a headed browser
> (go-rod + stealth) because both sit behind bot protection. Run from a
> residential / non-datacenter IP and at a polite rate.

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
You can customize the scraping process using CLI flags. The scraper will automatically deduplicate and merge new records into your `accidents.db`.

```bash
# Scrape all supported sources from 1980 to the current year
./aircrash-parser --start=1980 --end=2024 --asn=true --wikidata=true --b3a=true

# Scrape only Wikidata (Global semantic search)
./aircrash-parser --asn=false --b3a=false --wikidata=true
```

#### CLI Flags:
- `--serve`: Start the Gin web server instead of the scraper.
- `--port`: Specify the server port (default: `:8080`).
- `--start`: Year to start scraping from (default: `1980`).
- `--end`: Year to end scraping (default: current year).
- `--asn`: Scrape Aviation Safety Network (boolean).
- `--wikidata`: Scrape Wikidata via SPARQL (boolean).
- `--b3a`: Scrape Bureau of Aircraft Accidents Archives (boolean).
- `--db`: Path to the SQLite database (default: `accidents.db`).

## API Endpoints 🔌

- `GET /api/accidents?limit=100&offset=0` - Retrieve paginated accident records.
- `GET /api/stats/aircrafts` - Retrieve the top 10 most dangerous aircraft models.
- `GET /api/stats/operators` - Retrieve the top 10 operators with the most accidents.

## License 📄
This software is available under the **Apache License 2.0** (see the repository
`LICENSE`). The scraped **data** belongs to its respective sources — Aviation
Safety Network, the Bureau of Aircraft Accidents Archives (B3A), and the
Wikimedia Foundation — and is subject to each source's own terms of use.
Aviation Safety Network in particular asserts copyright over its database;
review and respect its terms before redistributing any harvested ASN data.
