package main

import (
	"database/sql"
	"fmt"
	"log"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

// Accident represents a single aviation accident record.
type Accident struct {
	ID             int    `json:"id"`
	NormalizedDate string `json:"-"`
	Date           string `json:"date"`
	AircraftModel  string `json:"aircraft_model"`
	Operator       string `json:"operator"`
	Fatalities     string `json:"fatalities"`
	Location       string `json:"location"`
	SourceURL      string `json:"source_url"` // can be comma-separated now
	Lat            float64 `json:"lat"`
	Lon            float64 `json:"lon"`
}

// NormalizeDate attempts to convert various date strings into YYYY-MM-DD.
func NormalizeDate(dateStr string) string {
	dateStr = strings.TrimSpace(dateStr)

	// Layout 1: "2 Jan 1980" or "02 Jan 1980"
	t, err := time.Parse("2 Jan 2006", dateStr)
	if err == nil {
		return t.Format("2006-01-02")
	}

	// Layout 2: "January 2, 1980" (Wikipedia style)
	t, err = time.Parse("January 2, 2006", dateStr)
	if err == nil {
		return t.Format("2006-01-02")
	}

	// Layout 3: "Jan 2, 1980"
	t, err = time.Parse("Jan 2, 2006", dateStr)
	if err == nil {
		return t.Format("2006-01-02")
	}

	// Fallback: return original if can't parse
	return dateStr
}

// InitDB sets up the SQLite database and creates the necessary tables and indexes.
func InitDB(filepath string) (*sql.DB, error) {
	db, err := sql.Open("sqlite3", filepath)
	if err != nil {
		return nil, err
	}

	createTableQuery := `
	CREATE TABLE IF NOT EXISTS accidents (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		normalized_date TEXT,
		date TEXT,
		aircraft_model TEXT,
		operator TEXT,
		fatalities TEXT,
		location TEXT,
		source_url TEXT,
		lat REAL,
		lon REAL
	);`

	_, err = db.Exec(createTableQuery)
	if err != nil {
		return nil, fmt.Errorf("failed to create table: %w", err)
	}

	// Try to alter table if it already exists from previous versions
	db.Exec(`ALTER TABLE accidents ADD COLUMN lat REAL;`)
	db.Exec(`ALTER TABLE accidents ADD COLUMN lon REAL;`)

	// Create Indexes for performance
	indexes := []string{
		`CREATE INDEX IF NOT EXISTS idx_aircraft ON accidents(aircraft_model);`,
		`CREATE INDEX IF NOT EXISTS idx_operator ON accidents(operator);`,
		`CREATE INDEX IF NOT EXISTS idx_date ON accidents(date);`,
	}
	for _, idx := range indexes {
		db.Exec(idx)
	}

	return db, nil
}

// getFirstWord is a helper for fuzzy matching.
func getFirstWord(s string) string {
	words := strings.Fields(s)
	if len(words) > 0 {
		return words[0]
	}
	return ""
}

// InsertAccident inserts a new accident record into the database, with deduplication logic.
func InsertAccident(db *sql.DB, accident Accident) error {
	accident.NormalizedDate = NormalizeDate(accident.Date)

	// Simple fuzzy logic: find record on same day where model or operator shares the first word.
	modelQuery := "%" + getFirstWord(accident.AircraftModel) + "%"
	opQuery := "%" + getFirstWord(accident.Operator) + "%"

	// Skip fuzzy match if queries are just "%" (empty string)
	if modelQuery == "%%" && opQuery == "%%" {
		return insertNew(db, accident)
	}

	query := `SELECT id, source_url FROM accidents WHERE normalized_date = ? AND (aircraft_model LIKE ? OR operator LIKE ?) LIMIT 1`
	row := db.QueryRow(query, accident.NormalizedDate, modelQuery, opQuery)

	var existingID int
	var existingURL string
	err := row.Scan(&existingID, &existingURL)

	if err == sql.ErrNoRows {
		// Not a duplicate
		return insertNew(db, accident)
	} else if err != nil {
		return err
	}

	// Duplicate found! Append URL if not present.
	if !strings.Contains(existingURL, accident.SourceURL) {
		newURL := existingURL + "," + accident.SourceURL
		updateSQL := `UPDATE accidents SET source_url = ? WHERE id = ?`
		_, err = db.Exec(updateSQL, newURL, existingID)
		if err != nil {
			return err
		}
		log.Printf("Merged duplicate: %s - %s", accident.NormalizedDate, accident.AircraftModel)
	} else {
		// Completely identical record from same source
		log.Printf("Ignored exact duplicate: %s", accident.SourceURL)
	}

	return nil
}

func insertNew(db *sql.DB, accident Accident) error {
	insertSQL := `INSERT INTO accidents(normalized_date, date, aircraft_model, operator, fatalities, location, source_url, lat, lon) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
	statement, err := db.Prepare(insertSQL)
	if err != nil {
		return err
	}
	defer statement.Close()

	_, err = statement.Exec(accident.NormalizedDate, accident.Date, accident.AircraftModel, accident.Operator, accident.Fatalities, accident.Location, accident.SourceURL, accident.Lat, accident.Lon)
	if err != nil {
		return err
	}
	log.Printf("Saved new: %s - %s (%s)", accident.Date, accident.AircraftModel, accident.Operator)
	return nil
}

// GetAccidents retrieves accidents with pagination for the API.
func GetAccidents(db *sql.DB, limit, offset int) ([]Accident, error) {
	query := `SELECT id, date, aircraft_model, operator, fatalities, location, source_url, COALESCE(lat, 0), COALESCE(lon, 0) FROM accidents ORDER BY normalized_date DESC, id DESC LIMIT ? OFFSET ?`
	rows, err := db.Query(query, limit, offset)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var accidents []Accident
	for rows.Next() {
		var a Accident
		if err := rows.Scan(&a.ID, &a.Date, &a.AircraftModel, &a.Operator, &a.Fatalities, &a.Location, &a.SourceURL, &a.Lat, &a.Lon); err != nil {
			log.Println("Error scanning row:", err)
			continue
		}
		accidents = append(accidents, a)
	}
	return accidents, nil
}

// StatResult represents an analytical row.
type StatResult struct {
	Name        string `json:"name"`
	Count       int    `json:"count"`
	Fatalities  int    `json:"fatalities"`
}

// GetAircraftStats calculates top aircrafts by accident count.
func GetAircraftStats(db *sql.DB) ([]StatResult, error) {
	query := `
		SELECT aircraft_model, COUNT(id) as c, SUM(CAST(fatalities AS INTEGER)) as f
		FROM accidents
		WHERE aircraft_model != '' AND aircraft_model IS NOT NULL
		GROUP BY aircraft_model
		ORDER BY c DESC
		LIMIT 10
	`
	rows, err := db.Query(query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var stats []StatResult
	for rows.Next() {
		var s StatResult
		if err := rows.Scan(&s.Name, &s.Count, &s.Fatalities); err != nil {
			continue
		}
		stats = append(stats, s)
	}
	return stats, nil
}

// GetOperatorStats calculates top operators by accident count.
func GetOperatorStats(db *sql.DB) ([]StatResult, error) {
	query := `
		SELECT operator, COUNT(id) as c, SUM(CAST(fatalities AS INTEGER)) as f
		FROM accidents
		WHERE operator != '' AND operator IS NOT NULL
		GROUP BY operator
		ORDER BY c DESC
		LIMIT 10
	`
	rows, err := db.Query(query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var stats []StatResult
	for rows.Next() {
		var s StatResult
		if err := rows.Scan(&s.Name, &s.Count, &s.Fatalities); err != nil {
			continue
		}
		stats = append(stats, s)
	}
	return stats, nil
}
