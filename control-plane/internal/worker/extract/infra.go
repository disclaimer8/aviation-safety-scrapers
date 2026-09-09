package extract

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
)

// InfraAbortError signals that an extract pass stopped early because the
// OCR or LLM endpoint itself is unreachable (dial refused/timeout, network
// unreachable — as opposed to a problem with the specific document). This is
// GO-CP-3: a 3-day LLM-endpoint tunnel outage ("dial tcp 127.0.0.1:11434:
// connect: connection refused") burned the extraction_attempts budget of every fresh
// IAC doc in the queue, permanently retiring documents that had done nothing
// wrong. When the endpoint itself is down, every other document in the batch
// would fail identically, so extractOne does NOT call RecordFailure (the
// document's attempt counter is left untouched) and instead returns this typed
// error, which ProcessExtractPending propagates immediately — aborting the
// rest of the batch rather than burning through it one connection-refused
// error at a time. The CLI (see app.go's runExtract) prints it to stderr and
// exits non-zero so the caller/notification layer can alert on it.
type InfraAbortError struct {
	DocID int64
	Step  string // "ocr" or "llm"
	Cause error
}

func (e *InfraAbortError) Error() string {
	return fmt.Sprintf("extract: aborting pass — %s endpoint unreachable on doc %d: %v", e.Step, e.DocID, e.Cause)
}

func (e *InfraAbortError) Unwrap() error { return e.Cause }

// isInfraError reports whether err is a connection-level failure reaching an
// endpoint — dial refused, DNS failure, network/host unreachable — as opposed
// to a failure that belongs to the specific request (non-200 status, bad
// response body, malformed JSON, or the request taking too long).
//
// http.Client wraps transport-level failures in *url.Error, whose Unwrap()
// exposes the underlying *net.OpError (dial failures) or other net.Error
// (timeouts, DNS errors); errors.As walks that chain regardless of how many
// fmt.Errorf("...: %w", err) layers the caller added on top.
//
// A REQUEST TIMEOUT IS NOT AN INFRA ERROR. This used to return true for any
// net.Error, and an http.Client timeout unwraps to exactly that. The result
// inverted GO-CP-3: one slow PDF timed out, extractOne skipped RecordFailure,
// ProcessExtractPending aborted the whole pass, and the same high-priority
// document was first in the queue on the next run — for ever. Every document
// behind it starved. Whether the endpoint is down is answered by "can we
// reach it at all", not by "did this document take too long".
func isInfraError(err error) bool {
	if err == nil {
		return false
	}
	// A dial failure is about the endpoint: refused, unreachable, or a dial
	// that timed out all mean no connection could be opened at all. That is
	// the GO-CP-3 case, and the one worth aborting the pass for. Checked
	// before the timeout rule below, because a dial timeout is a timeout.
	var opErr *net.OpError
	if errors.As(err, &opErr) && opErr.Op == "dial" {
		return true
	}
	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return true
	}
	// Anything else presenting as a timeout is this document being slow: a
	// client Timeout, a context deadline, a read that stalled mid-response.
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, os.ErrDeadlineExceeded) {
		return false
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return false
	}
	// Remaining network errors (a connection reset by the endpoint, say) keep
	// the previous classification.
	return errors.As(err, &netErr)
}
