package fetch_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/fetch"
)

// TestGetRetriesServerErrorsAndReturnsMetadata verifies that:
//   - the User-Agent header is sent on each attempt,
//   - 5xx responses are retried (up to Retries+1 total attempts),
//   - on success the body and metadata (ETag, ContentType, FetchedAt) are returned.
func TestGetRetriesServerErrorsAndReturnsMetadata(t *testing.T) {
	var attempts atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("User-Agent") != "coverage-test/1" {
			t.Errorf("unexpected user agent %q", r.Header.Get("User-Agent"))
		}
		if attempts.Add(1) < 3 {
			http.Error(w, "temporary", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "text/html")
		w.Header().Set("ETag", `"abc"`)
		io.WriteString(w, "<html>ok</html>")
	}))
	defer srv.Close()

	got, err := fetch.Get(context.Background(), srv.Client(), fetch.Request{
		URL:       srv.URL,
		UserAgent: "coverage-test/1",
		Timeout:   time.Second,
		MaxBytes:  1024,
		Retries:   2,
	})
	if err != nil {
		t.Fatal(err)
	}
	if string(got.Body) != "<html>ok</html>" || attempts.Load() != 3 {
		t.Fatalf("body=%q attempts=%d", got.Body, attempts.Load())
	}
	if got.ContentType != "text/html" {
		t.Errorf("ContentType=%q want text/html", got.ContentType)
	}
	if got.ETag != `"abc"` {
		t.Errorf("ETag=%q want \"abc\"", got.ETag)
	}
	if got.StatusCode != http.StatusOK {
		t.Errorf("StatusCode=%d want 200", got.StatusCode)
	}
	if got.FetchedAt.IsZero() {
		t.Error("FetchedAt should not be zero")
	}
}

// TestGetRejectsOversizedBody verifies ErrBodyTooLarge is returned when the
// server sends more bytes than MaxBytes.
func TestGetRejectsOversizedBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		io.WriteString(w, strings.Repeat("x", 2048))
	}))
	defer srv.Close()

	_, err := fetch.Get(context.Background(), srv.Client(), fetch.Request{
		URL:      srv.URL,
		Timeout:  time.Second,
		MaxBytes: 128,
		Retries:  0,
	})
	if !errors.Is(err, fetch.ErrBodyTooLarge) {
		t.Fatalf("want ErrBodyTooLarge, got %v", err)
	}
}

// TestGetRejectsUnsupportedScheme verifies ErrUnsupportedScheme for non-http(s) URLs.
func TestGetRejectsUnsupportedScheme(t *testing.T) {
	_, err := fetch.Get(context.Background(), http.DefaultClient, fetch.Request{
		URL:      "ftp://example.com/file.txt",
		Timeout:  time.Second,
		MaxBytes: 1024,
	})
	if !errors.Is(err, fetch.ErrUnsupportedScheme) {
		t.Fatalf("want ErrUnsupportedScheme, got %v", err)
	}
}

// TestGetNon2xxAfterRetries verifies that a non-2xx final response is an error.
func TestGetNon2xxAfterRetries(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "not found", http.StatusNotFound)
	}))
	defer srv.Close()

	_, err := fetch.Get(context.Background(), srv.Client(), fetch.Request{
		URL:      srv.URL,
		Timeout:  time.Second,
		MaxBytes: 1024,
		Retries:  0,
	})
	if err == nil {
		t.Fatal("expected error for 404 response, got nil")
	}
}

// TestGet4xxNotRetried verifies that 4xx (other than 429) responses are NOT retried.
func TestGet4xxNotRetried(t *testing.T) {
	var attempts atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts.Add(1)
		http.Error(w, "forbidden", http.StatusForbidden)
	}))
	defer srv.Close()

	_, err := fetch.Get(context.Background(), srv.Client(), fetch.Request{
		URL:      srv.URL,
		Timeout:  time.Second,
		MaxBytes: 1024,
		Retries:  3,
	})
	if err == nil {
		t.Fatal("expected error for 403, got nil")
	}
	if attempts.Load() != 1 {
		t.Fatalf("expected exactly 1 attempt for 4xx, got %d", attempts.Load())
	}
}

// TestGet429IsRetried verifies that 429 responses ARE retried.
func TestGet429IsRetried(t *testing.T) {
	var attempts atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := attempts.Add(1)
		if n < 2 {
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		w.Header().Set("Content-Type", "text/plain")
		io.WriteString(w, "ok")
	}))
	defer srv.Close()

	got, err := fetch.Get(context.Background(), srv.Client(), fetch.Request{
		URL:      srv.URL,
		Timeout:  time.Second,
		MaxBytes: 1024,
		Retries:  2,
	})
	if err != nil {
		t.Fatalf("expected success after 429 retry, got %v", err)
	}
	if attempts.Load() != 2 {
		t.Fatalf("expected 2 attempts, got %d", attempts.Load())
	}
	if string(got.Body) != "ok" {
		t.Errorf("unexpected body %q", got.Body)
	}
}

// TestGetRedirectCap verifies that more than 5 redirects returns an error.
func TestGetRedirectCap(t *testing.T) {
	// Build a chain of 7 redirects — well above the cap of 5.
	redirectCount := 0
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		redirectCount++
		if redirectCount <= 7 {
			http.Redirect(w, r, srv.URL+"/next", http.StatusFound)
			return
		}
		io.WriteString(w, "final")
	}))
	defer srv.Close()

	// Use a custom client that does NOT follow redirects so we can rely solely
	// on the fetcher's own redirect cap.
	client := srv.Client()
	client.CheckRedirect = nil // use default (will follow up to 10)

	_, err := fetch.Get(context.Background(), srv.Client(), fetch.Request{
		URL:      srv.URL,
		Timeout:  5 * time.Second,
		MaxBytes: 1024,
		Retries:  0,
	})
	if err == nil {
		t.Fatal("expected redirect-cap error, got nil")
	}
}

// TestGetContextTimeout verifies that a cancelled/timed-out context is respected.
func TestGetContextTimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Simulate a slow server.
		time.Sleep(200 * time.Millisecond)
		io.WriteString(w, "too slow")
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	_, err := fetch.Get(ctx, srv.Client(), fetch.Request{
		URL:      srv.URL,
		Timeout:  5 * time.Second,
		MaxBytes: 1024,
		Retries:  0,
	})
	if err == nil {
		t.Fatal("expected timeout error, got nil")
	}
}

// TestGetFinalURLAfterRedirect verifies FinalURL reflects the redirected location.
func TestGetFinalURLAfterRedirect(t *testing.T) {
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			http.Redirect(w, r, srv.URL+"/final", http.StatusFound)
			return
		}
		w.Header().Set("Content-Type", "text/plain")
		io.WriteString(w, "final page")
	}))
	defer srv.Close()

	got, err := fetch.Get(context.Background(), srv.Client(), fetch.Request{
		URL:      srv.URL + "/",
		Timeout:  time.Second,
		MaxBytes: 1024,
		Retries:  0,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.FinalURL != srv.URL+"/final" {
		t.Errorf("FinalURL=%q want %s/final", got.FinalURL, srv.URL)
	}
}

// TestGetLastModified verifies that Last-Modified header is captured.
func TestGetLastModified(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Last-Modified", "Mon, 02 Jan 2006 15:04:05 GMT")
		w.Header().Set("Content-Type", "text/html")
		io.WriteString(w, "<html/>")
	}))
	defer srv.Close()

	got, err := fetch.Get(context.Background(), srv.Client(), fetch.Request{
		URL:      srv.URL,
		Timeout:  time.Second,
		MaxBytes: 1024,
		Retries:  0,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.LastModified != "Mon, 02 Jan 2006 15:04:05 GMT" {
		t.Errorf("LastModified=%q", got.LastModified)
	}
}

// TestGetLargeBodySuccess verifies that a body well over the transport read
// buffer (~4 KiB) is returned in full when MaxBytes allows it. This test
// reproduces the "cancel-before-read" bug where the per-attempt context was
// cancelled immediately after client.Do returned (headers only), causing
// io.ReadAll to fail with context canceled on large bodies.
func TestGetLargeBodySuccess(t *testing.T) {
	const bodySize = 1 << 20 // 1 MiB — well above any transport read buffer
	payload := make([]byte, bodySize)
	for i := range payload {
		payload[i] = 'a'
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Write(payload)
	}))
	defer srv.Close()

	got, err := fetch.Get(context.Background(), srv.Client(), fetch.Request{
		URL:      srv.URL,
		Timeout:  5 * time.Second,
		MaxBytes: bodySize, // exactly the size — must succeed
		Retries:  0,
	})
	if err != nil {
		t.Fatalf("expected no error for 1 MiB body, got: %v", err)
	}
	if len(got.Body) != bodySize {
		t.Fatalf("expected body length %d, got %d", bodySize, len(got.Body))
	}
}

// TestGetBodyBoundaryExact verifies the exact MaxBytes boundary behaviour:
//   - exactly MaxBytes bytes must succeed,
//   - exactly MaxBytes+1 bytes must return ErrBodyTooLarge.
func TestGetBodyBoundaryExact(t *testing.T) {
	const limit = 512

	// Exactly at the boundary — must succeed.
	srvOK := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write(make([]byte, limit))
	}))
	defer srvOK.Close()

	got, err := fetch.Get(context.Background(), srvOK.Client(), fetch.Request{
		URL:      srvOK.URL,
		Timeout:  time.Second,
		MaxBytes: limit,
		Retries:  0,
	})
	if err != nil {
		t.Fatalf("exactly MaxBytes should succeed, got: %v", err)
	}
	if int64(len(got.Body)) != limit {
		t.Fatalf("expected %d bytes, got %d", limit, len(got.Body))
	}

	// One byte over — must return ErrBodyTooLarge.
	srvOver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write(make([]byte, limit+1))
	}))
	defer srvOver.Close()

	_, err = fetch.Get(context.Background(), srvOver.Client(), fetch.Request{
		URL:      srvOver.URL,
		Timeout:  time.Second,
		MaxBytes: limit,
		Retries:  0,
	})
	if !errors.Is(err, fetch.ErrBodyTooLarge) {
		t.Fatalf("MaxBytes+1 should return ErrBodyTooLarge, got: %v", err)
	}
}

// TestGetInterruptibleBackoff verifies that cancelling the parent context
// during a retry backoff causes Get to return promptly with a context error
// rather than sleeping for the full backoff duration.
func TestGetInterruptibleBackoff(t *testing.T) {
	// Always return 503 so every attempt is retriable.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())

	// Cancel the context shortly after the first attempt completes. The backoff
	// between attempt 0 and 1 is 250 ms; we cancel at 50 ms — if the sleep is
	// not interruptible the call would block for ~250 ms instead.
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()

	start := time.Now()
	_, err := fetch.Get(ctx, srv.Client(), fetch.Request{
		URL:      srv.URL,
		Timeout:  time.Second,
		MaxBytes: 1024,
		Retries:  3, // plenty of retries so we don't exhaust them before cancel
	})
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("expected an error after context cancellation, got nil")
	}
	// Should return well before the full 250 ms backoff.
	if elapsed > 200*time.Millisecond {
		t.Fatalf("Get took %v — backoff is not interruptible (expected <200ms)", elapsed)
	}
}
