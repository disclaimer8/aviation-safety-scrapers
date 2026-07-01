package extract

import (
	"errors"
	"fmt"
	"net"
)

// InfraAbortError signals that an extract pass stopped early because the
// OCR or LLM endpoint itself is unreachable (dial refused/timeout, network
// unreachable — as opposed to a problem with the specific document). This is
// GO-CP-3: a 3-day home-PC tunnel outage ("dial tcp 127.0.0.1:11434: connect:
// connection refused") burned the extraction_attempts budget of every fresh
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
// endpoint — dial refused, dial/connect timeout, DNS failure, network/host
// unreachable — as opposed to an application-level failure (non-200 status,
// bad response body, malformed JSON) which is a property of the specific
// request, not the endpoint's availability.
//
// http.Client wraps transport-level failures in *url.Error, whose Unwrap()
// exposes the underlying *net.OpError (dial failures) or other net.Error
// (timeouts, DNS errors); errors.As walks that chain regardless of how many
// fmt.Errorf("...: %w", err) layers the caller added on top.
func isInfraError(err error) bool {
	if err == nil {
		return false
	}
	var opErr *net.OpError
	if errors.As(err, &opErr) {
		return true
	}
	var netErr net.Error
	return errors.As(err, &netErr)
}
