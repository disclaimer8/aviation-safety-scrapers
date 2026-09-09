package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// SPARQLResult represents the JSON response from Wikidata
type SPARQLResult struct {
	Results struct {
		Bindings []map[string]struct {
			Value string `json:"value"`
		} `json:"bindings"`
	} `json:"results"`
}

// wikidataRowLimit caps one SPARQL page. With the GROUP BY below one row is
// one accident, so hitting the cap is a real signal that the query needs
// paging rather than an invisible truncation.
const wikidataRowLimit = 10000

// ScrapeWikidata fetches global aviation accidents using the Wikidata SPARQL
// API. It returns an error rather than logging and carrying on, so a failed
// run cannot end with "Scraping finished." and exit 0.
func ScrapeWikidata(db *sql.DB) error {
	// GROUP BY with SAMPLE/MIN, not a bare OPTIONAL join. Four OPTIONALs over
	// multi-valued properties produce the cartesian product of their values:
	// an accident with 3 aircraft and 2 countries arrived as 6 identical-looking
	// rows, and those duplicates filled the LIMIT while genuine accidents fell
	// off the end with no warning at all.
	//
	// P625 (coordinate location) is selected too. It was never queried, so the
	// background geocoder had nothing but the country label to work with and
	// asked Nominatim to locate "United States".
	query := fmt.Sprintf(`
SELECT ?accident ?accidentLabel
       (MIN(?date) AS ?dateMin)
       (MAX(?fatalities) AS ?fatalitiesMax)
       (SAMPLE(?countryLabel) AS ?country)
       (SAMPLE(?aircraftLabel) AS ?aircraft)
       (SAMPLE(?coord) AS ?coordinate)
WHERE {
  ?accident wdt:P31/wdt:P279* wd:Q744913.
  OPTIONAL { ?accident wdt:P585 ?date. }
  OPTIONAL { ?accident wdt:P1120 ?fatalities. }
  OPTIONAL { ?accident wdt:P17 ?country. }
  OPTIONAL { ?accident wdt:P8761|wdt:P289 ?aircraft. }
  OPTIONAL { ?accident wdt:P625 ?coord. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?accident ?accidentLabel
ORDER BY DESC(?dateMin)
LIMIT %d
`, wikidataRowLimit)
	apiURL := "https://query.wikidata.org/sparql?query=" + url.QueryEscape(query)

	req, err := http.NewRequest("GET", apiURL, nil)
	if err != nil {
		return fmt.Errorf("wikidata: build request: %w", err)
	}
	req.Header.Set("Accept", "application/sparql-results+json")
	// Wikidata requires a descriptive User-Agent with a contact. A repository
	// URL satisfies that without publishing a personal mailbox as the abuse
	// contact; this matches what sources-node/wikidata already sends.
	req.Header.Set("User-Agent",
		"aircrash-parser/1.0 (+https://github.com/disclaimer8/aviation-safety-scrapers)")

	client := &http.Client{Timeout: 120 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("wikidata: query: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("wikidata: API status %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("wikidata: read body: %w", err)
	}

	var result SPARQLResult
	if err := json.Unmarshal(body, &result); err != nil {
		return fmt.Errorf("wikidata: unmarshal JSON: %w", err)
	}

	n := len(result.Results.Bindings)
	fmt.Printf("Fetched %d global records from Wikidata. Saving to DB...\n", n)
	if n >= wikidataRowLimit {
		return fmt.Errorf("wikidata: returned %d rows, the query cap — results "+
			"are truncated and the query needs paging", n)
	}

	for _, binding := range result.Results.Bindings {
		// Extract fields safely
		accidentLabel := ""
		if val, ok := binding["accidentLabel"]; ok {
			accidentLabel = val.Value
		}

		dateStr := ""
		if val, ok := binding["dateMin"]; ok {
			// Wikidata date format: "1980-08-19T00:00:00Z"
			dateStr = strings.Split(val.Value, "T")[0]
		}

		// Empty, not "0": an accident whose fatality count Wikidata does not
		// record is unknown, and writing "0" asserts nobody died.
		fatalities := ""
		if val, ok := binding["fatalitiesMax"]; ok {
			fatalities = val.Value
		}

		country := ""
		if val, ok := binding["country"]; ok {
			country = val.Value
		}

		sourceURL := ""
		if val, ok := binding["accident"]; ok {
			sourceURL = val.Value // e.g. http://www.wikidata.org/entity/Q...
		}

		aircraft := ""
		if val, ok := binding["aircraft"]; ok {
			aircraft = val.Value
		}

		// P625 arrives as "Point(lon lat)".
		lat, lon := parsePointWKT(binding["coordinate"].Value)
		if aircraft == "" {
			aircraft = accidentLabel // fallback to incident name
		}

		if dateStr == "" || accidentLabel == "" {
			continue
		}

		accident := Accident{
			Date:          dateStr,
			AircraftModel: aircraft,
			Operator:      "",
			Fatalities:    fatalities,
			Location:      country,
			SourceURL:     sourceURL,
			Lat:           lat,
			Lon:           lon,
		}

		if err := InsertAccident(db, accident); err != nil {
			log.Printf("Error saving wikidata accident: %v\n", err)
		}
	}
	return nil
}

// parsePointWKT reads Wikidata's P625 literal, "Point(<lon> <lat>)". It
// returns (0,0) when the value is absent or malformed; the caller stores that
// as NULL-equivalent and the geocoder fills it in later.
func parsePointWKT(s string) (lat, lon float64) {
	s = strings.TrimSpace(s)
	if !strings.HasPrefix(s, "Point(") || !strings.HasSuffix(s, ")") {
		return 0, 0
	}
	parts := strings.Fields(strings.TrimSuffix(strings.TrimPrefix(s, "Point("), ")"))
	if len(parts) != 2 {
		return 0, 0
	}
	lonV, err1 := strconv.ParseFloat(parts[0], 64)
	latV, err2 := strconv.ParseFloat(parts[1], 64)
	if err1 != nil || err2 != nil {
		return 0, 0
	}
	return latV, lonV
}
