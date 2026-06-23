// Package wayback is the Internet-Archive acquisition worker: it drains
// wayback_cdx crawl jobs by querying the CDX index for archived PDFs, staging
// the discovered snapshots, and downloading them to a local store.
package wayback

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// Snapshot is one archived PDF capture worth staging.
type Snapshot struct {
	OriginalURL string
	ArchivedURL string
	Timestamp   string
	Mimetype    string
	Digest      string
	Length      int64
}

// ParseCDX parses Internet Archive CDX JSON (array-of-arrays, first row is the
// header). It keeps HTTP-200 PDF captures, collapses by digest (first wins), and
// builds the raw archived URL. Malformed rows are skipped and counted in
// warnings; a non-array body returns an error.
func ParseCDX(raw []byte) (snaps []Snapshot, warnings int, err error) {
	var rows [][]string
	if err := json.Unmarshal(raw, &rows); err != nil {
		return nil, 0, fmt.Errorf("wayback: parse cdx json: %w", err)
	}
	seen := map[string]bool{}
	for i, r := range rows {
		if i == 0 {
			continue // header
		}
		if len(r) < 7 {
			warnings++
			continue
		}
		timestamp, original, mimetype, statuscode, digest, lengthStr := r[1], r[2], r[3], r[4], r[5], r[6]
		if statuscode != "200" {
			continue
		}
		if !strings.Contains(mimetype, "pdf") {
			continue
		}
		length, convErr := strconv.ParseInt(lengthStr, 10, 64)
		if convErr != nil {
			warnings++
			continue
		}
		if seen[digest] {
			continue
		}
		seen[digest] = true
		snaps = append(snaps, Snapshot{
			OriginalURL: original,
			ArchivedURL: "https://web.archive.org/web/" + timestamp + "id_/" + original,
			Timestamp:   timestamp,
			Mimetype:    mimetype,
			Digest:      digest,
			Length:      length,
		})
	}
	return snaps, warnings, nil
}
