package main

import (
	"database/sql"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/launcher"
	"github.com/go-rod/stealth"
)

// ScrapeB3A begins the scraping process for B3A archives using a headless browser.
func ScrapeB3A(db *sql.DB, startYear, endYear int) {
	fmt.Println("Initializing Headless Browser for B3A...")
	
	path, _ := launcher.LookPath()
	u := launcher.New().Bin(path).Headless(true).MustLaunch()
	browser := rod.New().ControlURL(u).MustConnect()
	defer browser.MustClose()

	for year := startYear; year <= endYear; year++ {
		url := fmt.Sprintf("https://www.baaa-acro.com/crash-archives?year=%d", year)
		fmt.Printf("Visiting B3A (Headless): %s\n", url)
		
		page := stealth.MustPage(browser)
		
		err := page.Navigate(url)
		if err != nil {
			log.Printf("Failed to navigate B3A %d: %v\n", year, err)
			page.MustClose()
			continue
		}

		page.MustWaitLoad()
		time.Sleep(3 * time.Second)

		// B3A uses a view with rows. Let's try to extract all links to individual crash pages
		links, err := page.Elements("a[href*='/crash/crash-']")
		if err != nil {
			log.Printf("No crash links found for B3A year %d\n", year)
			page.MustClose()
			continue
		}

		for _, link := range links {
			href, err := link.Property("href")
			if err != nil || href.Nil() {
				continue
			}
			
			sourceURL := href.String()
			
			// We can parse the URL itself as a fallback if we don't visit every page.
			// Example: https://www.baaa-acro.com/crash/crash-cessna-208b-grand-caravan-guyana-1-killed
			parts := strings.Split(sourceURL, "/crash/crash-")
			if len(parts) < 2 {
				continue
			}
			
			slug := parts[1]
			// A very naive parsing of the slug:
			// "cessna-208b-grand-caravan-guyana-1-killed"
			slugParts := strings.Split(slug, "-")
			model := ""
			if len(slugParts) > 0 {
				model = strings.Title(slugParts[0]) // e.g. "Cessna"
			}
			
			accident := Accident{
				Date:          fmt.Sprintf("%d-01-01", year), // Fallback date
				AircraftModel: model + " (B3A)",
				Operator:      "",
				Fatalities:    "",
				Location:      "Unknown",
				SourceURL:     sourceURL,
			}
			InsertAccident(db, accident)
		}

		page.MustClose()
		time.Sleep(3 * time.Second)
	}
}
