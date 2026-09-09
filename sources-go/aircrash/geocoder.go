package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"time"
)

type NominatimResponse struct {
	Lat string `json:"lat"`
	Lon string `json:"lon"`
}

// geocodeOne performs a single Nominatim lookup. It is a function so the
// response body is closed when IT returns — the previous code had
// `defer resp.Body.Close()` inside an endless for loop in a goroutine that
// never returns, so every response leaked a file descriptor until the process
// died.
func geocodeOne(client *http.Client, location string) (lat, lon string, err error) {
	geocodeURL := fmt.Sprintf(
		"https://nominatim.openstreetmap.org/search?q=%s&format=json&limit=1",
		url.QueryEscape(location))
	req, err := http.NewRequest("GET", geocodeURL, nil)
	if err != nil {
		return "", "", err
	}
	// Nominatim requires a user-agent to comply with their usage policy
	req.Header.Set("User-Agent", "AviationSafetyExplorer/1.0 (+https://github.com/denyskolomiiets/aviation-safety-scrapers)")

	resp, err := client.Do(req)
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", "", fmt.Errorf("nominatim status %d", resp.StatusCode)
	}
	var results []NominatimResponse
	if err := json.NewDecoder(resp.Body).Decode(&results); err != nil {
		return "", "", err
	}
	if len(results) == 0 {
		return "", "", nil // no match — not an error
	}
	return results[0].Lat, results[0].Lon, nil
}

// StartGeocoder runs a background routine to find coordinates for textual
// locations using the OpenStreetMap Nominatim API.
//
// A location that cannot be geocoded leaves lat/lon NULL and bumps
// geocode_attempts. It used to be written as lat=lon=0.000001 — a real point
// in the Gulf of Guinea — purely to keep the "WHERE lat IS NULL" query from
// returning it again. Every consumer then had to know to filter that sentinel
// out, and one that did not would plot accidents off the coast of Africa.
func StartGeocoder(db *sql.DB) {
	go func() {
		log.Println("Starting Background Geocoder...")

		client := &http.Client{Timeout: 10 * time.Second}

		for {
			var id int
			var location string
			err := db.QueryRow(`
				SELECT id, location
				FROM accidents
				WHERE lat IS NULL
				  AND geocode_attempts < 3
				  AND location != ''
				  AND location != 'Unknown'
				  AND location != '-'
				LIMIT 1
			`).Scan(&id, &location)

			if err == sql.ErrNoRows {
				time.Sleep(30 * time.Second)
				continue
			} else if err != nil {
				log.Printf("Geocoder DB error: %v", err)
				time.Sleep(5 * time.Second)
				continue
			}

			lat, lon, err := geocodeOne(client, location)
			switch {
			case err != nil:
				log.Printf("Nominatim request error [ID %d]: %v", id, err)
				if _, e := db.Exec(
					`UPDATE accidents SET geocode_attempts = geocode_attempts + 1 WHERE id = ?`,
					id); e != nil {
					log.Printf("Geocoder DB error: %v", e)
				}
			case lat == "":
				// Not found. Count the attempt so the row retires from the
				// queue, but leave lat/lon NULL: "we do not know" is the truth.
				if _, e := db.Exec(
					`UPDATE accidents SET geocode_attempts = geocode_attempts + 1 WHERE id = ?`,
					id); e != nil {
					log.Printf("Geocoder DB error: %v", e)
				}
			default:
				if _, e := db.Exec(
					`UPDATE accidents SET lat = ?, lon = ? WHERE id = ?`, lat, lon, id); e != nil {
					log.Printf("Geocoder DB error: %v", e)
				} else {
					log.Printf("Geocoded [ID %d]: %s -> %s, %s", id, location, lat, lon)
				}
			}

			// Respect the Nominatim acceptable use policy (1 request per second absolute maximum)
			time.Sleep(1500 * time.Millisecond)
		}
	}()
}
