// Package aia parses and imports the ICAO Accident Investigation Authorities
// (AIA) contact directory. The directory is a single HTML page that lists, per
// State, the national authority responsible for accident investigation together
// with its contact details, website, and any delegation to another State or
// regional body. There is no machine-readable feed, so the importer reads the
// HTML structure directly.
//
// The real page renders the whole directory as ONE HTML <table> with two columns
// per row — Country | Address. The first column carries the State label (often
// with a "(DT)" Dependent-territory or "(NCS)" Non-Contracting-State marker); the
// second column carries the full raw contact block (authority name(s), postal
// address, Tel/Fax/AFTN lines, an optional website, and obfuscated emails).
//
// The parser is deliberately tolerant: malformed or incomplete rows never crash
// it. Instead it preserves the complete raw block and emits Warnings so the
// importer can stage every record and let an operator review the gaps.
package aia

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"regexp"
	"strings"
	"time"

	"golang.org/x/net/html"
	"golang.org/x/net/html/atom"
)

// Record is one parsed State row from the AIA directory. Every field except the
// identity (CountryLabel) is best-effort; an empty value simply means the page
// did not carry that datum for the State. RawContact always holds the complete
// text of the Address cell so nothing is silently lost.
type Record struct {
	CountryLabel     string
	AuthorityName    string
	RawContact       string
	Emails           []string
	Phones           []string
	WebsiteURL       string
	ArchiveURL       string
	ReferenceCountry string // "Refer to X" — delegation to another State
	ReferenceBody    string // "See Y" — delegation to a regional body
	UpdatedAt        *time.Time
	Warnings         []string
	Checksum         string
}

var (
	emailRe   = regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`)
	phoneRe   = regexp.MustCompile(`(?i)\b(?:tel|phone|fax|mobile)\b\.?[:\s]*\+?[0-9(][0-9()\-.\s/]{5,}`)
	urlRe     = regexp.MustCompile(`https?://[^\s<>")]+`)
	referToRe = regexp.MustCompile(`(?im)^\s*refer\s+to\s+(?:the\s+)?(.+?)\s*$`)
	seeBodyRe = regexp.MustCompile(`(?i)^\s*see\s+(.+?)\s*(?:\n|$)`)
	updatedRe = regexp.MustCompile(`(?i)updated[:\s]*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})`)
)

// Parse reads the AIA directory HTML and returns one Record per data row of the
// Country/Address directory table. It never returns an error for malformed
// individual rows; a returned error means the document itself could not be
// parsed as HTML.
func Parse(r io.Reader) ([]Record, error) {
	root, err := html.Parse(r)
	if err != nil {
		return nil, fmt.Errorf("parse aia html: %w", err)
	}

	table := findDirectoryTable(root)
	if table == nil {
		return nil, fmt.Errorf("aia: directory table (Country/Address) not found")
	}

	var records []Record
	for _, row := range tableRows(table) {
		cells := rowCells(row)
		if len(cells) < 2 {
			continue
		}
		country := cleanText(cellText(cells[0]))
		if country == "" {
			continue // spacer / empty row
		}
		if isHeaderRow(country, cleanText(cellText(cells[1]))) {
			continue
		}
		records = append(records, buildRecord(country, cells[1]))
	}

	return records, nil
}

// findDirectoryTable returns the directory <table> — the one whose header row is
// Country | Address. It walks every table so the parser is robust to the page's
// surrounding chrome. If no header matches, it falls back to the first table.
func findDirectoryTable(root *html.Node) *html.Node {
	var first, match *html.Node
	var walk func(*html.Node)
	walk = func(n *html.Node) {
		if n.Type == html.ElementNode && n.DataAtom == atom.Table {
			if first == nil {
				first = n
			}
			if match == nil && tableHasCountryAddressHeader(n) {
				match = n
			}
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(root)
	if match != nil {
		return match
	}
	return first
}

// tableHasCountryAddressHeader reports whether the table's first row reads
// Country | Address (in either <th> or <td>).
func tableHasCountryAddressHeader(table *html.Node) bool {
	rows := tableRows(table)
	if len(rows) == 0 {
		return false
	}
	cells := rowCells(rows[0])
	if len(cells) < 2 {
		return false
	}
	return isHeaderRow(cleanText(cellText(cells[0])), cleanText(cellText(cells[1])))
}

func isHeaderRow(col0, col1 string) bool {
	return strings.EqualFold(col0, "Country") && strings.EqualFold(col1, "Address")
}

// tableRows returns every <tr> under a table (across thead/tbody).
func tableRows(table *html.Node) []*html.Node {
	var rows []*html.Node
	var walk func(*html.Node)
	walk = func(n *html.Node) {
		if n.Type == html.ElementNode && n.DataAtom == atom.Tr {
			rows = append(rows, n)
			return
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(table)
	return rows
}

// rowCells returns the direct <td>/<th> children of a row, in order.
func rowCells(row *html.Node) []*html.Node {
	var cells []*html.Node
	for c := row.FirstChild; c != nil; c = c.NextSibling {
		if c.Type == html.ElementNode && (c.DataAtom == atom.Td || c.DataAtom == atom.Th) {
			cells = append(cells, c)
		}
	}
	return cells
}

// buildRecord distils one Address cell into a Record for the given country label.
func buildRecord(country string, addrCell *html.Node) Record {
	rec := Record{
		CountryLabel: country,
		RawContact:   strings.TrimSpace(collapseBlankLines(cellText(addrCell))),
	}
	// text is the cleaned, nbsp-/zero-width-free, line-preserving body that the
	// contact regexes run against. (Go's RE2 \s is ASCII-only, so matching the raw
	// cell text would miss labels separated from values by a non-breaking space.)
	text := rec.RawContact

	// Emails: prefer the structured spamspan markup (user/domain spans), which is
	// unambiguous; fall back to plain-text and explicit [at]/[dot] deobfuscation.
	rec.Emails = extractEmails(addrCell, text)

	// Delegations: a "Refer to X" or "See Y" row delegates and carries no
	// canonical authority. Detect these before extracting contact data so a
	// delegation never produces a spurious authority name.
	if m := referToRe.FindStringSubmatch(text); m != nil {
		rec.ReferenceCountry = cleanText(m[1])
	}
	if m := seeBodyRe.FindStringSubmatch(text); m != nil {
		rec.ReferenceBody = cleanText(m[1])
	}

	// Phones.
	seenPhone := map[string]bool{}
	for _, m := range phoneRe.FindAllString(text, -1) {
		p := cleanPhone(m)
		if p != "" && !seenPhone[p] {
			seenPhone[p] = true
			rec.Phones = append(rec.Phones, p)
		}
	}

	// Website / archive URL: first http(s) URL in the block.
	if m := urlRe.FindString(text); m != "" {
		rec.WebsiteURL = strings.Trim(m, ".,;)")
	}

	// ICAO update date — the page writes "Updated D Month YYYY"; ISO is accepted
	// too for resilience.
	if m := updatedRe.FindStringSubmatch(text); m != nil {
		if ts, ok := parseUpdated(m[1]); ok {
			rec.UpdatedAt = &ts
		} else {
			rec.Warnings = append(rec.Warnings, "unparsable ICAO update date: "+m[1])
		}
	}

	// Authority name: the first substantive line that is not a delegation marker
	// or a pure contact/metadata line. Skipped entirely for delegations.
	if rec.ReferenceCountry == "" && rec.ReferenceBody == "" {
		rec.AuthorityName = guessAuthorityName(rec.RawContact)
	}

	if rec.AuthorityName == "" && rec.ReferenceCountry == "" && rec.ReferenceBody == "" {
		rec.Warnings = append(rec.Warnings, "no authority name or delegation found in block")
	}

	rec.Checksum = checksum(rec)
	return rec
}

// extractEmails pulls clean addresses out of an Address cell. ICAO obfuscates
// emails as <span class="spamspan"><span class="u">user</span> [at]
// <span class="d">domain.tld</span><span class="t">(noise)</span></span>; the t
// span is a redundant [at]/[dot] hint we must drop or it yields garbage. We read
// the u+d spans directly, then fall back to plain text / explicit [at]/[dot].
func extractEmails(cell *html.Node, raw string) []string {
	seen := map[string]bool{}
	var out []string
	add := func(e string) {
		e = strings.ToLower(strings.Trim(strings.TrimSpace(e), ".,;:"))
		if e == "" || seen[e] || !emailRe.MatchString(e) {
			return
		}
		seen[e] = true
		out = append(out, e)
	}

	// Structured spamspan: combine the .u (user) and .d (domain) spans.
	var walkSpam func(*html.Node)
	walkSpam = func(n *html.Node) {
		if n.Type == html.ElementNode && n.DataAtom == atom.Span && hasClass(n, "spamspan") {
			user := spanByClass(n, "u")
			domain := spanByClass(n, "d")
			if user != "" && domain != "" {
				add(user + "@" + domain)
			}
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walkSpam(c)
		}
	}
	walkSpam(cell)

	// mailto: links (rare but explicit).
	var walkMailto func(*html.Node)
	walkMailto = func(n *html.Node) {
		if n.Type == html.ElementNode && n.DataAtom == atom.A {
			for _, a := range n.Attr {
				if a.Key == "href" && strings.HasPrefix(strings.ToLower(a.Val), "mailto:") {
					add(strings.TrimPrefix(a.Val[len("mailto:"):], ""))
				}
			}
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walkMailto(c)
		}
	}
	walkMailto(cell)

	// Plain-text and explicit [at]/[dot] forms anywhere not already captured.
	for _, m := range emailRe.FindAllString(deobfuscate(raw), -1) {
		add(m)
	}
	return out
}

// hasClass reports whether an element node carries the given class token.
func hasClass(n *html.Node, want string) bool {
	for _, a := range n.Attr {
		if a.Key == "class" {
			for _, f := range strings.Fields(a.Val) {
				if f == want {
					return true
				}
			}
		}
	}
	return false
}

// spanByClass returns the trimmed text of the first descendant <span> with the
// given exact class token.
func spanByClass(n *html.Node, class string) string {
	var found string
	var walk func(*html.Node) bool
	walk = func(x *html.Node) bool {
		if x.Type == html.ElementNode && x.DataAtom == atom.Span && hasClass(x, class) {
			found = strings.TrimSpace(nodeText(x))
			return true
		}
		for c := x.FirstChild; c != nil; c = c.NextSibling {
			if walk(c) {
				return true
			}
		}
		return false
	}
	walk(n)
	return found
}

// guessAuthorityName returns the first block line that looks like an authority
// name: not a delegation marker and not a pure contact/metadata line.
func guessAuthorityName(raw string) string {
	for _, ln := range strings.Split(raw, "\n") {
		ln = strings.TrimSpace(ln)
		low := strings.ToLower(ln)
		switch {
		case ln == "":
			continue
		case strings.HasPrefix(low, "refer to"), strings.HasPrefix(low, "see "):
			continue
		case strings.HasPrefix(low, "tel"), strings.HasPrefix(low, "fax"),
			strings.HasPrefix(low, "phone"), strings.HasPrefix(low, "mobile"),
			strings.HasPrefix(low, "email"), strings.HasPrefix(low, "e-mail"),
			strings.HasPrefix(low, "e-mails"), strings.HasPrefix(low, "emails"),
			strings.HasPrefix(low, "website"), strings.HasPrefix(low, "updated"),
			strings.HasPrefix(low, "aftn"), strings.HasPrefix(low, "sita"),
			strings.HasPrefix(low, "cable"), strings.HasPrefix(low, "telex"),
			strings.HasPrefix(low, "address:"),
			strings.HasPrefix(low, "accident notification"),
			strings.HasPrefix(low, "general enquiries"):
			continue
		case emailRe.MatchString(ln) || urlRe.MatchString(ln):
			continue
		case strings.Contains(low, "under review"), strings.Contains(low, "pending"):
			continue
		case len(ln) < 4:
			continue
		}
		return ln
	}
	return ""
}

// parseUpdated parses either "D Month YYYY" or ISO "YYYY-MM-DD".
func parseUpdated(s string) (time.Time, bool) {
	s = strings.TrimSpace(s)
	for _, layout := range []string{"2 January 2006", "2006-01-02"} {
		if ts, err := time.Parse(layout, s); err == nil {
			return ts, true
		}
	}
	return time.Time{}, false
}

// deobfuscate decodes explicit [at]/[dot] (and (at)/(dot)) obfuscation so plain
// inline addresses resolve. It does not repair any other malformed address.
func deobfuscate(s string) string {
	repl := strings.NewReplacer(
		" [at] ", "@", "[at]", "@",
		" [dot] ", ".", "[dot]", ".",
		" (at) ", "@", "(at)", "@",
		" (dot) ", ".", "(dot)", ".",
	)
	return repl.Replace(s)
}

// cleanPhone strips the leading label and trailing punctuation from a phone
// match, collapsing whitespace.
func cleanPhone(s string) string {
	s = regexp.MustCompile(`(?i)^(tel|phone|fax|mobile)\.?[:\s]*`).ReplaceAllString(s, "")
	s = strings.Trim(s, ".,;:/ ")
	return normalizeWhitespace(s)
}

// cellText returns the text content of a table cell with <br> and block-level
// elements rendered as newlines, so the per-line structure survives.
func cellText(n *html.Node) string {
	var sb strings.Builder
	var walk func(*html.Node)
	walk = func(x *html.Node) {
		switch x.Type {
		case html.TextNode:
			sb.WriteString(x.Data)
		case html.ElementNode:
			// Skip the redundant spamspan "(user[at]domain[dot]tld)" hint span; it
			// would otherwise deobfuscate into a duplicate garbage address.
			if x.DataAtom == atom.Span && hasClass(x, "t") {
				return
			}
			if x.DataAtom == atom.Br {
				sb.WriteString("\n")
				return
			}
			block := isBlock(x.DataAtom)
			if block {
				sb.WriteString("\n")
			}
			for c := x.FirstChild; c != nil; c = c.NextSibling {
				walk(c)
			}
			if block {
				sb.WriteString("\n")
			}
		}
	}
	walk(n)
	return sb.String()
}

// isBlock reports whether an element introduces a line break in rendered text.
func isBlock(a atom.Atom) bool {
	switch a {
	case atom.Div, atom.P, atom.Li, atom.Tr, atom.H1, atom.H2, atom.H3, atom.H4, atom.H5, atom.H6:
		return true
	}
	return false
}

// nodeText returns the concatenated text content of a node subtree.
func nodeText(n *html.Node) string {
	var sb strings.Builder
	var walk func(*html.Node)
	walk = func(x *html.Node) {
		if x.Type == html.TextNode {
			sb.WriteString(x.Data)
		}
		for c := x.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(n)
	return sb.String()
}

// cleanText trims and collapses an inline string to a single line. Used for
// labels and single-line captures.
func cleanText(s string) string {
	return normalizeWhitespace(strings.ReplaceAll(s, "\n", " "))
}

// normalizeWhitespace trims and collapses internal whitespace runs (including
// non-breaking and zero-width spaces) to single ASCII spaces.
func normalizeWhitespace(s string) string {
	s = strings.ReplaceAll(s, " ", " ") // nbsp
	s = strings.ReplaceAll(s, "​", "")  // zero-width space
	return strings.Join(strings.Fields(s), " ")
}

// collapseBlankLines trims each line and drops runs of blank lines so RawContact
// reads cleanly while preserving the one-line-per-datum structure.
func collapseBlankLines(s string) string {
	var lines []string
	prevBlank := false
	for _, ln := range strings.Split(s, "\n") {
		ln = strings.TrimSpace(strings.ReplaceAll(strings.ReplaceAll(ln, " ", " "), "​", ""))
		ln = strings.Join(strings.Fields(ln), " ")
		if ln == "" {
			if prevBlank {
				continue
			}
			prevBlank = true
		} else {
			prevBlank = false
		}
		lines = append(lines, ln)
	}
	return strings.Trim(strings.Join(lines, "\n"), "\n")
}

// checksum is a stable content hash of the identifying + canonical fields of a
// record, used as the staged_authorities record_checksum.
func checksum(r Record) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s\x00%s\x00%s\x00%s\x00%s\x00%s",
		r.CountryLabel, r.AuthorityName, r.WebsiteURL,
		strings.Join(r.Emails, ","), r.ReferenceCountry, r.ReferenceBody)
	return hex.EncodeToString(h.Sum(nil))
}
