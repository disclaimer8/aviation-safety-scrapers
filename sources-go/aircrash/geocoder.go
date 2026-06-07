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

// StartGeocoder runs a background routine to find coordinates for textual locations
// using the OpenStreetMap Nominatim API.
func StartGeocoder(db *sql.DB) {
	go func() {
		log.Println("Starting Background Geocoder...")
		
		// Create a custom HTTP client with timeout
		client := &http.Client{Timeout: 10 * time.Second}

		for {
			// Find one record that hasn't been geocoded yet
			// We check for lat IS NULL and we don't bother if location is empty or generic 'Unknown'
			var id int
			var location string
			
			// Note: We use lat = 0 as a flag that it's un-geocoded, or we can use NULL. 
			// Because SQLite REAL columns can be NULL, we check for that or 0.
			// Let's also exclude locations that failed before by setting them to a tiny specific value like 0.0001,
			// or we just rely on lat IS NULL. Since we altered table, default is NULL.
			err := db.QueryRow(`
				SELECT id, location 
				FROM accidents 
				WHERE lat IS NULL 
				  AND location != '' 
				  AND location != 'Unknown' 
				  AND location != '-'
				LIMIT 1
			`).Scan(&id, &location)

			if err == sql.ErrNoRows {
				// No more rows to geocode, sleep and check later
				time.Sleep(30 * time.Second)
				continue
			} else if err != nil {
				log.Printf("Geocoder DB error: %v", err)
				time.Sleep(5 * time.Second)
				continue
			}

			// Call Nominatim API
			geocodeURL := fmt.Sprintf("https://nominatim.openstreetmap.org/search?q=%s&format=json&limit=1", url.QueryEscape(location))
			
			req, err := http.NewRequest("GET", geocodeURL, nil)
			if err == nil {
				// Nominatim requires a user-agent to comply with their usage policy
				req.Header.Set("User-Agent", "AviationSafetyExplorer/1.0")
				
				resp, err := client.Do(req)
				if err == nil {
					defer resp.Body.Close()
					var results []NominatimResponse
					if err := json.NewDecoder(resp.Body).Decode(&results); err == nil && len(results) > 0 {
						// Success
						db.Exec(`UPDATE accidents SET lat = ?, lon = ? WHERE id = ?`, results[0].Lat, results[0].Lon, id)
						log.Printf("Geocoded [ID %d]: %s -> %s, %s", id, location, results[0].Lat, results[0].Lon)
					} else {
						// Not found or error parsing. Set lat/lon to 0.000001 to prevent infinite retries
						db.Exec(`UPDATE accidents SET lat = 0.000001, lon = 0.000001 WHERE id = ?`, id)
					}
				} else {
					log.Printf("Nominatim request error: %v", err)
				}
			}

			// Respect the Nominatim acceptable use policy (1 request per second absolute maximum)
			time.Sleep(1500 * time.Millisecond)
		}
	}()
}
