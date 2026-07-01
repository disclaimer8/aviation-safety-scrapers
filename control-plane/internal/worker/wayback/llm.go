package wayback

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
	"unicode/utf8"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/worker/extract"
)

//go:embed prompts/extract.txt
var extractPromptTemplate string

type httpLLMClient struct {
	endpoint string
	model    string
	maxChars int
	client   *http.Client
}

// NewHTTPLLMClient returns an LLM client backed by an Ollama-compatible
// /api/generate endpoint. Input is head-truncated to maxChars before sending.
// The returned concrete type structurally satisfies extract.LLMClient (no import
// of extract is required by callers for the interface).
func NewHTTPLLMClient(endpoint, model string, maxInputChars int, timeout time.Duration) *httpLLMClient {
	return &httpLLMClient{
		endpoint: endpoint,
		model:    model,
		maxChars: maxInputChars,
		client:   &http.Client{Timeout: timeout},
	}
}

// extractSchema is the JSON-Schema handed to Ollama's `format` field so the model
// is grammar-constrained to emit exactly the ExtractedEvent shape. EVERY property
// is listed in `required`: Ollama's grammar lets the model omit non-required keys,
// and in practice it does — omitting e.g. aircraft_registration/aircraft_type even
// when they are plainly in the text, which then fails the promotion gate. Requiring
// all keys forces the model to fill each field from the document (empty/null when
// genuinely absent). Verified against a real Honduras AHAC report.
var extractSchema = json.RawMessage(`{
  "type":"object",
  "properties":{
    "is_aviation_accident":{"type":"boolean"},
    "date":{"type":"string"},
    "date_precision":{"type":"string"},
    "location":{"type":"string"},
    "country":{"type":"string"},
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
  "required":[
    "is_aviation_accident","date","date_precision","location","country","latitude","longitude",
    "aircraft_registration","aircraft_type","manufacturer","operator_name","flight_number",
    "fatalities","injuries","event_type","investigation_status","report_type","title",
    "language","published_date"
  ]
}`)

func (h *httpLLMClient) Extract(ctx context.Context, text string) (extract.ExtractedEvent, error) {
	if h.maxChars > 0 && utf8.RuneCountInString(text) > h.maxChars {
		text = string([]rune(text)[:h.maxChars])
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
		return extract.ExtractedEvent{}, fmt.Errorf("wayback: marshal llm request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, h.endpoint, bytes.NewReader(b))
	if err != nil {
		return extract.ExtractedEvent{}, fmt.Errorf("wayback: build llm request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := h.client.Do(req)
	if err != nil {
		return extract.ExtractedEvent{}, fmt.Errorf("wayback: llm post: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return extract.ExtractedEvent{}, fmt.Errorf("wayback: llm status %d: %s", resp.StatusCode, snippet)
	}
	var wrap struct {
		Response string `json:"response"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&wrap); err != nil {
		return extract.ExtractedEvent{}, fmt.Errorf("wayback: decode llm wrapper: %w", err)
	}
	var ev extract.ExtractedEvent
	if err := json.Unmarshal([]byte(wrap.Response), &ev); err != nil {
		return extract.ExtractedEvent{}, fmt.Errorf("wayback: unmarshal extracted event: %w", err)
	}
	return ev, nil
}

var _ extract.LLMClient = (*httpLLMClient)(nil)
