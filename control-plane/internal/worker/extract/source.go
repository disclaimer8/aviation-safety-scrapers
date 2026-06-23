package extract

import (
	"context"
	"database/sql"
)

// ExtractedEvent is the structured result of LLM extraction. Pointer fields are
// nullable (unknown).
type ExtractedEvent struct {
	IsAviationAccident   bool     `json:"is_aviation_accident"`
	Date                 string   `json:"date"`
	DatePrecision        string   `json:"date_precision"`
	Location             string   `json:"location"`
	Latitude             *float64 `json:"latitude"`
	Longitude            *float64 `json:"longitude"`
	AircraftRegistration string   `json:"aircraft_registration"`
	AircraftType         string   `json:"aircraft_type"`
	Manufacturer         string   `json:"manufacturer"`
	OperatorName         string   `json:"operator_name"`
	FlightNumber         string   `json:"flight_number"`
	Fatalities           *int     `json:"fatalities"`
	Injuries             *int     `json:"injuries"`
	EventType            string   `json:"event_type"`
	InvestigationStatus  string   `json:"investigation_status"`
	ReportType           string   `json:"report_type"`
	Title                string   `json:"title"`
	Language             string   `json:"language"`
	PublishedDate        string   `json:"published_date"`
}

// OCRClient turns a PDF's bytes into plain text. Production uses an HTTP client
// (built in the wayback package); tests use a fixture.
type OCRClient interface {
	OCR(ctx context.Context, pdf []byte) (string, error)
}

// LLMClient extracts structured event fields from report text.
type LLMClient interface {
	Extract(ctx context.Context, text string) (ExtractedEvent, error)
}

// execQuerier is satisfied by *sql.DB and *sql.Tx, so promotion helpers (and
// source resolution) work inside or outside a transaction.
type execQuerier interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// ExtractStats is the aggregate result of a batch run.
type ExtractStats struct {
	Extracted int
	Skipped   int
	Failed    int
}

// ExtractDoc is a staged document ready for the extract step. It is
// source-agnostic; each StagedDocSource populates it from its own staging table.
type ExtractDoc struct {
	ID            int64
	CountryID     int64
	ISO2          string
	Digest        string
	LocalFilePath string
	OriginalURL   string
	ArchivedURL   string
	OCRTextPath   sql.NullString
	Checksum      sql.NullString
	WaybackTarget string
	Attempts      int
	CrawlJobID    int64
	Priority      int64
}

// StagedDocSource abstracts a staging table behind the generic extract core. The
// core never names a concrete table; it asks the source to enumerate pending
// docs, ensure the file is on disk, resolve the crediting source, and persist
// each terminal state. Each adapter owns its own SQL.
type StagedDocSource interface {
	// Name identifies the source (e.g. "wayback") for logging.
	Name() string
	// PendingDocs returns docs needing extraction, highest priority first,
	// capped at limit (limit <= 0 means no cap).
	PendingDocs(ctx context.Context, db *sql.DB, limit int) ([]ExtractDoc, error)
	// EnsureDownloaded makes sure doc.LocalFilePath exists on disk, downloading
	// it if the adapter stages report URLs rather than files. Wayback is a no-op
	// (already downloaded).
	EnsureDownloaded(ctx context.Context, db *sql.DB, storeDir string, doc *ExtractDoc) error
	// ResolveSource returns the source to credit for a recovered report.
	ResolveSource(ctx context.Context, q execQuerier, doc ExtractDoc) (sourceID int64, tier int, copyright string, err error)
	// MarkSkipped advances the row to a terminal non-accident state.
	MarkSkipped(ctx context.Context, db *sql.DB, id int64) error
	// MarkExtracted links the row to its event and advances it to 'extracted'.
	MarkExtracted(ctx context.Context, db *sql.DB, id, eventID int64) error
	// RecordFailure marks the row failed, bumps its attempt counter, and logs a
	// crawl_errors row with the classified errType (transport/ocr/llm/parse).
	RecordFailure(ctx context.Context, db *sql.DB, doc ExtractDoc, url, errType string, cause error) error
	// PersistOCRPath records the OCR text path and advances the row to 'ocr_done'.
	PersistOCRPath(ctx context.Context, db *sql.DB, id int64, path string) error
}
