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

// ScrapeASN begins the scraping process for ASN using a headless browser.
func ScrapeASN(db *sql.DB, startYear, endYear int) {
	fmt.Println("Initializing Headless Browser for ASN...")
	
	// Launch browser with stealth settings
	path, _ := launcher.LookPath()
	u := launcher.New().Bin(path).Headless(true).MustLaunch()
	browser := rod.New().ControlURL(u).MustConnect()
	defer browser.MustClose()

	// ASN paginates a year's records into ~100/page. Walk pages until we
	// hit an empty / vanishing table; cap at maxPages so a layout regression
	// doesn't loop forever. Earlier code hard-coded page=1 only, which
	// caps any single-year scrape at 100 records — modern years (2024-26)
	// regularly publish 200-500 incidents.
	const maxPages = 15

	for year := startYear; year <= endYear; year++ {
		emptyStreak := 0
		for pageNum := 1; pageNum <= maxPages; pageNum++ {
			url := fmt.Sprintf("https://aviation-safety.net/database/year/%d/%d", year, pageNum)
			fmt.Printf("Visiting ASN (Headless): %s\n", url)

			page := stealth.MustPage(browser)
			err := page.Navigate(url)
			if err != nil {
				log.Printf("Failed to navigate ASN %d page %d: %v\n", year, pageNum, err)
				page.MustClose()
				break // give up on this year
			}

			page.MustWaitLoad()
			time.Sleep(4 * time.Second) // Cloudflare JS challenge window

			rows, err := page.Elements("table tbody tr")
			if err != nil || len(rows) == 0 {
				log.Printf("No rows on year %d page %d (CF blocked or last page).\n", year, pageNum)
				page.MustClose()
				break
			}

			savedThisPage := 0
			for _, row := range rows {
				class, err := row.Attribute("class")
				if err == nil && class != nil && *class == "header" {
					continue
				}

				cells, err := row.Elements("td")
				if err != nil || len(cells) < 5 {
					continue
				}

				date := strings.TrimSpace(cells[0].MustText())
				model := strings.TrimSpace(cells[1].MustText())

				operator := ""
				if len(cells) > 3 {
					operator = strings.TrimSpace(cells[3].MustText())
				}

				fatalities := ""
				if len(cells) > 4 {
					fatalities = strings.TrimSpace(cells[4].MustText())
				}

				location := ""
				if len(cells) > 5 {
					location = strings.TrimSpace(cells[5].MustText())
				}

				sourceURL := ""
				link, err := cells[0].Element("a")
				if err == nil {
					href, _ := link.Property("href")
					if !href.Nil() {
						sourceURL = href.String()
					}
				}

				if sourceURL == "" {
					sourceURL = url + "#" + date + "-" + model
				}

				if date != "" && model != "" {
					InsertAccident(db, Accident{
						Date:          date,
						AircraftModel: model,
						Operator:      operator,
						Fatalities:    fatalities,
						Location:      location,
						SourceURL:     sourceURL,
					})
					savedThisPage++
				}
			}

			page.MustClose()
			time.Sleep(3 * time.Second) // respectful delay between pages

			if savedThisPage == 0 {
				emptyStreak++
				if emptyStreak >= 2 {
					// Two consecutive empty pages — definitely past the end.
					break
				}
			} else {
				emptyStreak = 0
			}
		}
	}
}
