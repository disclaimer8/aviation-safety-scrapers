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
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return ExtractedEvent{}, fmt.Errorf("wayback: llm status %d: %s", resp.StatusCode, snippet)
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
