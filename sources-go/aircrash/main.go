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

	if *wikidata {
		fmt.Printf("-> Scraping Global Data from Wikidata\n")
		ScrapeWikidata(db)
	}

	fmt.Println("-------------------------------")
	fmt.Println("Scraping finished.")
	os.Exit(0)
}
