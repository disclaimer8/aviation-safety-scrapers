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

// errRetriable wraps an error to signal that the attempt should be retried.
type errRetriable struct{ cause error }

func (e errRetriable) Error() string { return e.cause.Error() }
func (e errRetriable) Unwrap() error { return e.cause }

// backoffDuration returns the backoff duration for a given attempt index,
// clamping to the last element if the index exceeds the table length.
func backoffDuration(attempt int, backoffs []time.Duration) time.Duration {
	if attempt < len(backoffs) {
		return backoffs[attempt]
	}
	return backoffs[len(backoffs)-1]
}

// sleepInterruptible sleeps for d or until ctx is done, whichever comes first.
// It returns ctx.Err() if the context was cancelled, nil otherwise.
func sleepInterruptible(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// attemptResult holds the outcome of a single HTTP attempt including the
// fully-read body. The per-attempt context is kept alive until the body is
// drained, so callers never see a context-canceled read error on large bodies.
type attemptResult struct {
	resp      *http.Response // header metadata only (body already closed)
	fetchedAt time.Time
	body      []byte
}

// doAttempt executes one HTTP request + full body read under attemptCtx. The
// context cancel is deferred so it fires only after the body is consumed.
// It returns errRetriable-wrapped errors for 5xx / 429 / network failures so
// the caller can decide whether to retry.
func doAttempt(attemptCtx context.Context, cancel context.CancelFunc, c *http.Client, req Request) (attemptResult, error) {
	defer cancel() // keep the context alive until this function returns

	resp, fetchedAt, err := doRequest(attemptCtx, c, req)
	if err != nil {
		return attemptResult{fetchedAt: fetchedAt}, errRetriable{cause: err}
	}
	defer resp.Body.Close()

	// Non-retriable 4xx (not 429): fail immediately.
	if resp.StatusCode >= 400 && resp.StatusCode < 500 && resp.StatusCode != http.StatusTooManyRequests {
		return attemptResult{resp: resp, fetchedAt: fetchedAt},
			fmt.Errorf("fetch: non-retriable status %d", resp.StatusCode)
	}

	// Retriable: 5xx or 429.
	if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500 {
		return attemptResult{resp: resp, fetchedAt: fetchedAt},
			errRetriable{cause: fmt.Errorf("fetch: retriable status %d", resp.StatusCode)}
	}

	// Non-2xx that isn't 4xx or 5xx (e.g. 3xx that somehow slipped through).
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return attemptResult{resp: resp, fetchedAt: fetchedAt},
			fmt.Errorf("fetch: unexpected status %d", resp.StatusCode)
	}

	// 2xx: read body with size cap UNDER the live attemptCtx so that large
	// bodies are not truncated by an early context cancellation.
	lr := io.LimitReader(resp.Body, req.MaxBytes+1)
	body, readErr := io.ReadAll(lr)
	if readErr != nil {
		return attemptResult{resp: resp, fetchedAt: fetchedAt},
			fmt.Errorf("fetch: reading body: %w", readErr)
	}
	if int64(len(body)) > req.MaxBytes {
		return attemptResult{resp: resp, fetchedAt: fetchedAt}, ErrBodyTooLarge
	}

	return attemptResult{resp: resp, fetchedAt: fetchedAt, body: body}, nil
}

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
		// and the per-request timeout. cancel is passed into doAttempt which
		// defers it, so it fires only after the body is fully read.
		var cancel context.CancelFunc
		var attemptCtx context.Context
		if req.Timeout > 0 {
			attemptCtx, cancel = context.WithTimeout(ctx, req.Timeout)
		} else {
			attemptCtx, cancel = context.WithCancel(ctx)
		}

		result, attemptErr := doAttempt(attemptCtx, cancel, &c, req)

		if attemptErr != nil {
			// Check if the parent context is done — no point retrying.
			if ctx.Err() != nil {
				return Response{}, ctx.Err()
			}

			// Only retry on retriable errors.
			var re errRetriable
			if !errors.As(attemptErr, &re) {
				return Response{}, attemptErr
			}

			lastErr = attemptErr
			if attempt < maxAttempts-1 {
				if sleepErr := sleepInterruptible(ctx, backoffDuration(attempt, backoffs)); sleepErr != nil {
					return Response{}, sleepErr
				}
			}
			continue
		}

		finalURL := result.resp.Request.URL.String()

		return Response{
			FinalURL:     finalURL,
			StatusCode:   result.resp.StatusCode,
			ContentType:  result.resp.Header.Get("Content-Type"),
			ETag:         result.resp.Header.Get("ETag"),
			LastModified: result.resp.Header.Get("Last-Modified"),
			Body:         result.body,
			FetchedAt:    result.fetchedAt,
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
