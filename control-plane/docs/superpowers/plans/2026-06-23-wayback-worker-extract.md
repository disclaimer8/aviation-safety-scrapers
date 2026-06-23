# Wayback Worker — Extraction (stage-2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn each downloaded Wayback PDF (`staged_wayback_documents.download_status='downloaded'`) into structured safety data: OCR → LLM field extraction → promotion into `events`/`reports` with provenance, deterministic confidence, and deterministic dedup.

**Architecture:** Extend the existing Go package `internal/worker/wayback/`. Two new injectable HTTP seams (`OCRClient`, `LLMClient`) mirror stage-1's `Fetcher`, so the whole pipeline is offline-testable with fixtures. A per-document state machine on the document row (`extraction_status`) drives work; promotion is one transaction per document for crash idempotency.

**Tech Stack:** Go (stdlib `database/sql`, `net/http`, `encoding/json`, `go:embed`), SQLite (STRICT tables, migration checksum guard), Ollama `/api/generate` (`format` JSON-schema, `think:false`) for the LLM real impl.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-23-wayback-worker-extract-design.md` — authoritative.
- **Package:** all worker code lives in `package wayback` under `internal/worker/wayback/`.
- **Error style:** wrap every error `fmt.Errorf("wayback: <verb> <ctx>: %w", err)`, matching existing files.
- **Migrations are immutable once applied:** 001–005 must not change; add only `006_wayback_extract.sql`. Filename must match `^sql/(\d{3})_([a-z0-9]+(?:_[a-z0-9]+)*)\.sql$` (so `006_wayback_extract.sql` is valid). Migrations are auto-discovered via `//go:embed sql/*.sql` — no registry edit needed.
- **STRICT tables:** every column has an explicit type; `INTEGER` for ms timestamps via `CAST(unixepoch('subsec') * 1000 AS INTEGER)`.
- **Public repo:** no private endpoint or token in source. All endpoints come from CLI flags/env and default to innocuous local values (`http://127.0.0.1:...`).
- **Enum safety:** before any DB write, clamp LLM-supplied enum fields to the column's allowed set, falling back to the column default rather than violating a `CHECK`.
- **Test command:** `go test ./internal/worker/wayback/...` and `go test ./...` from `control-plane/`.
- **Retry cap:** a document is excluded from selection at `extraction_attempts >= 3`.

---

## Task 1: Migration `006_wayback_extract.sql`

Adds the six extraction columns to `staged_wayback_documents`.

**Files:**
- Create: `internal/migrations/sql/006_wayback_extract.sql`
- Test: `internal/migrations/migrations_extract_test.go`

**Interfaces:**
- Consumes: existing `staged_wayback_documents` (from 005), `events` (from 002).
- Produces: columns `extraction_status`, `ocr_text_path`, `extraction_error`, `extraction_attempts`, `event_id` on `staged_wayback_documents`.

- [ ] **Step 1: Write the failing test**

Create `internal/migrations/migrations_extract_test.go` (mirror the style of `migrations_wayback_test.go` — open an in-memory DB, run `Apply`, assert behavior):

```go
package migrations

import (
	"context"
	"database/sql"
	"testing"

	_ "modernc.org/sqlite"
)

func TestMigration006ExtractSchema(t *testing.T) {
	ctx := context.Background()
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.ExecContext(ctx, "PRAGMA foreign_keys=ON"); err != nil {
		t.Fatal(err)
	}
	if err := Apply(ctx, db); err != nil {
		t.Fatalf("apply migrations: %v", err)
	}

	// New columns exist with defaults: insert a staged doc, read back extraction_status.
	_, err = db.ExecContext(ctx, `INSERT INTO countries (iso2, name) VALUES ('XW','Testland')`)
	if err != nil {
		t.Fatal(err)
	}
	var countryID, srcID int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XW'`).Scan(&countryID)
	res, err := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier, copyright_policy_notes)
		VALUES ('S','u','c','wayback',2,NULL)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ = res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?,'wayback_cdx','running')`, srcID, countryID)
	if err != nil {
		t.Fatal(err)
	}
	jobID, _ := res.LastInsertId()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO staged_wayback_documents
			(crawl_job_id, country_id, original_url, archived_url, timestamp, mimetype, digest)
		VALUES (?,?,'o','a','20200101000000','application/pdf','d1')`, jobID, countryID); err != nil {
		t.Fatal(err)
	}

	var status string
	var attempts int
	if err := db.QueryRowContext(ctx, `
		SELECT extraction_status, extraction_attempts FROM staged_wayback_documents WHERE digest='d1'`).
		Scan(&status, &attempts); err != nil {
		t.Fatalf("read new columns: %v", err)
	}
	if status != "pending" || attempts != 0 {
		t.Fatalf("defaults wrong: status=%q attempts=%d", status, attempts)
	}

	// CHECK rejects an invalid extraction_status.
	_, err = db.ExecContext(ctx, `
		UPDATE staged_wayback_documents SET extraction_status='bogus' WHERE digest='d1'`)
	if err == nil {
		t.Fatal("expected CHECK to reject bogus extraction_status")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/migrations/ -run TestMigration006ExtractSchema -v`
Expected: FAIL — `no such column: extraction_status` (the migration file does not exist yet).

- [ ] **Step 3: Write the migration**

Create `internal/migrations/sql/006_wayback_extract.sql`:

```sql
-- 006_wayback_extract.sql
-- Stage-2 extraction state machine on staged_wayback_documents: OCR text artifact,
-- per-document status, retry accounting, and the promoted event link.

ALTER TABLE staged_wayback_documents ADD COLUMN extraction_status TEXT NOT NULL
  DEFAULT 'pending' CHECK(extraction_status IN (
    'pending',
    'ocr_done',
    'extracted',
    'failed',
    'skipped'
  ));

ALTER TABLE staged_wayback_documents ADD COLUMN ocr_text_path TEXT;

ALTER TABLE staged_wayback_documents ADD COLUMN extraction_error TEXT;

ALTER TABLE staged_wayback_documents ADD COLUMN extraction_attempts INTEGER NOT NULL
  DEFAULT 0;

ALTER TABLE staged_wayback_documents ADD COLUMN event_id INTEGER
  REFERENCES events(id);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `go test ./internal/migrations/ -run TestMigration006ExtractSchema -v`
Expected: PASS.

Also run the full migrations package to confirm the checksum/name guards stay green:
Run: `go test ./internal/migrations/...`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/migrations/sql/006_wayback_extract.sql internal/migrations/migrations_extract_test.go
git commit -m "feat(control-plane): migration 006 wayback extraction columns"
```

---

## Task 2: OCR seam + text persistence (`ocr.go`)

`OCRClient` interface, the `httpOCRClient` real impl, and `PersistOCRText` which writes the text artifact and advances the row to `ocr_done`.

**Files:**
- Create: `internal/worker/wayback/ocr.go`
- Test: `internal/worker/wayback/ocr_test.go`

**Interfaces:**
- Consumes: `staged_wayback_documents` columns from Task 1.
- Produces:
  - `type OCRClient interface { OCR(ctx context.Context, pdf []byte) (string, error) }`
  - `func NewHTTPOCRClient(endpoint string, timeout time.Duration) OCRClient`
  - `func PersistOCRText(ctx context.Context, db *sql.DB, storeDir, iso2, digest string, docID int64, text string) (string, error)` — writes `<storeDir>/<iso2>/<digest>.txt`, sets `ocr_text_path` + `extraction_status='ocr_done'`, returns the text path.

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/ocr_test.go`:

```go
package wayback

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// fixtureOCRClient is the offline OCRClient for tests.
type fixtureOCRClient struct {
	Text string
	Err  error
}

func (f *fixtureOCRClient) OCR(ctx context.Context, pdf []byte) (string, error) {
	if f.Err != nil {
		return "", f.Err
	}
	return f.Text, nil
}

var _ OCRClient = (*fixtureOCRClient)(nil)
var _ OCRClient = (*httpOCRClient)(nil)

func TestPersistOCRText(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t) // helper defined in extractrunner_test.go (Task 8) — for this task, inline below
	docID, countryID := seedDownloadedDoc(t, db, "US", "deadbeef")
	_ = countryID

	store := t.TempDir()
	path, err := PersistOCRText(ctx, db, store, "US", "deadbeef", docID, "REPORT TEXT")
	if err != nil {
		t.Fatalf("PersistOCRText: %v", err)
	}
	want := filepath.Join(store, "US", "deadbeef.txt")
	if path != want {
		t.Fatalf("path=%q want %q", path, want)
	}
	b, err := os.ReadFile(want)
	if err != nil {
		t.Fatalf("read text: %v", err)
	}
	if string(b) != "REPORT TEXT" {
		t.Fatalf("text=%q", string(b))
	}
	var status, gotPath string
	db.QueryRowContext(ctx, `
		SELECT extraction_status, ocr_text_path FROM staged_wayback_documents WHERE id=?`, docID).
		Scan(&status, &gotPath)
	if status != "ocr_done" || gotPath != want {
		t.Fatalf("status=%q ocr_text_path=%q", status, gotPath)
	}
}
```

> The helpers `newExtractTestDB` and `seedDownloadedDoc` are introduced in this task's test file (below) and reused by later tasks. Put them in a shared test helper file `internal/worker/wayback/extracthelpers_test.go` so every extraction test can use them.

Create `internal/worker/wayback/extracthelpers_test.go`:

```go
package wayback

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	_ "modernc.org/sqlite"
)

// newExtractTestDB returns a migrated in-memory DB.
func newExtractTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.ExecContext(context.Background(), "PRAGMA foreign_keys=ON"); err != nil {
		t.Fatal(err)
	}
	if err := migrations.Apply(context.Background(), db); err != nil {
		t.Fatalf("apply migrations: %v", err)
	}
	return db
}

// seedDownloadedDoc inserts a country, a source, a crawl_job, and one
// downloaded staged document. Returns the document id and country id.
func seedDownloadedDoc(t *testing.T, db *sql.DB, iso2, digest string) (docID, countryID int64) {
	t.Helper()
	ctx := context.Background()
	res, err := db.ExecContext(ctx, `INSERT INTO countries (iso2, name) VALUES (?, ?)`, iso2, iso2+"land")
	if err != nil {
		t.Fatal(err)
	}
	countryID, _ = res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('seed','u','c-`+digest+`','wayback',2)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?,'wayback_cdx','running')`, srcID, countryID)
	if err != nil {
		t.Fatal(err)
	}
	jobID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO staged_wayback_documents
			(crawl_job_id, country_id, original_url, archived_url, timestamp, mimetype, digest,
			 local_file_path, checksum, download_status)
		VALUES (?,?,?,?,?,?,?,?,?, 'downloaded')`,
		jobID, countryID,
		"https://caa.example/report.pdf",
		"https://web.archive.org/web/20200101id_/https://caa.example/report.pdf",
		"20200101000000", "application/pdf", digest,
		"/store/"+iso2+"/"+digest+".pdf", "checksum-"+digest)
	if err != nil {
		t.Fatal(err)
	}
	docID, _ = res.LastInsertId()
	return docID, countryID
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/worker/wayback/ -run TestPersistOCRText -v`
Expected: FAIL — `undefined: OCRClient` / `undefined: PersistOCRText`.

- [ ] **Step 3: Write the implementation**

Create `internal/worker/wayback/ocr.go`:

```go
package wayback

import (
	"bytes"
	"context"
	"database/sql"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// OCRClient turns a PDF's bytes into plain text. Production uses httpOCRClient;
// tests use a fixtureOCRClient.
type OCRClient interface {
	OCR(ctx context.Context, pdf []byte) (string, error)
}

type httpOCRClient struct {
	endpoint string
	client   *http.Client
}

// NewHTTPOCRClient returns an OCRClient that POSTs the PDF bytes to endpoint and
// reads back the extracted text as the response body.
func NewHTTPOCRClient(endpoint string, timeout time.Duration) OCRClient {
	return &httpOCRClient{endpoint: endpoint, client: &http.Client{Timeout: timeout}}
}

func (h *httpOCRClient) OCR(ctx context.Context, pdf []byte) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, h.endpoint, bytes.NewReader(pdf))
	if err != nil {
		return "", fmt.Errorf("wayback: build ocr request: %w", err)
	}
	req.Header.Set("Content-Type", "application/pdf")
	resp, err := h.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("wayback: ocr post: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("wayback: ocr status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("wayback: read ocr body: %w", err)
	}
	return string(body), nil
}

// PersistOCRText writes text to <storeDir>/<iso2>/<digest>.txt, records the path
// in ocr_text_path, advances extraction_status to 'ocr_done', and returns the
// text path.
func PersistOCRText(ctx context.Context, db *sql.DB, storeDir, iso2, digest string, docID int64, text string) (string, error) {
	dir := filepath.Join(storeDir, iso2)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("wayback: mkdir %s: %w", dir, err)
	}
	path := filepath.Join(dir, digest+".txt")
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		return "", fmt.Errorf("wayback: write %s: %w", path, err)
	}
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_wayback_documents
		   SET ocr_text_path = ?, extraction_status = 'ocr_done'
		 WHERE id = ?`, path, docID); err != nil {
		return "", fmt.Errorf("wayback: mark ocr_done %d: %w", docID, err)
	}
	return path, nil
}

var _ OCRClient = (*httpOCRClient)(nil)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `go test ./internal/worker/wayback/ -run TestPersistOCRText -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/ocr.go internal/worker/wayback/ocr_test.go internal/worker/wayback/extracthelpers_test.go
git commit -m "feat(control-plane): wayback OCR seam + text persistence"
```

---

## Task 3: LLM seam + ExtractedEvent (`llm.go`)

`LLMClient` interface, the `ExtractedEvent` struct, the `httpLLMClient` real impl (Ollama `/api/generate` with `format` schema, `think:false`, input truncation), and the embedded prompt.

**Files:**
- Create: `internal/worker/wayback/llm.go`
- Create: `internal/worker/wayback/prompts/extract.txt`
- Test: `internal/worker/wayback/llm_test.go`

**Interfaces:**
- Produces:
  - `type ExtractedEvent struct { ... }` (fields below)
  - `type LLMClient interface { Extract(ctx context.Context, text string) (ExtractedEvent, error) }`
  - `func NewHTTPLLMClient(endpoint, model string, maxInputChars int, timeout time.Duration) LLMClient`

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/llm_test.go`:

```go
package wayback

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// fixtureLLMClient is the offline LLMClient for tests.
type fixtureLLMClient struct {
	Event ExtractedEvent
	Err   error
}

func (f *fixtureLLMClient) Extract(ctx context.Context, text string) (ExtractedEvent, error) {
	if f.Err != nil {
		return ExtractedEvent{}, f.Err
	}
	return f.Event, nil
}

var _ LLMClient = (*fixtureLLMClient)(nil)
var _ LLMClient = (*httpLLMClient)(nil)

func TestHTTPLLMClientParsesOllamaResponse(t *testing.T) {
	// Ollama /api/generate returns {"response":"<json string>"} when format is set.
	inner := `{"is_aviation_accident":true,"date":"2019-03-10","date_precision":"exact",` +
		`"aircraft_registration":"ET-AVJ","fatalities":157}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"response": inner})
	}))
	defer srv.Close()

	c := NewHTTPLLMClient(srv.URL, "qwen3.6-rw", 24000, 5*time.Second)
	ev, err := c.Extract(context.Background(), "some long report text")
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if !ev.IsAviationAccident || ev.AircraftRegistration != "ET-AVJ" || ev.Fatalities == nil || *ev.Fatalities != 157 {
		t.Fatalf("parsed wrong: %+v", ev)
	}
}

func TestHTTPLLMClientTruncatesInput(t *testing.T) {
	var gotPrompt string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Prompt string `json:"prompt"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		gotPrompt = body.Prompt
		_ = json.NewEncoder(w).Encode(map[string]string{"response": `{"is_aviation_accident":false}`})
	}))
	defer srv.Close()

	long := make([]byte, 50000)
	for i := range long {
		long[i] = 'x'
	}
	c := NewHTTPLLMClient(srv.URL, "m", 100, 5*time.Second)
	if _, err := c.Extract(context.Background(), string(long)); err != nil {
		t.Fatal(err)
	}
	// The 50000-char body must have been truncated to <= 100 chars of report text.
	if len(gotPrompt) > 100+len(extractPromptTemplate) {
		t.Fatalf("prompt not truncated: len=%d", len(gotPrompt))
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/worker/wayback/ -run TestHTTPLLMClient -v`
Expected: FAIL — `undefined: ExtractedEvent` / `undefined: NewHTTPLLMClient`.

- [ ] **Step 3: Write the embedded prompt**

Create `internal/worker/wayback/prompts/extract.txt`:

```
You are an aviation-accident data extractor. Read the accident/incident report
text below and return ONLY a JSON object matching the required schema.

Rules:
- is_aviation_accident: true only if this text is an actual aviation
  occurrence report (accident, serious incident, or incident). False for index
  pages, cover sheets, regulations, or unrelated documents.
- date: the occurrence date in ISO-8601 (YYYY-MM-DD) when known; YYYY-MM or YYYY
  when only partially known; empty string if unknown.
- date_precision: "exact", "month", "year", or "unknown" to match `date`.
- Use empty string for unknown text fields and null for unknown numbers.
- fatalities and injuries are integers (counts), null if unknown.
- event_type: one of accident, serious_incident, incident, hijacking, unknown.
- report_type: one of final, preliminary, interim, factual.

REPORT TEXT:
```

- [ ] **Step 4: Write the implementation**

Create `internal/worker/wayback/llm.go`:

```go
package wayback

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

//go:embed prompts/extract.txt
var extractPromptTemplate string

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

// LLMClient extracts structured event fields from report text.
type LLMClient interface {
	Extract(ctx context.Context, text string) (ExtractedEvent, error)
}

type httpLLMClient struct {
	endpoint string
	model    string
	maxChars int
	client   *http.Client
}

// NewHTTPLLMClient returns an LLMClient backed by an Ollama-compatible
// /api/generate endpoint. Input is head-truncated to maxChars before sending.
func NewHTTPLLMClient(endpoint, model string, maxInputChars int, timeout time.Duration) LLMClient {
	return &httpLLMClient{
		endpoint: endpoint,
		model:    model,
		maxChars: maxInputChars,
		client:   &http.Client{Timeout: timeout},
	}
}

// extractSchema is the JSON-Schema handed to Ollama's `format` field so the model
// is grammar-constrained to emit exactly the ExtractedEvent shape.
var extractSchema = json.RawMessage(`{
  "type":"object",
  "properties":{
    "is_aviation_accident":{"type":"boolean"},
    "date":{"type":"string"},
    "date_precision":{"type":"string"},
    "location":{"type":"string"},
    "latitude":{"type":["number","null"]},
    "longitude":{"type":["number","null"]},
    "aircraft_registration":{"type":"string"},
    "aircraft_type":{"type":"string"},
    "manufacturer":{"type":"string"},
    "operator_name":{"type":"string"},
    "flight_number":{"type":"string"},
    "fatalities":{"type":["integer","null"]},
    "injuries":{"type":["integer","null"]},
    "event_type":{"type":"string"},
    "investigation_status":{"type":"string"},
    "report_type":{"type":"string"},
    "title":{"type":"string"},
    "language":{"type":"string"},
    "published_date":{"type":"string"}
  },
  "required":["is_aviation_accident"]
}`)

func (h *httpLLMClient) Extract(ctx context.Context, text string) (ExtractedEvent, error) {
	if h.maxChars > 0 && len(text) > h.maxChars {
		text = text[:h.maxChars]
	}
	reqBody := map[string]any{
		"model":  h.model,
		"prompt": extractPromptTemplate + text,
		"stream": false,
		"think":  false,
		"format": extractSchema,
	}
	b, err := json.Marshal(reqBody)
	if err != nil {
		return ExtractedEvent{}, fmt.Errorf("wayback: marshal llm request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, h.endpoint, bytes.NewReader(b))
	if err != nil {
		return ExtractedEvent{}, fmt.Errorf("wayback: build llm request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := h.client.Do(req)
	if err != nil {
		return ExtractedEvent{}, fmt.Errorf("wayback: llm post: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return ExtractedEvent{}, fmt.Errorf("wayback: llm status %d", resp.StatusCode)
	}
	var wrap struct {
		Response string `json:"response"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&wrap); err != nil {
		return ExtractedEvent{}, fmt.Errorf("wayback: decode llm wrapper: %w", err)
	}
	var ev ExtractedEvent
	if err := json.Unmarshal([]byte(wrap.Response), &ev); err != nil {
		return ExtractedEvent{}, fmt.Errorf("wayback: unmarshal extracted event: %w", err)
	}
	return ev, nil
}

var _ LLMClient = (*httpLLMClient)(nil)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `go test ./internal/worker/wayback/ -run TestHTTPLLMClient -v`
Expected: PASS (both subtests).

- [ ] **Step 6: Commit**

```bash
git add internal/worker/wayback/llm.go internal/worker/wayback/prompts/extract.txt internal/worker/wayback/llm_test.go
git commit -m "feat(control-plane): wayback LLM seam + ExtractedEvent contract"
```

---

## Task 4: Pure mapping — confidence, gate, enum clamping (`extract.go`)

No I/O. The accident gate, the deterministic confidence formula, and enum normalization.

**Files:**
- Create: `internal/worker/wayback/extract.go`
- Test: `internal/worker/wayback/extract_test.go`

**Interfaces:**
- Consumes: `ExtractedEvent` (Task 3).
- Produces:
  - `func HasCriticalFields(e ExtractedEvent) bool`
  - `func ConfidenceScore(e ExtractedEvent, official bool) int`
  - `func NormalizeEvent(e ExtractedEvent) ExtractedEvent` — clamps enum fields to allowed values (defaults otherwise).

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/extract_test.go`:

```go
package wayback

import "testing"

func intp(n int) *int { return &n }

func TestHasCriticalFields(t *testing.T) {
	ok := ExtractedEvent{Date: "2019-03-10", DatePrecision: "exact", AircraftType: "B738"}
	if !HasCriticalFields(ok) {
		t.Fatal("expected critical fields present")
	}
	noDate := ExtractedEvent{AircraftType: "B738"}
	if HasCriticalFields(noDate) {
		t.Fatal("missing date should fail gate")
	}
	noCraft := ExtractedEvent{Date: "2019", DatePrecision: "year"} // year precision is not usable
	if HasCriticalFields(noCraft) {
		t.Fatal("year precision + no aircraft should fail gate")
	}
}

func TestConfidenceScore(t *testing.T) {
	full := ExtractedEvent{
		Date: "2019-03-10", DatePrecision: "exact", Location: "Bishoftu",
		AircraftType: "B738", Fatalities: intp(157),
	}
	// 4/4 critical => base 80, +20 official => 100
	if got := ConfidenceScore(full, true); got != 100 {
		t.Fatalf("full official = %d, want 100", got)
	}
	// 4/4 critical, not official => 80
	if got := ConfidenceScore(full, false); got != 80 {
		t.Fatalf("full unofficial = %d, want 80", got)
	}
	// 2/4 (date + aircraft), official => round(0.5*80)=40 +20 = 60
	half := ExtractedEvent{Date: "2019-03-10", DatePrecision: "exact", AircraftType: "B738"}
	if got := ConfidenceScore(half, true); got != 60 {
		t.Fatalf("half official = %d, want 60", got)
	}
}

func TestNormalizeEvent(t *testing.T) {
	e := NormalizeEvent(ExtractedEvent{EventType: "crash", InvestigationStatus: "", ReportType: "weird", DatePrecision: ""})
	if e.EventType != "unknown" {
		t.Fatalf("event_type=%q want unknown", e.EventType)
	}
	if e.InvestigationStatus != "unknown" {
		t.Fatalf("investigation_status=%q want unknown", e.InvestigationStatus)
	}
	if e.ReportType != "final" {
		t.Fatalf("report_type=%q want final (default)", e.ReportType)
	}
	if e.DatePrecision != "unknown" {
		t.Fatalf("date_precision=%q want unknown", e.DatePrecision)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/worker/wayback/ -run 'TestHasCriticalFields|TestConfidenceScore|TestNormalizeEvent' -v`
Expected: FAIL — `undefined: HasCriticalFields` etc.

- [ ] **Step 3: Write the implementation**

Create `internal/worker/wayback/extract.go`:

```go
package wayback

import "math"

// HasCriticalFields is the accident-promotion gate: a usable date (exact or
// month precision) AND at least one of registration / aircraft type.
func HasCriticalFields(e ExtractedEvent) bool {
	usableDate := e.Date != "" && (e.DatePrecision == "exact" || e.DatePrecision == "month")
	hasCraft := e.AircraftRegistration != "" || e.AircraftType != ""
	return usableDate && hasCraft
}

// ConfidenceScore is deterministic: the fraction of four critical fields present,
// scaled to 80, plus a 20-point bonus when the source is an official AAI. Capped
// at 100.
func ConfidenceScore(e ExtractedEvent, official bool) int {
	critical := []bool{
		e.Date != "" && (e.DatePrecision == "exact" || e.DatePrecision == "month"),
		e.Location != "",
		e.AircraftType != "" || e.AircraftRegistration != "",
		e.Fatalities != nil,
	}
	n := 0
	for _, ok := range critical {
		if ok {
			n++
		}
	}
	base := int(math.Round(float64(n) / 4.0 * 80.0))
	if official {
		base += 20
	}
	if base > 100 {
		base = 100
	}
	return base
}

func normalizeEnum(val string, allowed []string, def string) string {
	for _, a := range allowed {
		if val == a {
			return val
		}
	}
	return def
}

// NormalizeEvent clamps enum-valued fields to the DB's allowed sets so a write
// never violates a CHECK constraint.
func NormalizeEvent(e ExtractedEvent) ExtractedEvent {
	e.EventType = normalizeEnum(e.EventType,
		[]string{"accident", "serious_incident", "incident", "hijacking", "unknown"}, "unknown")
	e.InvestigationStatus = normalizeEnum(e.InvestigationStatus,
		[]string{"final_report_available", "preliminary_report_available",
			"investigation_open", "no_report_found", "unknown"}, "unknown")
	e.ReportType = normalizeEnum(e.ReportType,
		[]string{"final", "preliminary", "interim", "factual"}, "final")
	e.DatePrecision = normalizeEnum(e.DatePrecision,
		[]string{"exact", "month", "year", "unknown"}, "unknown")
	return e
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `go test ./internal/worker/wayback/ -run 'TestHasCriticalFields|TestConfidenceScore|TestNormalizeEvent' -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/extract.go internal/worker/wayback/extract_test.go
git commit -m "feat(control-plane): wayback extraction gate, confidence, enum clamps"
```

---

## Task 5: Source resolution (`promote.go` part 1)

Per-regulator `sources` lookup-or-create from the country's authority, with the `wayback` fallback.

**Files:**
- Create: `internal/worker/wayback/promote.go`
- Test: `internal/worker/wayback/promote_source_test.go`

**Interfaces:**
- Produces: `func ResolveSource(ctx context.Context, q execQuerier, countryID int64, waybackTarget string) (sourceID int64, tier int, copyright string, err error)`
- where `type execQuerier interface { ExecContext(...); QueryRowContext(...) }` is satisfied by both `*sql.DB` and `*sql.Tx`.

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/promote_source_test.go`:

```go
package wayback

import (
	"context"
	"testing"
)

func TestResolveSourceOfficial(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	_, countryID := seedDownloadedDoc(t, db, "KE", "k1")
	// Author a national_aai authority for the country.
	_, err := db.ExecContext(ctx, `
		INSERT INTO authorities (country_id, normalized_name, name, type, website_url, archive_url, source_url, source_name)
		VALUES (?, 'aaid', 'AAID Kenya', 'national_aai', 'https://aaid.ke', 'https://aaid.ke/reports', 'https://aaid.ke', 'seed')`,
		countryID)
	if err != nil {
		t.Fatal(err)
	}
	id, tier, cr, err := ResolveSource(ctx, db, countryID, "aaid.ke")
	if err != nil {
		t.Fatalf("ResolveSource: %v", err)
	}
	if id == 0 || tier != 1 || cr != "official_public" {
		t.Fatalf("got id=%d tier=%d cr=%q", id, tier, cr)
	}
	// Second call reuses the same source (ON CONFLICT), no duplicate.
	id2, _, _, _ := ResolveSource(ctx, db, countryID, "aaid.ke")
	if id2 != id {
		t.Fatalf("second resolve made a new source: %d vs %d", id2, id)
	}
	var n int
	db.QueryRowContext(ctx, `SELECT count(*) FROM sources WHERE source_type='official_aai'`).Scan(&n)
	if n != 1 {
		t.Fatalf("expected 1 official_aai source, got %d", n)
	}
}

func TestResolveSourceWaybackFallback(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	_, countryID := seedDownloadedDoc(t, db, "ZW", "z1") // no authority
	id, tier, cr, err := ResolveSource(ctx, db, countryID, "caa.gov.zw")
	if err != nil {
		t.Fatalf("ResolveSource: %v", err)
	}
	if id == 0 || tier != 2 || cr != "unknown" {
		t.Fatalf("fallback got id=%d tier=%d cr=%q", id, tier, cr)
	}
	var st string
	db.QueryRowContext(ctx, `SELECT source_type FROM sources WHERE id=?`, id).Scan(&st)
	if st != "wayback" {
		t.Fatalf("fallback source_type=%q want wayback", st)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/worker/wayback/ -run TestResolveSource -v`
Expected: FAIL — `undefined: ResolveSource`.

- [ ] **Step 3: Write the implementation**

Create `internal/worker/wayback/promote.go`:

```go
package wayback

import (
	"context"
	"database/sql"
	"fmt"
)

// execQuerier is satisfied by *sql.DB and *sql.Tx, so promotion helpers work
// inside or outside a transaction.
type execQuerier interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// ResolveSource returns the source to credit for a recovered report. It prefers
// the country's national_aai authority (else caa) as an official_aai tier-1
// source; failing that it falls back to a per-country wayback tier-2 source built
// from waybackTarget. Lookup-or-create keys on UNIQUE(canonical_url, source_type).
func ResolveSource(ctx context.Context, q execQuerier, countryID int64, waybackTarget string) (int64, int, string, error) {
	var name, website, archive sql.NullString
	err := q.QueryRowContext(ctx, `
		SELECT name, website_url, archive_url FROM authorities
		 WHERE country_id = ? AND type IN ('national_aai','caa')
		 ORDER BY CASE type WHEN 'national_aai' THEN 0 ELSE 1 END, id ASC
		 LIMIT 1`, countryID).Scan(&name, &website, &archive)
	if err != nil && err != sql.ErrNoRows {
		return 0, 0, "", fmt.Errorf("wayback: lookup authority %d: %w", countryID, err)
	}

	if err == nil && name.Valid {
		canonical := archive.String
		if canonical == "" {
			canonical = website.String
		}
		if canonical != "" {
			id, e := upsertSource(ctx, q, name.String, website.String, canonical, "official_aai", 1)
			if e != nil {
				return 0, 0, "", e
			}
			return id, 1, "official_public", nil
		}
	}

	// Fallback: wayback source from the target domain.
	canonical := "wayback://" + waybackTarget
	id, e := upsertSource(ctx, q, "Internet Archive: "+waybackTarget, "https://"+waybackTarget, canonical, "wayback", 2)
	if e != nil {
		return 0, 0, "", e
	}
	return id, 2, "unknown", nil
}

func upsertSource(ctx context.Context, q execQuerier, name, url, canonical, sourceType string, tier int) (int64, error) {
	if _, err := q.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(canonical_url, source_type) DO NOTHING`,
		name, url, canonical, sourceType, tier); err != nil {
		return 0, fmt.Errorf("wayback: upsert source %s: %w", canonical, err)
	}
	var id int64
	if err := q.QueryRowContext(ctx, `
		SELECT id FROM sources WHERE canonical_url = ? AND source_type = ?`,
		canonical, sourceType).Scan(&id); err != nil {
		return 0, fmt.Errorf("wayback: select source %s: %w", canonical, err)
	}
	return id, nil
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `go test ./internal/worker/wayback/ -run TestResolveSource -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/promote.go internal/worker/wayback/promote_source_test.go
git commit -m "feat(control-plane): wayback per-regulator source resolution"
```

---

## Task 6: Deterministic dedup (`promote.go` part 2)

Match an extracted event against existing `events` by key-1 (date+registration) then key-2 (date+operator+fatalities).

**Files:**
- Modify: `internal/worker/wayback/promote.go`
- Test: `internal/worker/wayback/promote_dedup_test.go`

**Interfaces:**
- Produces: `func FindDuplicateEvent(ctx context.Context, q execQuerier, e ExtractedEvent) (eventID int64, found bool, err error)`
- Helper produced: `func normalizeReg(s string) string` (upper + trim).

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/promote_dedup_test.go`:

```go
package wayback

import (
	"context"
	"testing"
)

func insertEvent(t *testing.T, db execQuerier, date, reg, operator string, fatalities *int) int64 {
	t.Helper()
	res, err := db.ExecContext(context.Background(), `
		INSERT INTO events (date, date_precision, aircraft_registration, operator_name, fatalities, confidence_score)
		VALUES (?, 'exact', ?, ?, ?, 50)`, date, reg, operator, fatalities)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}

func TestFindDuplicateEventKey1(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	want := insertEvent(t, db, "2019-03-10", "ET-AVJ", "Ethiopian", intp(157))

	cand := ExtractedEvent{Date: "2019-03-10", DatePrecision: "exact", AircraftRegistration: "et-avj "}
	id, found, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || !found || id != want {
		t.Fatalf("key1 dedup: id=%d found=%v err=%v want %d", id, found, err, want)
	}
}

func TestFindDuplicateEventKey2(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	want := insertEvent(t, db, "2018-05-01", "", "AeroX", intp(3))

	// No registration on candidate -> key-2 (date+operator+fatalities).
	cand := ExtractedEvent{Date: "2018-05-01", DatePrecision: "exact", OperatorName: "AeroX", Fatalities: intp(3)}
	id, found, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || !found || id != want {
		t.Fatalf("key2 dedup: id=%d found=%v err=%v want %d", id, found, err, want)
	}
}

func TestFindDuplicateEventNoMatch(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	insertEvent(t, db, "2019-03-10", "ET-AVJ", "Ethiopian", intp(157))

	cand := ExtractedEvent{Date: "2020-01-01", DatePrecision: "exact", AircraftRegistration: "N12345"}
	_, found, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || found {
		t.Fatalf("expected no match: found=%v err=%v", found, err)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/worker/wayback/ -run TestFindDuplicateEvent -v`
Expected: FAIL — `undefined: FindDuplicateEvent`.

- [ ] **Step 3: Append the implementation to `promote.go`**

Add to `internal/worker/wayback/promote.go`:

```go
import "strings" // add to the existing import block

// normalizeReg upper-cases and trims an aircraft registration for comparison.
func normalizeReg(s string) string {
	return strings.ToUpper(strings.TrimSpace(s))
}

// FindDuplicateEvent looks for an existing event that is the same occurrence.
// Key 1 (when the candidate has a registration): same exact date AND same
// normalized registration. Key 2 (when registration is absent): same exact date
// AND same operator AND same fatalities. Only exact-precision candidate dates
// participate.
func FindDuplicateEvent(ctx context.Context, q execQuerier, e ExtractedEvent) (int64, bool, error) {
	if e.DatePrecision != "exact" || e.Date == "" {
		return 0, false, nil
	}
	reg := normalizeReg(e.AircraftRegistration)
	if reg != "" {
		var id int64
		err := q.QueryRowContext(ctx, `
			SELECT id FROM events
			 WHERE date = ? AND upper(trim(aircraft_registration)) = ?
			 ORDER BY id ASC LIMIT 1`, e.Date, reg).Scan(&id)
		if err == sql.ErrNoRows {
			return 0, false, nil
		}
		if err != nil {
			return 0, false, fmt.Errorf("wayback: dedup key1: %w", err)
		}
		return id, true, nil
	}
	if e.OperatorName != "" && e.Fatalities != nil {
		var id int64
		err := q.QueryRowContext(ctx, `
			SELECT id FROM events
			 WHERE date = ? AND operator_name = ? AND fatalities = ?
			 ORDER BY id ASC LIMIT 1`, e.Date, e.OperatorName, *e.Fatalities).Scan(&id)
		if err == sql.ErrNoRows {
			return 0, false, nil
		}
		if err != nil {
			return 0, false, fmt.Errorf("wayback: dedup key2: %w", err)
		}
		return id, true, nil
	}
	return 0, false, nil
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `go test ./internal/worker/wayback/ -run TestFindDuplicateEvent -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/promote.go internal/worker/wayback/promote_dedup_test.go
git commit -m "feat(control-plane): wayback deterministic event dedup"
```

---

## Task 7: Promotion transaction (`promote.go` part 3)

Tie source + dedup + confidence together: insert/link `events`, insert `reports`, set the document's `event_id` and `extraction_status='extracted'` — all in one transaction.

**Files:**
- Modify: `internal/worker/wayback/promote.go`
- Test: `internal/worker/wayback/promote_test.go`

**Interfaces:**
- Consumes: `ResolveSource`, `FindDuplicateEvent`, `ConfidenceScore`, `NormalizeEvent`, and the `ExtractDoc` type (define it here so Task 8 can reuse it).
- Produces:
  - `type ExtractDoc struct { ID, CountryID int64; ISO2, Digest, LocalFilePath, OriginalURL, ArchivedURL string; OCRTextPath sql.NullString; Checksum sql.NullString; WaybackTarget string; Attempts int }`
  - `func PromoteDocument(ctx context.Context, db *sql.DB, doc ExtractDoc, e ExtractedEvent) (eventID int64, linked bool, err error)`

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/promote_test.go`:

```go
package wayback

import (
	"context"
	"database/sql"
	"testing"
)

func loadDoc(t *testing.T, db *sql.DB, docID int64) ExtractDoc {
	t.Helper()
	var d ExtractDoc
	d.ID = docID
	err := db.QueryRowContext(context.Background(), `
		SELECT d.country_id, c.iso2, d.digest, d.local_file_path, d.original_url, d.archived_url,
		       d.ocr_text_path, d.checksum, coalesce(c.wayback_target,'')
		  FROM staged_wayback_documents d JOIN countries c ON c.id=d.country_id
		 WHERE d.id=?`, docID).
		Scan(&d.CountryID, &d.ISO2, &d.Digest, &d.LocalFilePath, &d.OriginalURL, &d.ArchivedURL,
			&d.OCRTextPath, &d.Checksum, &d.WaybackTarget)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestPromoteDocumentNewEvent(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	doc := loadDoc(t, db, docID)

	e := NormalizeEvent(ExtractedEvent{
		IsAviationAccident: true, Date: "2019-03-10", DatePrecision: "exact",
		Location: "Bishoftu", AircraftRegistration: "ET-AVJ", AircraftType: "B738",
		OperatorName: "Ethiopian", Fatalities: intp(157), EventType: "accident",
		ReportType: "final", Title: "Final Report", Language: "en",
	})
	eventID, linked, err := PromoteDocument(ctx, db, doc, e)
	if err != nil {
		t.Fatalf("PromoteDocument: %v", err)
	}
	if eventID == 0 || linked {
		t.Fatalf("want new event, got id=%d linked=%v", eventID, linked)
	}
	// events row: confidence, dedup_status.
	var conf int
	var dedup string
	db.QueryRowContext(ctx, `SELECT confidence_score, dedup_status FROM events WHERE id=?`, eventID).Scan(&conf, &dedup)
	if conf == 0 || dedup != "unreviewed" {
		t.Fatalf("event conf=%d dedup=%q", conf, dedup)
	}
	// reports row: archived_url + copyright_status.
	var arch, cr string
	db.QueryRowContext(ctx, `SELECT archived_url, copyright_status FROM reports WHERE event_id=?`, eventID).Scan(&arch, &cr)
	if arch != doc.ArchivedURL || cr == "" {
		t.Fatalf("report arch=%q cr=%q", arch, cr)
	}
	// staged doc advanced.
	var status string
	var linkedEvent sql.NullInt64
	db.QueryRowContext(ctx, `SELECT extraction_status, event_id FROM staged_wayback_documents WHERE id=?`, docID).Scan(&status, &linkedEvent)
	if status != "extracted" || !linkedEvent.Valid || linkedEvent.Int64 != eventID {
		t.Fatalf("doc status=%q event_id=%v", status, linkedEvent)
	}
}

func TestPromoteDocumentLinksDuplicate(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	existing := insertEvent(t, db, "2019-03-10", "ET-AVJ", "Ethiopian", intp(157))
	docID, _ := seedDownloadedDoc(t, db, "KE", "k2")
	doc := loadDoc(t, db, docID)

	e := NormalizeEvent(ExtractedEvent{
		IsAviationAccident: true, Date: "2019-03-10", DatePrecision: "exact",
		AircraftRegistration: "ET-AVJ", AircraftType: "B738", ReportType: "final", Language: "en",
	})
	eventID, linked, err := PromoteDocument(ctx, db, doc, e)
	if err != nil {
		t.Fatalf("PromoteDocument: %v", err)
	}
	if !linked || eventID != existing {
		t.Fatalf("want link to %d, got id=%d linked=%v", existing, eventID, linked)
	}
	var dedup string
	db.QueryRowContext(ctx, `SELECT dedup_status FROM events WHERE id=?`, existing).Scan(&dedup)
	if dedup != "soft_linked" {
		t.Fatalf("dedup_status=%q want soft_linked", dedup)
	}
	// No second event created.
	var n int
	db.QueryRowContext(ctx, `SELECT count(*) FROM events`).Scan(&n)
	if n != 1 {
		t.Fatalf("event count=%d want 1", n)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/worker/wayback/ -run TestPromoteDocument -v`
Expected: FAIL — `undefined: ExtractDoc` / `undefined: PromoteDocument`.

- [ ] **Step 3: Append the implementation to `promote.go`**

Add to `internal/worker/wayback/promote.go`:

```go
// ExtractDoc is a staged document ready for the extract step.
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
}

// PromoteDocument inserts or links an event, inserts a report, and advances the
// document to 'extracted', all in one transaction. Returns the event id and
// whether it linked to an existing event.
func PromoteDocument(ctx context.Context, db *sql.DB, doc ExtractDoc, e ExtractedEvent) (int64, bool, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, false, fmt.Errorf("wayback: promote begin tx: %w", err)
	}
	defer tx.Rollback()

	sourceID, tier, copyright, err := ResolveSource(ctx, tx, doc.CountryID, doc.WaybackTarget)
	if err != nil {
		return 0, false, err
	}
	official := tier == 1

	eventID, linked, err := FindDuplicateEvent(ctx, tx, e)
	if err != nil {
		return 0, false, err
	}
	if linked {
		if _, err := tx.ExecContext(ctx, `
			UPDATE events SET dedup_status='soft_linked', updated_at=unixepoch('subsec')*1000
			 WHERE id=? AND dedup_status='unreviewed'`, eventID); err != nil {
			return 0, false, fmt.Errorf("wayback: soft-link event %d: %w", eventID, err)
		}
	} else {
		conf := ConfidenceScore(e, official)
		res, err := tx.ExecContext(ctx, `
			INSERT INTO events
				(date, date_precision, occurrence_country_id, location, latitude, longitude,
				 aircraft_registration, aircraft_type, manufacturer, operator_name, flight_number,
				 fatalities, injuries, event_type, investigation_status, confidence_score, dedup_status)
			VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'unreviewed')`,
			nullStr(e.Date), e.DatePrecision, doc.CountryID, nullStr(e.Location), e.Latitude, e.Longitude,
			nullStr(e.AircraftRegistration), nullStr(e.AircraftType), nullStr(e.Manufacturer),
			nullStr(e.OperatorName), nullStr(e.FlightNumber), e.Fatalities, e.Injuries,
			e.EventType, e.InvestigationStatus, conf)
		if err != nil {
			return 0, false, fmt.Errorf("wayback: insert event: %w", err)
		}
		eventID, _ = res.LastInsertId()
	}

	title := e.Title
	if title == "" {
		title = doc.ISO2 + " accident report"
	}
	language := e.Language
	if language == "" {
		language = "en"
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO reports
			(event_id, source_id, report_type, title, language, original_url, archived_url, pdf_url,
			 published_date, accessed_at, checksum, local_file_path, source_tier, extraction_status, copyright_status)
		VALUES (?,?,?,?,?,?,?,?,?, unixepoch('subsec')*1000, ?, ?, ?, 'extracted', ?)`,
		eventID, sourceID, e.ReportType, title, language, doc.OriginalURL, doc.ArchivedURL, doc.OriginalURL,
		nullStr(e.PublishedDate), doc.Checksum, doc.LocalFilePath, tier, copyright); err != nil {
		return 0, false, fmt.Errorf("wayback: insert report: %w", err)
	}

	if _, err := tx.ExecContext(ctx, `
		UPDATE staged_wayback_documents SET event_id=?, extraction_status='extracted' WHERE id=?`,
		eventID, doc.ID); err != nil {
		return 0, false, fmt.Errorf("wayback: mark extracted %d: %w", doc.ID, err)
	}

	if err := tx.Commit(); err != nil {
		return 0, false, fmt.Errorf("wayback: promote commit: %w", err)
	}
	return eventID, linked, nil
}

// nullStr returns nil for an empty string so an empty optional column stays NULL.
func nullStr(s string) any {
	if s == "" {
		return nil
	}
	return s
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `go test ./internal/worker/wayback/ -run TestPromoteDocument -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/promote.go internal/worker/wayback/promote_test.go
git commit -m "feat(control-plane): wayback promotion transaction (events+reports)"
```

---

## Task 8: State machine + batch runner (`extractrunner.go`)

`ExtractOne` runs a single document through OCR (if needed) then extract+promote, recording failures with the attempt counter. `ProcessExtractPending` selects and runs a batch.

**Files:**
- Create: `internal/worker/wayback/extractrunner.go`
- Test: `internal/worker/wayback/extractrunner_test.go`

**Interfaces:**
- Consumes: `OCRClient`, `LLMClient`, `PersistOCRText`, `PromoteDocument`, `HasCriticalFields`, `NormalizeEvent`, `ExtractDoc`.
- Produces:
  - `type ExtractStats struct { OCRDone, Extracted, Skipped, Failed int }`
  - `func ExtractOne(ctx context.Context, db *sql.DB, ocr OCRClient, llm LLMClient, storeDir string, doc ExtractDoc) (string, error)` — returns the terminal status for that doc (`extracted`/`skipped`/`failed`/`ocr_done`).
  - `func ProcessExtractPending(ctx context.Context, db *sql.DB, ocr OCRClient, llm LLMClient, storeDir string, limit int) (ExtractStats, error)`

- [ ] **Step 1: Write the failing test**

Create `internal/worker/wayback/extractrunner_test.go`:

```go
package wayback

import (
	"context"
	"errors"
	"os"
	"testing"
)

func goodEvent() ExtractedEvent {
	return ExtractedEvent{
		IsAviationAccident: true, Date: "2019-03-10", DatePrecision: "exact",
		Location: "Bishoftu", AircraftRegistration: "ET-AVJ", AircraftType: "B738",
		Fatalities: intp(157), EventType: "accident", ReportType: "final", Language: "en",
	}
}

func TestExtractOneHappyPath(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	// Put a real PDF file where local_file_path points (the runner reads it).
	writePDF(t, db, docID)
	doc := loadDoc(t, db, docID)

	status, err := ExtractOne(ctx, db, &fixtureOCRClient{Text: "REPORT"}, &fixtureLLMClient{Event: goodEvent()}, t.TempDir(), doc)
	if err != nil {
		t.Fatalf("ExtractOne: %v", err)
	}
	if status != "extracted" {
		t.Fatalf("status=%q want extracted", status)
	}
	var n int
	db.QueryRowContext(ctx, `SELECT count(*) FROM events`).Scan(&n)
	if n != 1 {
		t.Fatalf("events=%d want 1", n)
	}
}

func TestExtractOneSkipsNonAccident(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	writePDF(t, db, docID)
	doc := loadDoc(t, db, docID)

	status, err := ExtractOne(ctx, db, &fixtureOCRClient{Text: "INDEX"}, &fixtureLLMClient{Event: ExtractedEvent{IsAviationAccident: false}}, t.TempDir(), doc)
	if err != nil {
		t.Fatalf("ExtractOne: %v", err)
	}
	if status != "skipped" {
		t.Fatalf("status=%q want skipped", status)
	}
}

func TestExtractOneOCRFailureCountsAttempt(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	writePDF(t, db, docID)
	doc := loadDoc(t, db, docID)

	status, err := ExtractOne(ctx, db, &fixtureOCRClient{Err: errors.New("boom")}, &fixtureLLMClient{}, t.TempDir(), doc)
	if err != nil {
		t.Fatalf("ExtractOne returned err: %v", err) // data failures are recorded, not returned
	}
	if status != "failed" {
		t.Fatalf("status=%q want failed", status)
	}
	var attempts int
	var estatus string
	db.QueryRowContext(ctx, `SELECT extraction_attempts, extraction_status FROM staged_wayback_documents WHERE id=?`, docID).Scan(&attempts, &estatus)
	if attempts != 1 || estatus != "failed" {
		t.Fatalf("attempts=%d status=%q", attempts, estatus)
	}
}

func TestProcessExtractPendingResumesFromOCRText(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	// Simulate a crashed-after-OCR doc: text already persisted, status ocr_done.
	store := t.TempDir()
	if _, err := PersistOCRText(ctx, db, store, "KE", "k1", docID, "REPORT"); err != nil {
		t.Fatal(err)
	}
	// OCR client that would error if called — proves OCR is skipped on resume.
	stats, err := ProcessExtractPending(ctx, db, &fixtureOCRClient{Err: errors.New("should not be called")},
		&fixtureLLMClient{Event: goodEvent()}, store, 0)
	if err != nil {
		t.Fatalf("ProcessExtractPending: %v", err)
	}
	if stats.Extracted != 1 {
		t.Fatalf("stats=%+v want Extracted 1", stats)
	}
}

// writePDF creates a file at the doc's local_file_path so the runner can read it.
func writePDF(t *testing.T, db interface {
	QueryRowContext(ctx context.Context, q string, a ...any) *rowScanner
}, docID int64) {
	// replaced below — see note
}
```

> **Note on `writePDF`:** the inline signature above is illustrative. Implement `writePDF` simply: read `local_file_path` for the doc, `os.MkdirAll` its dir, and `os.WriteFile` a few bytes. Concretely:

```go
func writePDF(t *testing.T, db *sql.DB, docID int64) {
	t.Helper()
	var p string
	if err := db.QueryRowContext(context.Background(),
		`SELECT local_file_path FROM staged_wayback_documents WHERE id=?`, docID).Scan(&p); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepathDir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("%PDF-1.4 fake"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func filepathDir(p string) string { return filepath.Dir(p) }
```

Add `"path/filepath"` to the test imports and delete the illustrative stub. (The seed helper writes `local_file_path` as `/store/<iso2>/<digest>.pdf`; since the runner reads that exact path, point the seed at a temp dir or have `writePDF` create the parent — `os.MkdirAll` handles it. If `/store` is not writable in CI, change `seedDownloadedDoc` to use `t.TempDir()` for `local_file_path` — update the helper accordingly.)

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/worker/wayback/ -run 'TestExtractOne|TestProcessExtractPending' -v`
Expected: FAIL — `undefined: ExtractOne` / `undefined: ProcessExtractPending`.

- [ ] **Step 3: Write the implementation**

Create `internal/worker/wayback/extractrunner.go`:

```go
package wayback

import (
	"context"
	"database/sql"
	"fmt"
	"os"
)

// ExtractStats is the aggregate result of a batch run.
type ExtractStats struct {
	OCRDone   int
	Extracted int
	Skipped   int
	Failed    int
}

// ExtractOne runs one document through the state machine: OCR (when no text
// artifact yet) then extract+promote. Data-level failures are recorded on the
// row (status='failed', attempt++, crawl_errors) and returned as status without
// an error; only unexpected DB failures return an error.
func ExtractOne(ctx context.Context, db *sql.DB, ocr OCRClient, llm LLMClient, storeDir string, doc ExtractDoc) (string, error) {
	// OCR step.
	if !doc.OCRTextPath.Valid || doc.OCRTextPath.String == "" {
		pdf, err := os.ReadFile(doc.LocalFilePath)
		if err != nil {
			return recordExtractFailure(ctx, db, doc, doc.LocalFilePath, err)
		}
		text, err := ocr.OCR(ctx, pdf)
		if err != nil {
			return recordExtractFailure(ctx, db, doc, doc.ArchivedURL, err)
		}
		path, err := PersistOCRText(ctx, db, storeDir, doc.ISO2, doc.Digest, doc.ID, text)
		if err != nil {
			return "", err
		}
		doc.OCRTextPath = sql.NullString{String: path, Valid: true}
	}

	// Extract step.
	text, err := os.ReadFile(doc.OCRTextPath.String)
	if err != nil {
		return recordExtractFailure(ctx, db, doc, doc.OCRTextPath.String, err)
	}
	raw, err := llm.Extract(ctx, string(text))
	if err != nil {
		return recordExtractFailure(ctx, db, doc, doc.ArchivedURL, err)
	}
	e := NormalizeEvent(raw)
	if !raw.IsAviationAccident || !HasCriticalFields(e) {
		if _, err := db.ExecContext(ctx,
			`UPDATE staged_wayback_documents SET extraction_status='skipped' WHERE id=?`, doc.ID); err != nil {
			return "", fmt.Errorf("wayback: mark skipped %d: %w", doc.ID, err)
		}
		return "skipped", nil
	}
	if _, _, err := PromoteDocument(ctx, db, doc, e); err != nil {
		return "", err
	}
	return "extracted", nil
}

// recordExtractFailure marks the row failed, bumps the attempt counter, and logs
// a crawl_errors row against the document's crawl_job. Returns status "failed".
func recordExtractFailure(ctx context.Context, db *sql.DB, doc ExtractDoc, url string, cause error) (string, error) {
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_wayback_documents
		   SET extraction_status='failed', extraction_error=?, extraction_attempts=extraction_attempts+1
		 WHERE id=?`, cause.Error(), doc.ID); err != nil {
		return "", fmt.Errorf("wayback: mark failed %d: %w", doc.ID, err)
	}
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message)
		SELECT crawl_job_id, ?, 'unknown', ? FROM staged_wayback_documents WHERE id=?`,
		url, cause.Error(), doc.ID)
	return "failed", nil
}

// ProcessExtractPending runs up to limit documents needing extraction, highest
// country priority first. limit <= 0 means no cap.
func ProcessExtractPending(ctx context.Context, db *sql.DB, ocr OCRClient, llm LLMClient, storeDir string, limit int) (ExtractStats, error) {
	q := `
		SELECT d.id, d.country_id, c.iso2, d.digest, d.local_file_path, d.original_url,
		       d.archived_url, d.ocr_text_path, d.checksum, coalesce(c.wayback_target,''),
		       d.extraction_attempts
		  FROM staged_wayback_documents d
		  JOIN countries c ON c.id = d.country_id
		 WHERE d.download_status = 'downloaded'
		   AND d.extraction_status IN ('pending','ocr_done','failed')
		   AND d.extraction_attempts < 3
		 ORDER BY c.priority_score DESC, d.id ASC`
	if limit > 0 {
		q += fmt.Sprintf(" LIMIT %d", limit)
	}
	rows, err := db.QueryContext(ctx, q)
	if err != nil {
		return ExtractStats{}, fmt.Errorf("wayback: select pending extract docs: %w", err)
	}
	var docs []ExtractDoc
	for rows.Next() {
		var d ExtractDoc
		if err := rows.Scan(&d.ID, &d.CountryID, &d.ISO2, &d.Digest, &d.LocalFilePath, &d.OriginalURL,
			&d.ArchivedURL, &d.OCRTextPath, &d.Checksum, &d.WaybackTarget, &d.Attempts); err != nil {
			rows.Close()
			return ExtractStats{}, fmt.Errorf("wayback: scan extract doc: %w", err)
		}
		docs = append(docs, d)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return ExtractStats{}, err
	}
	rows.Close()

	var stats ExtractStats
	for _, d := range docs {
		status, err := ExtractOne(ctx, db, ocr, llm, storeDir, d)
		if err != nil {
			return stats, err
		}
		switch status {
		case "extracted":
			stats.Extracted++
		case "skipped":
			stats.Skipped++
		case "failed":
			stats.Failed++
		case "ocr_done":
			stats.OCRDone++
		}
	}
	return stats, nil
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `go test ./internal/worker/wayback/ -run 'TestExtractOne|TestProcessExtractPending' -v`
Expected: PASS.

Then run the whole package:
Run: `go test ./internal/worker/wayback/...`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/worker/wayback/extractrunner.go internal/worker/wayback/extractrunner_test.go
git commit -m "feat(control-plane): wayback extract state machine + batch runner"
```

---

## Task 9: CLI wiring + README (`process-wayback-extract`)

Wire the subcommand and document it.

**Files:**
- Modify: `internal/app/app.go` (dispatch switch + new `runProcessWaybackExtract`)
- Modify: `README.md`
- Test: `internal/app/app_wayback_extract_test.go`

**Interfaces:**
- Consumes: `wayback.ProcessExtractPending`, `wayback.NewHTTPOCRClient`, `wayback.NewHTTPLLMClient`.

- [ ] **Step 1: Write the failing test**

Create `internal/app/app_wayback_extract_test.go` (mirror `app_wayback_test.go`):

```go
package app

import (
	"bytes"
	"context"
	"strings"
	"testing"
)

func TestRunProcessWaybackExtractRequiresDB(t *testing.T) {
	var stderr bytes.Buffer
	code := Run(context.Background(), []string{"process-wayback-extract"}, &bytes.Buffer{}, &stderr)
	if code == 0 {
		t.Fatal("expected non-zero exit without --db")
	}
	if !strings.Contains(stderr.String(), "--db is required") {
		t.Fatalf("stderr=%q", stderr.String())
	}
}
```

> Check `app_wayback_test.go` for the exact `Run` signature and exit-code constants; match them. If the existing test calls a differently-named entry point, mirror that.

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/app/ -run TestRunProcessWaybackExtract -v`
Expected: FAIL — unknown command `process-wayback-extract` (exit usage, but the "--db is required" string is absent).

- [ ] **Step 3: Wire the command**

In `internal/app/app.go`, add to the dispatch switch (next to `case "process-wayback":`):

```go
	case "process-wayback-extract":
		return runProcessWaybackExtract(ctx, rest, stderr)
```

Update both `commands:` usage lines to append `, process-wayback-extract`.

Add the handler (after `runProcessWayback`):

```go
// ── process-wayback-extract ──────────────────────────────────────────────────

func runProcessWaybackExtract(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("process-wayback-extract", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	limit := fs.Int("limit", 0, "max documents to process (0 = no cap)")
	storeDir := fs.String("store-dir", "./wayback-store", "directory for OCR text artifacts")
	ocrEndpoint := fs.String("ocr-endpoint", "http://127.0.0.1:8021/ocr", "OCR HTTP endpoint")
	llmEndpoint := fs.String("llm-endpoint", "http://127.0.0.1:11434/api/generate", "Ollama generate endpoint")
	llmModel := fs.String("llm-model", "qwen3.6-rw", "LLM model name")
	maxInputChars := fs.Int("max-input-chars", 24000, "truncate OCR text to this many chars before LLM")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "process-wayback-extract: --db is required")
		fs.Usage()
		return exitUsage
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "process-wayback-extract: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	ocr := wayback.NewHTTPOCRClient(*ocrEndpoint, 600*time.Second)
	llm := wayback.NewHTTPLLMClient(*llmEndpoint, *llmModel, *maxInputChars, 120*time.Second)
	stats, err := wayback.ProcessExtractPending(ctx, db, ocr, llm, *storeDir, *limit)
	if err != nil {
		fmt.Fprintf(stderr, "process-wayback-extract: %v\n", err)
		return exitFailure
	}
	fmt.Fprintf(stderr, "extracted=%d skipped=%d failed=%d ocr_done=%d\n",
		stats.Extracted, stats.Skipped, stats.Failed, stats.OCRDone)
	return exitOK
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `go test ./internal/app/ -run TestRunProcessWaybackExtract -v`
Expected: PASS.

- [ ] **Step 5: Document in README**

Add a `### process-wayback-extract` section to `README.md` after the `process-wayback` section:

````markdown
### process-wayback-extract

Drains downloaded Wayback PDFs (`staged_wayback_documents.download_status='downloaded'`)
into structured `events`/`reports`. Per document, highest-country-priority first, it
OCRs the PDF (persisting the text under `<store-dir>/<iso2>/<digest>.txt`), extracts
event fields with an LLM, and promotes the result with a deterministic confidence
score and deterministic dedup.

```
./aviation-coverage process-wayback-extract --db coverage.db --limit 50 \
  --store-dir ./wayback-store \
  --ocr-endpoint https://<ocr-host>/ocr \
  --llm-endpoint http://127.0.0.1:11434/api/generate --llm-model qwen3.6-rw
```

A document is `extracted` (promoted), `skipped` (not an aviation accident or missing
critical fields), or `failed` (OCR/LLM error; retried until `extraction_attempts`
reaches 3). The resume point is decided by `ocr_text_path` — a re-run never repeats a
completed OCR. The OCR endpoint is a thin HTTP wrapper around `ocrmypdf` (see the
spec, §9); endpoints are passed by flag and are never hardcoded.
````

- [ ] **Step 6: Run the whole suite and commit**

Run: `go test ./...`
Expected: PASS.

```bash
git add internal/app/app.go internal/app/app_wayback_extract_test.go README.md
git commit -m "feat(control-plane): wire process-wayback-extract subcommand + docs"
```

---

## Task 10 (out-of-band): hetzner OCR HTTP service

**Not a Go task and not in this repo.** A small FastAPI route wrapping the existing
`ocrmypdf` invocation, required only for the live smoke run. Tracked here so it is
not forgotten; build/deploy it separately on hetzner.

**Deliverable sketch** (deploy under the existing OCR tooling, run as user `a1`):

```python
# ocr_service.py — POST a PDF body, get text back. Bind 127.0.0.1; expose via the
# existing reverse tunnel / nginx, never publicly. Run under nice/ionice.
from fastapi import FastAPI, Request, Response
import subprocess, tempfile, os

app = FastAPI()

@app.post("/ocr")
async def ocr(req: Request):
    pdf = await req.body()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf); path = f.name
    sidecar = path + ".txt"
    try:
        subprocess.run(
            ["nice", "-n", "19", "ionice", "-c3",
             "ocrmypdf", "--force-ocr", "--language", "eng+spa+fra+rus",
             "--sidecar", sidecar, "--output-type", "none", path, "-"],
            check=True, timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(sidecar) as s:
            return Response(content=s.read(), media_type="text/plain")
    except Exception:
        return Response(content="", media_type="text/plain")
    finally:
        for p in (path, sidecar):
            try: os.remove(p)
            except OSError: pass
```

- [ ] **Step 1:** Deploy the service on hetzner (bound to localhost, reachable only via the existing tunnel/nginx), run as `a1`.
- [ ] **Step 2:** Smoke-check: `curl --data-binary @sample.pdf -H 'Content-Type: application/pdf' http://127.0.0.1:8021/ocr` returns text.
- [ ] **Step 3:** Record the internal endpoint URL for the `--ocr-endpoint` flag (kept out of the repo).

---

## Live smoke (after Task 9, optional in this branch)

With the OCR service (Task 10) up and an Ollama reachable, run a tiny batch against
a real coverage DB that already has downloaded docs:

```
./aviation-coverage process-wayback-extract --db coverage.db --limit 3 \
  --ocr-endpoint <internal-ocr-url> \
  --llm-endpoint <ollama-url>/api/generate --llm-model qwen3.6-rw
```

Verify: the three documents end `extracted`/`skipped`, `events`/`reports` rows
appear with sane fields, `archived_url` is populated, and confidence scores are in
range. Capture the output for the PR description.

---

## Self-Review (completed)

- **Spec coverage:** §3 seams → Tasks 2,3; §4 migration → Task 1; §5 worker/state
  machine → Task 8; §5.1 gate → Task 4; §5.2 confidence → Task 4; §5.3 report row →
  Task 7; §6 error handling/attempts → Task 8; §7 source+dedup → Tasks 5,6,7; §8 LLM
  contract → Task 3; §9 OCR service → Task 10; §10 tests → each task's tests; §11
  files → all tasks. No gaps.
- **Placeholders:** none — every code/test step carries full code. (Task 8's
  `writePDF` illustrative stub is explicitly replaced with concrete code in the same
  step.)
- **Type consistency:** `OCRClient.OCR(ctx,[]byte)(string,error)`,
  `LLMClient.Extract(ctx,string)(ExtractedEvent,error)`, `ExtractDoc`,
  `PromoteDocument(...)→(int64,bool,error)`, `ProcessExtractPending(...)→(ExtractStats,error)`
  are used identically across Tasks 2–9.
