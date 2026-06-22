// Package aia parses and imports the ICAO Accident Investigation Authorities
// (AIA) contact directory. The directory is a single HTML page that lists, per
// State, the national authority responsible for accident investigation together
// with its contact details, website, and any delegation to another State or
// regional body. There is no machine-readable feed, so the importer reads the
// HTML structure directly.
//
// The parser is deliberately tolerant: malformed or incomplete blocks never
// crash it. Instead it preserves the complete raw block and emits Warnings so
// the importer can stage every record and let an operator review the gaps.
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
)

// Record is one parsed State block from the AIA directory. Every field except
// the identity (CountryLabel) is best-effort; an empty value simply means the
// page did not carry that datum for the State. RawContact always holds the
// complete text of the block so nothing is silently lost.
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

// contactHeadingRe matches the directory's top heading so we only start parsing
// State blocks once we are inside the contact section.
var contactHeadingRe = regexp.MustCompile(`(?i)accident\s+investigation\s+authorities\s+contact\s+information`)

// headingTags are the element names that introduce a State block. The directory
// uses <h2> per State under a single <h1> section heading; we accept any of the
// lower heading levels to tolerate markup drift.
var headingTags = map[string]bool{"h2": true, "h3": true, "h4": true}

var (
	emailRe   = regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`)
	phoneRe   = regexp.MustCompile(`(?i)\b(?:tel|phone|fax)\b[:.\s]*\+?[0-9][0-9()\-.\s]{6,}`)
	urlRe     = regexp.MustCompile(`https?://[^\s<>")]+`)
	referToRe = regexp.MustCompile(`(?i)refer\s+to\s+(?:the\s+)?([A-Za-z][A-Za-z .'\-]+?)\s*[.\n]`)
	seeBodyRe = regexp.MustCompile(`(?i)\bsee\s+([A-Za-z][A-Za-z .'\-]+?)\s*[.\n]`)
	updatedRe = regexp.MustCompile(`(?i)(?:icao\s+)?updated[:.\s]*([0-9]{4}-[0-9]{2}-[0-9]{2})`)
)

// Parse reads the AIA directory HTML and returns one Record per State block.
// It never returns an error for malformed individual blocks; a returned error
// means the document itself could not be parsed as HTML.
func Parse(r io.Reader) ([]Record, error) {
	root, err := html.Parse(r)
	if err != nil {
		return nil, fmt.Errorf("parse aia html: %w", err)
	}

	// Flatten the document into an ordered stream of (heading|text) tokens. The
	// directory is laid out as a flat sequence of per-State headings followed by
	// their content, so a single in-order walk lets us slice it into blocks. Each
	// State heading inside the contact section starts a new Record; matching to a
	// canonical ISO country happens later in the importer against the seeded
	// countries table (NormalizeName is applied there), so the parser stays
	// independent of any country list and never silently drops an unknown State.
	var tokens []token
	inSection := false
	collect(root, &tokens, &inSection)

	var records []Record
	var cur *blockBuilder
	flush := func() {
		if cur != nil {
			records = append(records, cur.build())
			cur = nil
		}
	}

	for _, tk := range tokens {
		if tk.isHeading {
			flush()
			cur = &blockBuilder{label: normalizeWhitespace(tk.text)}
			continue
		}
		if cur != nil {
			cur.add(tk.text)
		}
	}
	flush()

	return records, nil
}

// token is one flattened node: either a heading or a run of body text.
type token struct {
	isHeading bool
	text      string
}

// collect walks the DOM in document order, emitting a heading token for each
// heading element (after the contact section heading is seen) and a text token
// for each non-empty text node. The contact-section <h1> flips inSection on so
// preamble headers above it are ignored.
func collect(n *html.Node, out *[]token, inSection *bool) {
	if n.Type == html.ElementNode && isHeadingTag(n.Data) {
		// The contact-section heading flips inSection on; it is itself never a
		// State block, and any heading above it is preamble we skip.
		if contactHeadingRe.MatchString(nodeText(n)) {
			*inSection = true
			return
		}
		if !*inSection {
			return
		}
		if headingTags[n.Data] {
			*out = append(*out, token{isHeading: true, text: normalizeWhitespace(nodeText(n))})
		}
		return
	}

	if *inSection && n.Type == html.TextNode {
		text := normalizeWhitespace(n.Data)
		if text != "" {
			*out = append(*out, token{text: text})
		}
	}

	for c := n.FirstChild; c != nil; c = c.NextSibling {
		collect(c, out, inSection)
	}
}

// isHeadingTag reports whether an element name is any heading level.
func isHeadingTag(tag string) bool {
	switch tag {
	case "h1", "h2", "h3", "h4", "h5", "h6":
		return true
	}
	return false
}

// blockBuilder accumulates the text lines of one State block before they are
// distilled into a Record.
type blockBuilder struct {
	label string
	lines []string
}

func (b *blockBuilder) add(line string) {
	line = normalizeWhitespace(line)
	if line != "" {
		b.lines = append(b.lines, line)
	}
}

func (b *blockBuilder) build() Record {
	raw := strings.Join(b.lines, "\n")
	rec := Record{
		CountryLabel: b.label,
		RawContact:   raw,
	}

	// Emails: deobfuscate explicit [at]/[dot] forms first, then extract.
	deob := deobfuscate(raw)
	seenEmail := map[string]bool{}
	for _, m := range emailRe.FindAllString(deob, -1) {
		e := strings.ToLower(strings.Trim(m, ".,;"))
		if !seenEmail[e] {
			seenEmail[e] = true
			rec.Emails = append(rec.Emails, e)
		}
	}

	// Phones.
	seenPhone := map[string]bool{}
	for _, m := range phoneRe.FindAllString(raw, -1) {
		p := cleanPhone(m)
		if p != "" && !seenPhone[p] {
			seenPhone[p] = true
			rec.Phones = append(rec.Phones, p)
		}
	}

	// Website / archive URL: first http(s) URL in the block.
	if m := urlRe.FindString(raw); m != "" {
		rec.WebsiteURL = strings.Trim(m, ".,;)")
	}

	// Delegations.
	if m := referToRe.FindStringSubmatch(raw); m != nil {
		rec.ReferenceCountry = normalizeWhitespace(m[1])
	}
	if m := seeBodyRe.FindStringSubmatch(raw); m != nil {
		// Avoid mis-capturing "see" inside ordinary prose; only treat the first
		// "See X" sentence (the directory's delegation marker) as a reference.
		rec.ReferenceBody = normalizeWhitespace(m[1])
	}

	// ICAO update date.
	if m := updatedRe.FindStringSubmatch(raw); m != nil {
		if ts, err := time.Parse("2006-01-02", m[1]); err == nil {
			rec.UpdatedAt = &ts
		} else {
			rec.Warnings = append(rec.Warnings, "unparsable ICAO update date: "+m[1])
		}
	}

	// Authority name: the directory bolds the authority; we approximate that as
	// the first non-delegation, non-contact line of the block.
	rec.AuthorityName = guessAuthorityName(b.lines, rec)

	// Warnings: a block that is neither a delegation nor carries an authority is
	// malformed/incomplete. Preserve it (raw is already set) and flag it.
	if rec.AuthorityName == "" && rec.ReferenceCountry == "" && rec.ReferenceBody == "" {
		rec.Warnings = append(rec.Warnings, "no authority name or delegation found in block")
	}

	rec.Checksum = checksum(rec)
	return rec
}

// guessAuthorityName returns the first line of the block that looks like an
// authority name: not a delegation marker and not a pure contact line.
func guessAuthorityName(lines []string, rec Record) string {
	for _, ln := range lines {
		low := strings.ToLower(ln)
		switch {
		case strings.HasPrefix(low, "refer to"), strings.HasPrefix(low, "see "):
			continue
		case strings.HasPrefix(low, "tel"), strings.HasPrefix(low, "fax"),
			strings.HasPrefix(low, "phone"), strings.HasPrefix(low, "email"),
			strings.HasPrefix(low, "website"), strings.HasPrefix(low, "icao updated"),
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

// deobfuscate decodes only explicit [at]/[dot] obfuscation. It does not attempt
// to repair any other malformed address, per spec §9.
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
	s = regexp.MustCompile(`(?i)^(tel|phone|fax)[:.\s]*`).ReplaceAllString(s, "")
	s = strings.TrimRight(s, ".,; ")
	return normalizeWhitespace(s)
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

// normalizeWhitespace trims and collapses internal whitespace runs (including
// non-breaking spaces) to single ASCII spaces.
func normalizeWhitespace(s string) string {
	s = strings.ReplaceAll(s, " ", " ")
	return strings.Join(strings.Fields(s), " ")
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
