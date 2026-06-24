package manufacturer

// ParseSafetyFirstListing is a pure, fixture-driven parser for the Airbus
// Safety First magazine listing at https://safetyfirst.airbus.com/magazine/.
//
// The listing is a WordPress/Elementor SSR page; each magazine issue is
// rendered as an <a> anchor with class "category-magazine" that links directly
// to a PDF stored on S3.  Because there is no text title inside the anchor,
// the title is synthesised from the PDF filename.
//
// Parsing strategy mirrors internal/worker/regional/parsehtml.go:
//   - stdlib-only regex (no external HTML parser);
//   - scheme guard: only http/https resolved URLs pass through;
//   - deduplication by IssueRef.

import (
	"fmt"
	"html"
	"net/url"
	"regexp"
	"strings"
)

var (
	// magazineAnchorRe matches <a … class="…category-magazine…" …href="…"…>
	// in either href-before-class or class-before-href attribute order.
	// We capture the href value.  The (?is) flags give case-insensitive + dot-
	// matches-newline so the full multi-line block is consumed by .*? inside the
	// outer anchor (we only need the href from the opening tag itself).
	magazineHrefRe = regexp.MustCompile(
		`(?i)<a\b[^>]*\bclass\s*=\s*"[^"]*\bcategory-magazine\b[^"]*"[^>]*\bhref\s*=\s*"([^"]+)"[^>]*>|` +
			`(?i)<a\b[^>]*\bhref\s*=\s*"([^"]+)"[^>]*\bclass\s*=\s*"[^"]*\bcategory-magazine\b[^"]*"[^>]*>`,
	)

	// issueNumRe extracts a trailing issue number from a filename stem such as
	// "safety_first_41" → "41" or "safety_first_09" → "9".
	issueNumRe = regexp.MustCompile(`_(\d+)$`)

	// specialEdRe detects the special-edition naming convention and captures the
	// descriptive slug after "special_edition_-_".
	specialEdRe = regexp.MustCompile(`_special_edition_-_(.+)$`)
)

// ParseSafetyFirstListing parses the HTML listing page and returns one
// ManufacturerRecord per magazine issue found.  baseURL is used to resolve any
// relative hrefs (in practice all hrefs are already absolute).
func ParseSafetyFirstListing(page []byte, baseURL string) ([]ManufacturerRecord, error) {
	base, err := url.Parse(baseURL)
	if err != nil {
		return nil, fmt.Errorf("manufacturer: parse baseURL %q: %w", baseURL, err)
	}

	seen := map[string]bool{}
	var recs []ManufacturerRecord

	for _, m := range magazineHrefRe.FindAllSubmatch(page, -1) {
		// The alternation gives two capture groups; exactly one will be non-empty.
		rawHref := string(m[1])
		if rawHref == "" {
			rawHref = string(m[2])
		}
		// Decode HTML entities (e.g. &amp; in query strings) before parsing —
		// parity with regional/parsehtml.go.
		rawHref = strings.TrimSpace(html.UnescapeString(rawHref))
		if rawHref == "" {
			continue
		}

		u, err := url.Parse(rawHref)
		if err != nil {
			continue
		}
		resolved := base.ResolveReference(u)
		// Scheme guard — same pattern as regional/parsehtml.go.
		if resolved.Scheme != "http" && resolved.Scheme != "https" {
			continue
		}
		absURL := resolved.String()

		ref, title := refAndTitle(resolved)
		if ref == "" || title == "" {
			continue
		}
		if seen[ref] {
			continue
		}
		seen[ref] = true

		rec := ManufacturerRecord{
			IssueRef:    ref,
			Title:       title,
			OriginalURL: absURL,
		}
		if strings.HasSuffix(strings.ToLower(resolved.Path), ".pdf") {
			rec.ReportURL = absURL
		}
		recs = append(recs, rec)
	}
	return recs, nil
}

// refAndTitle derives (IssueRef, Title) from a resolved PDF URL.
//
// URL path examples:
//
//	/pdf/safety+first/safety_first_41.pdf           → ref="41",  title="Safety First #41"
//	/pdf/safety+first/safety_first_09.pdf           → ref="9",   title="Safety First #9"
//	/pdf/safety+first/safety_first_special_edition_-_control_your_speed.pdf
//	                                                 → ref="special_edition_-_control_your_speed"
//	                                                   title="Safety First: Control Your Speed"
func refAndTitle(u *url.URL) (ref, title string) {
	// Last path segment, strip known .pdf extension.
	p := u.Path
	p = strings.TrimSuffix(strings.ToLower(p), ".pdf")
	if idx := strings.LastIndex(p, "/"); idx >= 0 {
		p = p[idx+1:]
	}
	// URL-decode '+' and '%20' in path.
	p = strings.ReplaceAll(p, "+", " ")
	p, _ = url.PathUnescape(p)
	// Normalise back to underscore-joined slug for matching.
	slug := strings.ReplaceAll(p, " ", "_")

	if m := specialEdRe.FindStringSubmatch(slug); m != nil {
		descSlug := m[1]
		ref = "special_edition_-_" + descSlug
		title = "Safety First: " + humanizeSlug(descSlug)
		return
	}
	if m := issueNumRe.FindStringSubmatch(slug); m != nil {
		// Strip any leading zeros so "09" becomes "9".
		num := strings.TrimLeft(m[1], "0")
		if num == "" {
			num = "0"
		}
		ref = num
		title = "Safety First #" + num
		return
	}
	// Fallback: use the whole slug as ref and a generic title.
	if slug != "" {
		ref = slug
		title = "Safety First: " + humanizeSlug(slug)
	}
	return
}

// humanizeSlug converts an underscore-joined lowercase slug to a title-cased
// human-readable string.  Example: "control_your_speed" → "Control Your Speed".
func humanizeSlug(s string) string {
	words := strings.Split(s, "_")
	for i, w := range words {
		if len(w) == 0 {
			continue
		}
		words[i] = strings.ToUpper(w[:1]) + w[1:]
	}
	return strings.Join(words, " ")
}
