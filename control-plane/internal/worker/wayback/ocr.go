package wayback

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/worker/extract"
)

type httpOCRClient struct {
	endpoint string
	client   *http.Client
}

// httpOCRClient structurally satisfies extract.OCRClient.
var _ extract.OCRClient = (*httpOCRClient)(nil)

// NewHTTPOCRClient returns an OCR client that POSTs the PDF bytes to endpoint and
// reads back the extracted text as the response body. The returned concrete type
// structurally satisfies extract.OCRClient (no import of extract needed).
func NewHTTPOCRClient(endpoint string, timeout time.Duration) *httpOCRClient {
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
