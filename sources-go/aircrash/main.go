package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"time"
)

func main() {
	startYear := flag.Int("start", 1980, "Year to start scraping from")
	endYear := flag.Int("end", time.Now().Year(), "Year to end scraping")
	dbPath := flag.String("db", "accidents.db", "Path to the SQLite database")
	serve := flag.Bool("serve", false, "Start the web server instead of scraping")
	wikidata := flag.Bool("wikidata", false, "Scrape global data from Wikidata")
	asn := flag.Bool("asn", true, "Scrape Aviation Safety Network")
	b3a := flag.Bool("b3a", false, "Scrape Bureau of Aircraft Accidents Archives")
	port := flag.String("port", ":8080", "Port for the web server")
	flag.Parse()

	db, err := InitDB(*dbPath)
	if err != nil {
		log.Fatalf("Failed to initialize database: %v", err)
	}
	defer db.Close()

	if *serve {
		fmt.Printf("Aviation Accident API & Web Server\n")
		fmt.Printf("Database: %s\n", *dbPath)
		fmt.Println("-------------------------------")
		StartServer(db, *port)
		return
	}

	fmt.Printf("Aviation Accident Scraper\n")
	fmt.Printf("Database: %s\n", *dbPath)
	fmt.Println("-------------------------------")
	fmt.Println("Database initialized successfully.")
	fmt.Println("Starting scrapers... (Press Ctrl+C to stop)")

	if *asn {
		fmt.Printf("-> Scraping ASN from %d to %d\n", *startYear, *endYear)
		ScrapeASN(db, *startYear, *endYear)
	}

	if *wikidata {
		fmt.Printf("-> Scraping Global Data from Wikidata\n")
		ScrapeWikidata(db)
	}

	if *b3a {
		fmt.Printf("-> Scraping B3A from %d to %d\n", *startYear, *endYear)
		ScrapeB3A(db, *startYear, *endYear)
	}

	fmt.Println("-------------------------------")
	fmt.Println("Scraping finished.")
	os.Exit(0)
}
