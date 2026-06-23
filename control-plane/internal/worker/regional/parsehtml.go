package regional

import (
	"context"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"
)

// Shared HTML-listing parsing for the regional bodies. Each body's parser is a
// thin wrapper over parseListing with a body-specific origin and link filter;
// the parsers are pure (stdlib only) and unit-tested against captured fixtures.

var (
	anchorRe  = regexp.MustCompile(`(?is)<a\b[^>]*\bhref\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>`)
	tagRe     = regexp.MustCompile(`(?s)<[^>]+>`)
	wsRe      = regexp.MustCompile(`\s+`)
	isoDateRe = regexp.MustCompile(`(\d{4})[-/](\d{2})[-/](\d{2})`)
	dmyDateRe = regexp.MustCompile(`(\d{2})\.(\d{2})\.(\d{4})`)
	yearRe    = regexp.MustCompile(`(?:^|[-/_])(?:19|20)\d{2}(?:[-/_]|$)`)
)

// parseListing extracts RegionalRecords from an HTML report listing. base is the
// site origin used to absolute-ify relative hrefs; match keeps only anchors
// whose resolved URL is an actual report entry. warnings counts anchors that
// matched the link filter but lacked a usable title or ref.
func parseListing(raw []byte, base string, match func(abs string) bool) ([]RegionalRecord, int, error) {
	baseURL, err := url.Parse(base)
	if err != nil {
		return nil, 0, fmt.Errorf("regional: parse base %q: %w", base, err)
	}
	seen := map[string]bool{}
	var recs []RegionalRecord
	warnings := 0
	for _, m := range anchorRe.FindAllSubmatch(raw, -1) {
		href := strings.TrimSpace(html.UnescapeString(string(m[1])))
		if href == "" || strings.HasPrefix(href, "#") {
			continue
		}
		u, err := url.Parse(href)
		if err != nil {
			continue
		}
		resolved := baseURL.ResolveReference(u)
		// Allow-list the safe web schemes only; this drops javascript:, data:,
		// vbscript:, mailto:, etc. without enumerating every dangerous scheme.
		if resolved.Scheme != "http" && resolved.Scheme != "https" {
			continue
		}
		abs := resolved.String()
		if match != nil && !match(abs) {
			continue
		}
		title := cleanText(string(m[2]))
		ref := refFromURL(abs)
		if title == "" || ref == "" {
			warnings++
			continue
		}
		if seen[ref] {
			continue
		}
		seen[ref] = true
		rec := RegionalRecord{Ref: ref, Title: title, OriginalURL: abs}
		if d := extractDate(title); d != "" {
			rec.OccurrenceDate = d
		}
		if isPDF(abs) {
			rec.ReportURL = abs
			rec.Mimetype = "application/pdf"
		}
		recs = append(recs, rec)
	}
	return recs, warnings, nil
}

func cleanText(s string) string {
	s = tagRe.ReplaceAllString(s, " ")
	s = html.UnescapeString(s)
	s = wsRe.ReplaceAllString(s, " ")
	return strings.TrimSpace(s)
}

// knownExts are the page/document extensions stripped from a ref's trailing
// segment; any other trailing dot (e.g. "report.2024-final") is left intact.
var knownExts = map[string]bool{
	".pdf": true, ".html": true, ".htm": true,
	".php": true, ".aspx": true, ".asp": true, ".jsp": true,
}

// refFromURL derives a stable record id from the last path segment of the URL,
// stripping a known file extension and folding in the query string so reports
// that differ only by query (e.g. ?p=123 on WordPress/Wix) get distinct refs.
func refFromURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	seg := ""
	if p := strings.Trim(u.Path, "/"); p != "" {
		seg = p
		if i := strings.LastIndex(p, "/"); i >= 0 {
			seg = p[i+1:]
		}
		if i := strings.LastIndex(seg, "."); i > 0 && knownExts[strings.ToLower(seg[i:])] {
			seg = seg[:i]
		}
	}
	if q := u.RawQuery; q != "" {
		if seg == "" {
			seg = q
		} else {
			seg = seg + "?" + q
		}
	}
	return seg
}

func isPDF(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	return strings.HasSuffix(strings.ToLower(u.Path), ".pdf")
}

// extractDate finds the first yyyy-mm-dd / yyyy/mm/dd or dd.mm.yyyy date in s and
// returns it in ISO yyyy-mm-dd form, or "" when none is present or the matched
// digits are not a real calendar date (e.g. a "2023-45-67" report number).
func extractDate(s string) string {
	for _, m := range [][]string{
		firstNonNil(isoDateRe.FindStringSubmatch(s), 1, 2, 3),
		firstNonNil(dmyDateRe.FindStringSubmatch(s), 3, 2, 1),
	} {
		if m == nil {
			continue
		}
		if t, err := time.Parse("2006-01-02", fmt.Sprintf("%s-%s-%s", m[0], m[1], m[2])); err == nil {
			return t.Format("2006-01-02")
		}
	}
	return ""
}

// firstNonNil reorders a regexp submatch into [year, month, day] order using the
// given 1-based group indices, or returns nil when there was no match.
func firstNonNil(m []string, y, mo, d int) []string {
	if m == nil {
		return nil
	}
	return []string{m[y], m[mo], m[d]}
}

// hostMatch reports whether abs is hosted on wantHost or a subdomain of it.
func hostMatch(abs, wantHost string) bool {
	u, err := url.Parse(abs)
	if err != nil {
		return false
	}
	h := strings.ToLower(u.Hostname())
	return h == wantHost || strings.HasSuffix(h, "."+wantHost)
}

// looksLikeReport reports whether a same-site URL is an accident-report entry
// rather than navigation chrome: a PDF, a report/investigation/accident path, or
// a path bearing a 4-digit year segment.
func looksLikeReport(abs string) bool {
	u, err := url.Parse(abs)
	if err != nil {
		return false
	}
	p := strings.ToLower(u.Path)
	if strings.HasSuffix(p, ".pdf") {
		return true
	}
	for _, kw := range []string{"report", "investig", "accident", "incident", "occurrence", "rassled", "final"} {
		if strings.Contains(p, kw) {
			return true
		}
	}
	return yearRe.MatchString(u.Path)
}

// loadListing returns the listing bytes from sourceFile when set (out-of-band
// operator export), else live-fetches liveURL.
func loadListing(ctx context.Context, timeout time.Duration, sourceFile, liveURL string) ([]byte, error) {
	if sourceFile != "" {
		b, err := os.ReadFile(sourceFile)
		if err != nil {
			return nil, fmt.Errorf("regional: read source-file %q: %w", sourceFile, err)
		}
		return b, nil
	}
	client := &http.Client{Timeout: timeout}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, liveURL, nil)
	if err != nil {
		return nil, fmt.Errorf("regional: build request %s: %w", liveURL, err)
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (compatible; aviation-coverage/1.0)")
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("regional: fetch %s: %w", liveURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("regional: fetch %s: status %d", liveURL, resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("regional: read %s: %w", liveURL, err)
	}
	return body, nil
}
