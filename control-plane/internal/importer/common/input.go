package common

import "time"

// Input carries one fetched source document into an importer. It is the single
// shared entry type for every importer (AIA in Task 9, RAIO in Task 10) and the
// CLI in Task 13, so it stays deliberately general.
//
// Two callers populate it:
//   - A live fetch sets Body plus the response metadata fields (FinalURL,
//     StatusCode, ContentType, ETag, LastModified) so the snapshot mirrors the
//     HTTP response.
//   - An offline --source-file run sets Body, SourceURL and FetchedAt only; the
//     metadata fields stay zero and are stored as NULL.
//
// SourceURL identifies which seeded source row the importer resolves against and
// is the URL recorded on the import run and snapshot.
type Input struct {
	SourceURL string
	Body      []byte
	FetchedAt time.Time

	// Optional response metadata, set by a live fetch.
	FinalURL     string
	StatusCode   int
	ContentType  string
	ETag         string
	LastModified string
}
