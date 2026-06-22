// Package raio parses and imports the ICAO list of Regional Accident and
// Incident Investigation Organizations (RAIOs) and Investigation Cooperation
// Mechanisms (ICMs). The page is a single HTML document with two distinct
// sections — one for RAIOs and one for ICMs — each listing regional bodies with
// their member States, an optional observer clause, a region label and a
// website.
//
// There is no machine-readable feed, so the parser reads the HTML structure
// directly. It parses the two sections independently: a body's class derives
// from which section it sits under (RAIO section → "raio", ICM section →
// "icm"). The parser is deliberately tolerant: a malformed body block never
// crashes it; unparsable bits become Warnings so the importer can stage every
// body and let an operator review the gaps.
package raio

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"regexp"
	"strings"

	"golang.org/x/net/html"
)

// BodyRecord is one parsed regional body. Code and Class form its identity; all
// other fields are best-effort. Members and Observers hold the raw State labels
// exactly as written on the page — resolution to seeded ISO countries happens in
// the importer, so the parser stays independent of any country list and never
// silently drops an unknown label.
type BodyRecord struct {
	Code       string
	Name       string
	Class      string // "raio" (RAIO section) or "icm" (ICM section)
	Region     string
	Members    []string
	Observers  []string
	WebsiteURL string
	Warnings   []string
	Checksum   string
}

var (
	// raioHeadingRe / icmHeadingRe mark the section a body belongs to. The RAIO
	// check runs first and is specific to "RAIO"/"Investigation Organization"; the
	// ICM check is specific to "ICM"/"Cooperation Mechanism".
	raioHeadingRe = regexp.MustCompile(`(?i)\bRAIO|investigation\s+organization`)
	icmHeadingRe  = regexp.MustCompile(`(?i)\bICM|cooperation\s+mechanism`)

	// memberLineRe matches the various lead-ins used for the member list.
	memberLineRe = regexp.MustCompile(`(?i)^(?:member|participating|cooperating)\s+states\s*:\s*(.*)$`)
	// regionLineRe matches "Region: X".
	regionLineRe = regexp.MustCompile(`(?i)^region\s*:\s*(.*)$`)
	// observerClauseRe captures a parenthesised observer clause anywhere in the
	// member text, e.g. "(observer: Panama; observers: Mexico)".
	observerClauseRe = regexp.MustCompile(`(?i)\(\s*observers?\s*:\s*([^)]*)\)`)
	// observerLabelRe strips a redundant "observer(s):" lead-in that can repeat
	// inside a single clause, e.g. "Panama; observers: Mexico".
	observerLabelRe = regexp.MustCompile(`(?i)^\s*observers?\s*:\s*`)
	// codeHeadingRe splits a body heading "CODE — Name" into its code and name.
	codeHeadingRe = regexp.MustCompile(`^\s*([A-Z][A-Z0-9-]+)\s*[—–-]\s*(.+)$`)
	urlRe         = regexp.MustCompile(`https?://[^\s<>")]+`)
	// splitRe splits a member label run on commas and semicolons.
	splitRe = regexp.MustCompile(`[;,]`)
)

// Parse reads the RAIO/ICM HTML and returns one BodyRecord per body. It never
// returns an error for an individual malformed body block; a returned error
// means the document itself could not be parsed as HTML.
func Parse(r io.Reader) ([]BodyRecord, error) {
	root, err := html.Parse(r)
	if err != nil {
		return nil, fmt.Errorf("parse raio html: %w", err)
	}

	var records []BodyRecord
	class := "" // current section class; empty until a section heading is seen
	var cur *bodyBuilder

	flush := func() {
		if cur != nil {
			records = append(records, cur.build())
			cur = nil
		}
	}

	var walk func(*html.Node)
	walk = func(n *html.Node) {
		if n.Type == html.ElementNode {
			switch n.Data {
			case "h2":
				// A section heading switches the active class. RAIO is checked first
				// so a heading mentioning both still classifies as RAIO.
				txt := nodeText(n)
				switch {
				case raioHeadingRe.MatchString(txt):
					class = "raio"
				case icmHeadingRe.MatchString(txt):
					class = "icm"
				}
				return
			case "h3":
				// A body heading inside a section starts a new body block.
				if class != "" {
					flush()
					cur = newBodyBuilder(class, normalizeWhitespace(nodeText(n)))
				}
				return
			}
		}
		if cur != nil && n.Type == html.TextNode {
			cur.add(normalizeWhitespace(n.Data), nodeHref(n))
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(root)
	flush()

	return records, nil
}

// bodyBuilder accumulates the text/href of one body block before it is distilled
// into a BodyRecord.
type bodyBuilder struct {
	class   string
	heading string
	lines   []string
	href    string
}

func newBodyBuilder(class, heading string) *bodyBuilder {
	return &bodyBuilder{class: class, heading: heading}
}

func (b *bodyBuilder) add(line, href string) {
	if line != "" {
		b.lines = append(b.lines, line)
	}
	if href != "" && b.href == "" {
		b.href = href
	}
}

func (b *bodyBuilder) build() BodyRecord {
	rec := BodyRecord{Class: b.class}

	// Identity: "CODE — Name". Fall back to the whole heading as the code if the
	// dash form is absent, so nothing is dropped.
	if m := codeHeadingRe.FindStringSubmatch(b.heading); m != nil {
		rec.Code = strings.TrimSpace(m[1])
		rec.Name = normalizeWhitespace(m[2])
	} else {
		rec.Code = normalizeWhitespace(b.heading)
		rec.Warnings = append(rec.Warnings, "body heading missing CODE — Name form: "+b.heading)
	}

	for _, ln := range b.lines {
		if m := regionLineRe.FindStringSubmatch(ln); m != nil {
			rec.Region = normalizeWhitespace(m[1])
			continue
		}
		if m := memberLineRe.FindStringSubmatch(ln); m != nil {
			members, observers := splitMembers(m[1])
			rec.Members = append(rec.Members, members...)
			rec.Observers = append(rec.Observers, observers...)
		}
	}

	// Website: prefer an <a href>; fall back to a bare URL in the text.
	if b.href != "" {
		rec.WebsiteURL = strings.Trim(b.href, ".,;)")
	} else {
		for _, ln := range b.lines {
			if m := urlRe.FindString(ln); m != "" {
				rec.WebsiteURL = strings.Trim(m, ".,;)")
				break
			}
		}
	}

	if len(rec.Members) == 0 {
		rec.Warnings = append(rec.Warnings, "no member States parsed for "+rec.Code)
	}

	rec.Checksum = checksum(rec)
	return rec
}

// splitMembers extracts the observer clause first, then splits the remaining
// member run on commas and semicolons. Removing observers before splitting keeps
// observer labels out of Members. Empty and stray-punctuation fragments are
// dropped.
func splitMembers(raw string) (members, observers []string) {
	// Pull out every observer clause and collect its labels separately.
	for _, m := range observerClauseRe.FindAllStringSubmatch(raw, -1) {
		observers = append(observers, splitLabels(m[1])...)
	}
	// Strip the observer clauses from the member run so they cannot leak in.
	cleaned := observerClauseRe.ReplaceAllString(raw, "")
	members = splitLabels(cleaned)
	return members, observers
}

// splitLabels splits a run on commas/semicolons, trims each fragment, strips a
// trailing period, and drops empties.
func splitLabels(raw string) []string {
	var out []string
	for _, part := range splitRe.Split(raw, -1) {
		part = observerLabelRe.ReplaceAllString(part, "")
		label := strings.TrimRight(normalizeWhitespace(part), ".")
		label = normalizeWhitespace(label)
		if label != "" {
			out = append(out, label)
		}
	}
	return out
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

// nodeHref returns the href of the nearest enclosing anchor of a text node, if
// the text node's parent is an <a>. The walk visits text nodes after their
// element parents, so an anchor's text node carries the link.
func nodeHref(n *html.Node) string {
	if n.Parent == nil || n.Parent.Type != html.ElementNode || n.Parent.Data != "a" {
		return ""
	}
	for _, a := range n.Parent.Attr {
		if a.Key == "href" {
			return a.Val
		}
	}
	return ""
}

// normalizeWhitespace trims and collapses internal whitespace runs (including
// non-breaking spaces) to single ASCII spaces.
func normalizeWhitespace(s string) string {
	s = strings.ReplaceAll(s, " ", " ")
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
