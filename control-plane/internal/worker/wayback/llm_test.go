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

// TestExtractSchemaRequiresAllProperties guards against the model omitting fields.
// Ollama's grammar lets it drop any property not in `required`, and qwen3.6-rw does
// — omitting aircraft_registration/aircraft_type even when present, which fails the
// promotion gate (caught in a live smoke against a real Honduras report). Every
// property must be required so the model fills each field from the document.
func TestExtractSchemaRequiresAllProperties(t *testing.T) {
	var s struct {
		Properties map[string]json.RawMessage `json:"properties"`
		Required   []string                   `json:"required"`
	}
	if err := json.Unmarshal(extractSchema, &s); err != nil {
		t.Fatalf("extractSchema is not valid JSON: %v", err)
	}
	required := make(map[string]bool, len(s.Required))
	for _, r := range s.Required {
		required[r] = true
	}
	for prop := range s.Properties {
		if !required[prop] {
			t.Errorf("property %q is not in `required` — Ollama may omit it", prop)
		}
	}
	if len(s.Required) != len(s.Properties) {
		t.Errorf("required lists %d fields but schema has %d properties", len(s.Required), len(s.Properties))
	}
}

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
