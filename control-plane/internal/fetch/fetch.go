// Package fetch provides a bounded, resilient HTTP fetcher with retry logic,
// redirect capping, body-size limits, and response metadata capture.
package fetch

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// Sentinel errors returned by Get.
var (
	ErrBodyTooLarge      = errors.New("response body exceeds limit")
	ErrUnsupportedScheme = errors.New("unsupported URL scheme")
)

// Request holds the parameters for a single HTTP fetch operation.
type Request struct {
	URL       string
	UserAgent string
	Timeout   time.Duration
	MaxBytes  int64
	Retries   int
}

// Response contains the result of a successful HTTP fetch.
type Response struct {
	FinalURL     string
	StatusCode   int
	ContentType  string
	ETag         string
	LastModified string
	Body         []byte
	FetchedAt    time.Time
}

const maxRedirects = 5

// Get performs an HTTP GET for req using client. It enforces scheme validation,
// a redirect cap of 5, per-attempt context timeouts, retries on network errors /
// 429 / 5xx with exponential backoff (250ms → 500ms), a body-size cap, and
// returns populated Response metadata on success. Non-2xx final responses are
// returned as errors.
func Get(ctx context.Context, client *http.Client, req Request) (Response, error) {
	// Validate scheme before making any network calls.
	u, err := url.Parse(req.URL)
	if err != nil {
		return Response{}, fmt.Errorf("fetch: invalid URL: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return Response{}, ErrUnsupportedScheme
	}

	// Clone the client so we can set our own redirect policy without mutating
	// the caller's client.
	c := *client
	c.CheckRedirect = func(r *http.Request, via []*http.Request) error {
		if len(via) >= maxRedirects {
			return fmt.Errorf("fetch: stopped after %d redirects", maxRedirects)
		}
		return nil
	}

	// Backoff durations: attempt 0→1 waits 250ms, attempt 1→2 waits 500ms, etc.
	backoffs := []time.Duration{
		250 * time.Millisecond,
		500 * time.Millisecond,
	}

	maxAttempts := req.Retries + 1
	var lastErr error

	for attempt := 0; attempt < maxAttempts; attempt++ {
		// Derive a per-attempt context that respects both the caller's deadline
		// and the per-request timeout.
		attemptCtx := ctx
		var cancel context.CancelFunc
		if req.Timeout > 0 {
			attemptCtx, cancel = context.WithTimeout(ctx, req.Timeout)
		} else {
			attemptCtx, cancel = context.WithCancel(ctx)
		}

		resp, fetchedAt, err := doRequest(attemptCtx, &c, req)
		cancel()

		if err != nil {
			// Check if the parent context is done — no point retrying.
			if ctx.Err() != nil {
				return Response{}, ctx.Err()
			}
			lastErr = err
			if attempt < maxAttempts-1 {
				if idx := attempt; idx < len(backoffs) {
					time.Sleep(backoffs[idx])
				} else {
					time.Sleep(backoffs[len(backoffs)-1])
				}
			}
			continue
		}

		// Non-retriable 4xx (not 429): fail immediately.
		if resp.StatusCode >= 400 && resp.StatusCode < 500 && resp.StatusCode != http.StatusTooManyRequests {
			resp.Body.Close()
			return Response{}, fmt.Errorf("fetch: non-retriable status %d", resp.StatusCode)
		}

		// Retriable: 5xx or 429.
		if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500 {
			resp.Body.Close()
			lastErr = fmt.Errorf("fetch: retriable status %d", resp.StatusCode)
			if attempt < maxAttempts-1 {
				if idx := attempt; idx < len(backoffs) {
					time.Sleep(backoffs[idx])
				} else {
					time.Sleep(backoffs[len(backoffs)-1])
				}
			}
			continue
		}

		// Non-2xx that isn't 4xx or 5xx (e.g. 3xx that somehow slipped through).
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			resp.Body.Close()
			return Response{}, fmt.Errorf("fetch: unexpected status %d", resp.StatusCode)
		}

		// 2xx: read body with size cap.
		defer resp.Body.Close()
		lr := io.LimitReader(resp.Body, req.MaxBytes+1)
		body, readErr := io.ReadAll(lr)
		if readErr != nil {
			return Response{}, fmt.Errorf("fetch: reading body: %w", readErr)
		}
		if int64(len(body)) > req.MaxBytes {
			return Response{}, ErrBodyTooLarge
		}

		finalURL := resp.Request.URL.String()

		return Response{
			FinalURL:     finalURL,
			StatusCode:   resp.StatusCode,
			ContentType:  resp.Header.Get("Content-Type"),
			ETag:         resp.Header.Get("ETag"),
			LastModified: resp.Header.Get("Last-Modified"),
			Body:         body,
			FetchedAt:    fetchedAt,
		}, nil
	}

	return Response{}, lastErr
}

// doRequest executes a single HTTP GET and returns the raw response together
// with the timestamp at which the request was initiated.
func doRequest(ctx context.Context, client *http.Client, req Request) (*http.Response, time.Time, error) {
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodGet, req.URL, nil)
	if err != nil {
		return nil, time.Time{}, fmt.Errorf("fetch: build request: %w", err)
	}
	if req.UserAgent != "" {
		httpReq.Header.Set("User-Agent", req.UserAgent)
	}
	fetchedAt := time.Now().UTC()
	resp, err := client.Do(httpReq)
	if err != nil {
		return nil, fetchedAt, err
	}
	return resp, fetchedAt, nil
}
