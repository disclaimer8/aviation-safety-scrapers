// Package raio parses and imports the ICAO list of Regional Accident and
// Incident Investigation Organizations (RAIOs) and Investigation Cooperation
// Mechanisms (ICMs). There is no machine-readable feed, so the parser reads the
// HTML structure directly.
//
// The real page renders the list as TWO HTML <table>s: the FIRST is the RAIOs,
// the SECOND is the Investigation Cooperation Mechanisms (ICMs). A body's class
// derives from which table it sits in — RAIO table → "raio", ICM table → "icm".
// Each data row has five columns: Organization | Description | Region |
// Member States | Website. The Member States cell carries the member list with
// any observer clause embedded inline ("+ Observers: ..." or
// "SPECIAL OBSERVERS: ..."); the Website cell carries an <a> link.
//
// The parser is deliberately tolerant: a malformed row never crashes it.
// Unparsable bits become Warnings so the importer can stage every body and let an
// operator review the gaps. Members and Observers hold the raw State labels
// exactly as written — resolution to seeded ISO countries happens in the
// importer, so the parser never silently drops an unknown label.
package raio

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"regexp"
	"strings"

	"golang.org/x/net/html"
	"golang.org/x/net/html/atom"
)

// BodyRecord is one parsed regional body. Code and Class form its identity; all
// other fields are best-effort.
type BodyRecord struct {
	Code       string
	Name       string
	Class      string // "raio" (RAIO table) or "icm" (ICM table)
	Region     string
	Members    []string
	Observers  []string
	WebsiteURL string
	Warnings   []string
	Checksum   string
}

var (
	// icmHeadingRe marks the Investigation Cooperation Mechanisms table by its
	// section heading. The RAIO table carries no such heading, so a table that
	// contains this phrase is the ICM table.
	icmHeadingRe = regexp.MustCompile(`(?i)cooperation\s+mechanism`)

	// observerClauseRe captures every observer clause embedded in a Member States
	// cell. The page uses two real lead-ins: "+ Observers:" (ENCASIA) and
	// "SPECIAL OBSERVERS:" (ARCM-SAM). The clause runs to end of the cell.
	observerClauseRe = regexp.MustCompile(`(?i)(?:\+\s*)?(?:special\s+)?observers?\s*:\s*(.*)$`)

	// headerCellRe matches a column-header cell so header rows are skipped.
	headerCellRe = regexp.MustCompile(`(?i)^(organization|description|region|member\s+states|website)$`)

	urlRe = regexp.MustCompile(`https?://[^\s<>")]+`)

	// splitRe splits a member/observer run on commas and semicolons only. The
	// English connector " and " is intentionally excluded: it appears as the last
	// separator in some real lists ("Suriname and Venezuela") but also occurs
	// inside multi-word country names ("Trinidad and Tobago", "Bosnia and
	// Herzegovina") where splitting would produce bogus labels. Callers that need
	// to handle the trailing " and " pattern should pre-process or use commas in
	// source data.
	splitRe = regexp.MustCompile(`\s*[;,]\s*`)
)

// Parse reads the RAIO/ICM HTML and returns one BodyRecord per data row of the
// two directory tables. It never returns an error for an individual malformed
// row; a returned error means the document itself could not be parsed as HTML.
func Parse(r io.Reader) ([]BodyRecord, error) {
	root, err := html.Parse(r)
	if err != nil {
		return nil, fmt.Errorf("parse raio html: %w", err)
	}

	tables := findTables(root)
	if len(tables) == 0 {
		return nil, fmt.Errorf("raio: no directory tables found")
	}

	var records []BodyRecord
	for i, table := range tables {
		class := classForTable(table, i)
		for _, row := range tableRows(table) {
			cells := rowCells(row)
			if len(cells) < 5 {
				continue // section-heading row or layout spacer
			}
			code := cleanText(cellText(cells[0]))
			if code == "" || headerCellRe.MatchString(code) {
				continue // header row or empty spacer
			}
			records = append(records, buildRecord(class, code, cells))
		}
	}

	return records, nil
}

// classForTable returns the class for a table. The Investigation Cooperation
// Mechanisms table is identified by its section heading; otherwise the first
// table is the RAIO table and any later one defaults to ICM.
func classForTable(table *html.Node, index int) string {
	if icmHeadingRe.MatchString(nodeText(table)) {
		return "icm"
	}
	if index == 0 {
		return "raio"
	}
	return "icm"
}

// buildRecord distils one five-column data row into a BodyRecord.
// cells = [Organization, Description, Region, Member States, Website].
func buildRecord(class, code string, cells []*html.Node) BodyRecord {
	rec := BodyRecord{
		Code:   code,
		Name:   cleanText(cellText(cells[1])),
		Class:  class,
		Region: cleanText(cellText(cells[2])),
	}

	members, observers := splitMembers(cleanText(cellText(cells[3])))
	rec.Members = members
	rec.Observers = observers

	// Website: prefer the cell's <a href>; fall back to a bare URL in the text.
	if href := firstHref(cells[4]); href != "" {
		rec.WebsiteURL = strings.Trim(href, ".,;)")
	} else if m := urlRe.FindString(cellText(cells[4])); m != "" {
		rec.WebsiteURL = strings.Trim(m, ".,;)")
	}

	if len(rec.Members) == 0 {
		rec.Warnings = append(rec.Warnings, "no member States parsed for "+rec.Code)
	}

	rec.Checksum = checksum(rec)
	return rec
}

// splitMembers extracts the observer clause first, then splits the remaining
// member run. Removing observers before splitting keeps observer labels out of
// Members. Both real lead-ins ("+ Observers:" and "SPECIAL OBSERVERS:") end the
// member list and run to the end of the cell.
func splitMembers(raw string) (members, observers []string) {
	memberPart := raw
	if m := observerClauseRe.FindStringSubmatchIndex(raw); m != nil {
		memberPart = raw[:m[0]]
		observers = splitLabels(raw[m[2]:m[3]])
	}
	members = splitLabels(memberPart)
	return members, observers
}

// splitLabels splits a run on commas/semicolons/" and ", trims each fragment,
// strips a trailing period, and drops empties.
func splitLabels(raw string) []string {
	var out []string
	for _, part := range splitRe.Split(raw, -1) {
		label := strings.TrimRight(normalizeWhitespace(part), ".")
		label = normalizeWhitespace(label)
		if label != "" {
			out = append(out, label)
		}
	}
	return out
}

// findTables returns every <table> in document order.
func findTables(root *html.Node) []*html.Node {
	var tables []*html.Node
	var walk func(*html.Node)
	walk = func(n *html.Node) {
		if n.Type == html.ElementNode && n.DataAtom == atom.Table {
			tables = append(tables, n)
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(root)
	return tables
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

// firstHref returns the href of the first descendant <a> of a node, if any.
func firstHref(n *html.Node) string {
	var href string
	var walk func(*html.Node) bool
	walk = func(x *html.Node) bool {
		if x.Type == html.ElementNode && x.DataAtom == atom.A {
			for _, a := range x.Attr {
				if a.Key == "href" && strings.TrimSpace(a.Val) != "" {
					href = strings.TrimSpace(a.Val)
					return true
				}
			}
		}
		for c := x.FirstChild; c != nil; c = c.NextSibling {
			if walk(c) {
				return true
			}
		}
		return false
	}
	walk(n)
	return href
}

// cellText returns the concatenated text content of a table cell. Inline markup
// (the page splits labels across nested spans) collapses into one run; whitespace
// cleanup happens in cleanText.
func cellText(n *html.Node) string {
	return nodeText(n)
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

// cleanText trims and collapses an inline string to a single line, dropping
// non-breaking and zero-width spaces.
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

// checksum is a stable content hash of the identifying + canonical fields of a
// body record, used as the staged_regional_bodies record_checksum.
func checksum(r BodyRecord) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s\x00%s\x00%s\x00%s\x00%s\x00%s",
		r.Code, r.Name, r.Class, r.WebsiteURL,
		strings.Join(r.Members, ","), strings.Join(r.Observers, ","))
	return hex.EncodeToString(h.Sum(nil))
}
