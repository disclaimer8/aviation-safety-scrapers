package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
)

// SPARQLResult represents the JSON response from Wikidata
type SPARQLResult struct {
	Results struct {
		Bindings []map[string]struct {
			Value string `json:"value"`
		} `json:"bindings"`
	} `json:"results"`
}

// ScrapeWikidata fetches global aviation accidents using Wikidata SPARQL API.
func ScrapeWikidata(db *sql.DB) {
	query := `
SELECT ?accident ?accidentLabel ?date ?fatalities ?countryLabel ?aircraftLabel WHERE {
  ?accident wdt:P31/wdt:P279* wd:Q744913.
  OPTIONAL { ?accident wdt:P585 ?date. }
  OPTIONAL { ?accident wdt:P1120 ?fatalities. }
  OPTIONAL { ?accident wdt:P17 ?country. }
  OPTIONAL { ?accident wdt:P8761|wdt:P289 ?aircraft. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?date)
LIMIT 10000
`
	apiURL := "https://query.wikidata.org/sparql?query=" + url.QueryEscape(query)

	req, err := http.NewRequest("GET", apiURL, nil)
	if err != nil {
		log.Printf("Failed to create request: %v\n", err)
		return
	}
	req.Header.Set("Accept", "application/sparql-results+json")
	// Wikidata requires a descriptive User-Agent
	req.Header.Set("User-Agent", "AviationSafetyExplorer/1.0 (disclaimer8@gmail.com) Go/1.22")

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("Failed to query Wikidata: %v\n", err)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("Wikidata API error: %d\n", resp.StatusCode)
		return
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Printf("Failed to read body: %v\n", err)
		return
	}

	var result SPARQLResult
	if err := json.Unmarshal(body, &result); err != nil {
		log.Printf("Failed to unmarshal JSON: %v\n", err)
		return
	}

	fmt.Printf("Fetched %d global records from Wikidata. Saving to DB...\n", len(result.Results.Bindings))

	for _, binding := range result.Results.Bindings {
		// Extract fields safely
		accidentLabel := ""
		if val, ok := binding["accidentLabel"]; ok {
			accidentLabel = val.Value
		}

		dateStr := ""
		if val, ok := binding["date"]; ok {
			// Wikidata date format: "1980-08-19T00:00:00Z"
			dateStr = strings.Split(val.Value, "T")[0]
		}

		fatalities := "0"
		if val, ok := binding["fatalities"]; ok {
			fatalities = val.Value
		}

		country := ""
		if val, ok := binding["countryLabel"]; ok {
			country = val.Value
		}

		sourceURL := ""
		if val, ok := binding["accident"]; ok {
			sourceURL = val.Value // e.g. http://www.wikidata.org/entity/Q...
		}

		aircraft := ""
		if val, ok := binding["aircraftLabel"]; ok {
			aircraft = val.Value
		}
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
		}

		err := InsertAccident(db, accident)
		if err != nil {
			log.Printf("Error saving wikidata accident: %v\n", err)
		}
	}
}
