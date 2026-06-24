package extract

import (
	"html"
	"regexp"
	"strings"
)

var (
	scriptRe  = regexp.MustCompile(`(?is)<(script|style)\b[^>]*>.*?</(script|style)>`)
	htmlTagRe = regexp.MustCompile(`(?s)<[^>]+>`)
	wsRunRe   = regexp.MustCompile(`\s+`)
)

// htmlToText strips <script>/<style> blocks, removes all HTML tags, unescapes
// HTML entities, collapses whitespace, and trims the result. Block elements are
// replaced with a space so adjacent field values don't run together.
// Returns "" when the result is only whitespace.
func htmlToText(b []byte) string {
	s := string(b)
	s = scriptRe.ReplaceAllString(s, " ")
	s = htmlTagRe.ReplaceAllString(s, " ")
	s = html.UnescapeString(s)
	s = wsRunRe.ReplaceAllString(s, " ")
	return strings.TrimSpace(s)
}
