package foreignsearch

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"
)

const (
	beaBase           = "https://www.bea.aero"
	beaListingURL     = "https://www.bea.aero/en/investigation-reports/notified-events/?tx_news_pi1%5Baction%5D=searchResult&tx_news_pi1%5Bcontroller%5D=News&tx_news_pi1%5BfacetAction%5D=removeAll&cHash=2f2908414ec2726192a2da91db42f658"
	beaDetailPathPfx  = "/en/investigation-reports/notified-events/detail/"
)

// reDateInTitle matches the date embedded in BEA event titles, e.g. "on 14/06/2026 at".
var reDateInTitle = regexp.MustCompile(`\bon (\d{2})/(\d{2})/(\d{4})\b`)

// reDetailHref matches an href to a BEA notified-events detail page, capturing the slug.
// The href form is: /en/investigation-reports/notified-events/detail/SLUG/
var reDetailHref = regexp.MustCompile(`href="(/en/investigation-reports/notified-events/detail/([^/"]+)/)"`)

// parseBEA extracts ForeignRecord entries from a BEA investigation-listing HTML page.
//
// The page structure (captured from the live search-result URL) is:
//
//	<article class="search-entry">
//	  <h1 class="search-entry__title">
//	    <a href="/en/investigation-reports/notified-events/detail/SLUG/"
//	       title="TITLE TEXT">
//	      TITLE TEXT
//	    </a>
//	  </h1>
//	</article>
//
// Date is embedded in the title text as "on DD/MM/YYYY".
// ForeignRef is the stable slug segment from the detail URL.
// OriginalURL is the absolute URL (beaBase + path).
//
// Entries missing a link or title are skipped and counted in warnings.
// An empty body (no entries) returns (nil, n, nil) — not an error.
func parseBEA(raw []byte) (recs []ForeignRecord, warnings int, err error) {
	// Scan line by line for <article class="search-entry"> blocks.
	// Each block spans until the closing </article>.
	content := string(raw)

	const articleOpen = `<article class="search-entry">`
	const articleClose = `</article>`

	for {
		startIdx := strings.Index(content, articleOpen)
		if startIdx < 0 {
			break
		}
		endIdx := strings.Index(content[startIdx:], articleClose)
		if endIdx < 0 {
			// Unterminated article — skip rest.
			break
		}
		block := content[startIdx : startIdx+endIdx+len(articleClose)]
		content = content[startIdx+endIdx+len(articleClose):]

		rec, ok := beaParseBlock(block)
		if !ok {
			warnings++
			continue
		}
		recs = append(recs, rec)
	}
	return recs, warnings, nil
}

// beaParseBlock extracts a ForeignRecord from a single <article class="search-entry"> block.
// Returns (rec, true) on success; (zero, false) when required fields are absent.
func beaParseBlock(block string) (ForeignRecord, bool) {
	// Extract detail href and slug.
	hrefMatch := reDetailHref.FindStringSubmatch(block)
	if hrefMatch == nil {
		return ForeignRecord{}, false
	}
	path := hrefMatch[1] // e.g. /en/investigation-reports/notified-events/detail/SLUG/
	slug := hrefMatch[2] // e.g. accident-to-the-helicopters-...

	originalURL := beaBase + path

	// Extract the title from the anchor text (between > and </a>).
	// The href pattern already gives us the start of the <a> tag — find its text.
	title := beaExtractAnchorText(block, path)
	if title == "" {
		// Fall back to title attribute.
		title = beaExtractAttr(block, "title")
	}
	title = strings.TrimSpace(title)
	if title == "" {
		return ForeignRecord{}, false
	}

	// Extract occurrence date from title text: "on DD/MM/YYYY".
	occDate := ""
	if m := reDateInTitle.FindStringSubmatch(title); m != nil {
		// m[1]=DD, m[2]=MM, m[3]=YYYY → reformat to yyyy-mm-dd
		occDate = m[3] + "-" + m[2] + "-" + m[1]
	}

	return ForeignRecord{
		ForeignRef:     slug,
		Title:          title,
		OccurrenceDate: occDate,
		OriginalURL:    originalURL,
	}, true
}

// beaExtractAnchorText returns the trimmed text content of the first <a href="PATH"…> … </a>
// in block that contains the given path.
func beaExtractAnchorText(block, path string) string {
	needle := `href="` + path + `"`
	idx := strings.Index(block, needle)
	if idx < 0 {
		return ""
	}
	// Advance past the closing > of the opening <a> tag.
	closeAngle := strings.IndexByte(block[idx:], '>')
	if closeAngle < 0 {
		return ""
	}
	afterTag := block[idx+closeAngle+1:]
	// Everything up to </a>.
	endIdx := strings.Index(afterTag, "</a>")
	if endIdx < 0 {
		return ""
	}
	raw := afterTag[:endIdx]
	// Strip any nested HTML tags (e.g. <p> disclaimers inside the <a>).
	raw = stripHTMLTags(raw)
	return strings.TrimSpace(raw)
}

// beaExtractAttr returns the value of the first occurrence of attrName="VALUE" in s.
func beaExtractAttr(s, attrName string) string {
	needle := attrName + `="`
	idx := strings.Index(s, needle)
	if idx < 0 {
		return ""
	}
	rest := s[idx+len(needle):]
	end := strings.IndexByte(rest, '"')
	if end < 0 {
		return ""
	}
	return rest[:end]
}

// stripHTMLTags removes all <…> tags from s and collapses whitespace.
func stripHTMLTags(s string) string {
	var b bytes.Buffer
	inTag := false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c == '<' {
			inTag = true
			continue
		}
		if c == '>' {
			inTag = false
			continue
		}
		if !inTag {
			b.WriteByte(c)
		}
	}
	// Collapse runs of whitespace (including newlines).
	out := b.String()
	parts := strings.Fields(out)
	return strings.Join(parts, " ")
}

// beaClient implements AuthorityClient against the live BEA notified-events listing.
type beaClient struct {
	http *http.Client
}

// NewBEAClient returns an AuthorityClient backed by the live BEA website.
func NewBEAClient(timeout time.Duration) AuthorityClient {
	return &beaClient{http: &http.Client{Timeout: timeout}}
}

// Search fetches the BEA notified-events listing and returns all records.
// countryISO2 is accepted for interface compliance; BEA listing is not filtered
// per country in the current implementation (all notified events are returned).
func (c *beaClient) Search(ctx context.Context, countryISO2 string) ([]ForeignRecord, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, beaListingURL, nil)
	if err != nil {
		return nil, fmt.Errorf("foreignsearch: beaClient: build request: %w", err)
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (compatible; aviation-safety-scrapers/1.0)")
	req.Header.Set("Accept", "text/html,application/xhtml+xml")

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("foreignsearch: beaClient: do request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("foreignsearch: beaClient: unexpected status %d", resp.StatusCode)
	}

	var buf bytes.Buffer
	if _, err := buf.ReadFrom(resp.Body); err != nil {
		return nil, fmt.Errorf("foreignsearch: beaClient: read body: %w", err)
	}

	recs, _, err := parseBEA(buf.Bytes())
	return recs, err
}
