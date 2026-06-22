package config

import (
	"testing"
	"time"
)

func TestDefaultHTTP(t *testing.T) {
	got := DefaultHTTP()

	if got.UserAgent != "aviation-coverage-control-plane/1.0 (+https://github.com/denyskolomiiets/aviation-safety-scrapers)" {
		t.Fatalf("UserAgent=%q", got.UserAgent)
	}
	if got.Timeout != 30*time.Second {
		t.Fatalf("Timeout=%s, want 30s", got.Timeout)
	}
	if got.MaxBytes != 8<<20 {
		t.Fatalf("MaxBytes=%d, want %d", got.MaxBytes, 8<<20)
	}
	if got.Retries != 2 {
		t.Fatalf("Retries=%d, want 2", got.Retries)
	}
	if DefaultAIAURL != "https://www.icao.int/safety/AIG/AIA" {
		t.Fatalf("DefaultAIAURL=%q", DefaultAIAURL)
	}
	if DefaultRAIOURL != "https://www.icao.int/safety/regional-safety-cooperation/List-of-RAIOs-and-ICMs" {
		t.Fatalf("DefaultRAIOURL=%q", DefaultRAIOURL)
	}
}
