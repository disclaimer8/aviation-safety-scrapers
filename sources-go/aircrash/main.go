package main

import (
	"flag"
	"fmt"
	"log"
	"os"
)

func main() {
	dbPath := flag.String("db", "accidents.db", "Path to the SQLite database")
	serve := flag.Bool("serve", false, "Start the web server instead of scraping")
	wikidata := flag.Bool("wikidata", false, "Scrape global data from Wikidata")
	// Loopback by default. The dashboard and its API have no authentication,
	// and docker-compose used to publish them on host port 80. Pass an
	// explicit address (e.g. -addr 0.0.0.0:8080) to expose it deliberately,
	// behind something that does authenticate.
	port := flag.String("addr", "127.0.0.1:8080", "Listen address for the web server")
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

	if *wikidata {
		fmt.Printf("-> Scraping Global Data from Wikidata\n")
		if err := ScrapeWikidata(db); err != nil {
			// A failed scrape used to be logged and then followed by
			// "Scraping finished." and exit 0, so a timer saw a clean run.
			fmt.Fprintf(os.Stderr, "Wikidata scrape failed: %v\n", err)
			os.Exit(1)
		}
	}

	fmt.Println("-------------------------------")
	fmt.Println("Scraping finished.")
	os.Exit(0)
}
