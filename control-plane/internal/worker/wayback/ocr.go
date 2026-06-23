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
