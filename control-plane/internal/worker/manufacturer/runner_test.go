package manufacturer

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"strings"
	"testing"
)

// fakeDiscoverer is a test double for the Discoverer interface.
type fakeDiscoverer struct {
	records     []ManufacturerRecord
	discoverErr error
	probeRecord ManufacturerRecord
	probeFound  bool
	probeErr    error
	highestSeen int // records the highestKnown passed to ProbeNextIssue
}

func (f *fakeDiscoverer) Discover(_ context.Context) ([]ManufacturerRecord, error) {
	return f.records, f.discoverErr
}

func (f *fakeDiscoverer) ProbeNextIssue(_ context.Context, highestKnown int) (ManufacturerRecord, bool, error) {
	f.highestSeen = highestKnown
	return f.probeRecord, f.probeFound, f.probeErr
}

// TestProcessManufacturer_Happy covers: Discover returns 3 records (2 numeric,
// 1 special edition), ProbeNextIssue returns 1 more; total Found=4, Staged=4.
// Second run: same discoverer → Staged=0 (dedup).
func TestProcessManufacturer_Happy(t *testing.T) {
	ctx, db := seededManufacturerDB(t)

	d := &fakeDiscoverer{
		records: []ManufacturerRecord{
			{IssueRef: "40", Title: "Safety First #40", OriginalURL: "https://example.com/40.pdf"},
			{IssueRef: "41", Title: "Safety First #41", OriginalURL: "https://example.com/41.pdf"},
			// special edition: non-numeric IssueRef must be skipped when computing highest
			{IssueRef: "special_ed", Title: "Special Edition", OriginalURL: "https://example.com/se.pdf"},
		},
		probeRecord: ManufacturerRecord{
			IssueRef: "42", Title: "Safety First #42", OriginalURL: "https://example.com/42.pdf",
		},
		probeFound: true,
	}

	res, err := ProcessManufacturer(ctx, db, d)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Found != 4 {
		t.Errorf("Found = %d, want 4", res.Found)
	}
	if res.Staged != 4 {
		t.Errorf("Staged = %d, want 4", res.Staged)
	}
	if res.Errors != 0 {
		t.Errorf("Errors = %d, want 0", res.Errors)
	}
	// highestKnown passed to probe should be 41 (highest numeric IssueRef)
	if d.highestSeen != 41 {
		t.Errorf("ProbeNextIssue called with highestKnown=%d, want 41", d.highestSeen)
	}

	// Second run — all records already staged → Staged=0.
	res2, err := ProcessManufacturer(ctx, db, d)
	if err != nil {
		t.Fatalf("second run unexpected error: %v", err)
	}
	if res2.Found != 4 {
		t.Errorf("second run Found = %d, want 4", res2.Found)
	}
	if res2.Staged != 0 {
		t.Errorf("second run Staged = %d, want 0", res2.Staged)
	}
}

// TestProcessManufacturer_DiscoverError verifies that a Discover error is
// propagated and no staging occurs.
func TestProcessManufacturer_DiscoverError(t *testing.T) {
	ctx, db := seededManufacturerDB(t)

	boom := errors.New("network timeout")
	d := &fakeDiscoverer{discoverErr: boom}

	_, err := ProcessManufacturer(ctx, db, d)
	if !errors.Is(err, boom) {
		t.Fatalf("expected boom error, got %v", err)
	}
}

// TestProcessManufacturer_ProbeError verifies that a ProbeNextIssue error is
// non-fatal: Errors is incremented, the discovered records are still staged.
func TestProcessManufacturer_ProbeError(t *testing.T) {
	ctx, db := seededManufacturerDB(t)

	d := &fakeDiscoverer{
		records: []ManufacturerRecord{
			{IssueRef: "10", Title: "Safety First #10", OriginalURL: "https://example.com/10.pdf"},
		},
		probeErr: errors.New("probe failed"),
	}

	res, err := ProcessManufacturer(ctx, db, d)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Found != 1 {
		t.Errorf("Found = %d, want 1", res.Found)
	}
	if res.Staged != 1 {
		t.Errorf("Staged = %d, want 1", res.Staged)
	}
	if res.Errors != 1 {
		t.Errorf("Errors = %d, want 1", res.Errors)
	}
}

// TestProcessManufacturer_ProbeNotFound verifies that when probe returns
// found=false, the record is not appended.
func TestProcessManufacturer_ProbeNotFound(t *testing.T) {
	ctx, db := seededManufacturerDB(t)

	d := &fakeDiscoverer{
		records: []ManufacturerRecord{
			{IssueRef: "5", Title: "Safety First #5", OriginalURL: "https://example.com/5.pdf"},
		},
		probeFound: false,
	}

	res, err := ProcessManufacturer(ctx, db, d)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Found != 1 {
		t.Errorf("Found = %d, want 1", res.Found)
	}
	if res.Staged != 1 {
		t.Errorf("Staged = %d, want 1", res.Staged)
	}
	if res.Errors != 0 {
		t.Errorf("Errors = %d, want 0", res.Errors)
	}
}

// captureStderr redirects os.Stderr for the duration of fn and returns
// everything written to it. Used to assert on the GO-CP-4 tripwire token.
func captureStderr(t *testing.T, fn func()) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	orig := os.Stderr
	os.Stderr = w
	defer func() { os.Stderr = orig }()

	fn()

	w.Close()
	var buf bytes.Buffer
	io.Copy(&buf, r)
	return buf.String()
}

// TestProcessManufacturer_ZeroDiscoveredEmitsSilentFailTripwire pins GO-CP-4
// for the manufacturer worker: Discover succeeding with zero records (the
// shape a listing-page redesign breaking the parser takes) must print the
// grep-able SILENT_FAIL_SUSPECT token instead of silently proceeding as if
// nothing were wrong.
func TestProcessManufacturer_ZeroDiscoveredEmitsSilentFailTripwire(t *testing.T) {
	ctx, db := seededManufacturerDB(t)
	d := &fakeDiscoverer{records: nil, probeFound: false}

	var res Result
	out := captureStderr(t, func() {
		var err error
		res, err = ProcessManufacturer(ctx, db, d)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	if !strings.Contains(out, "SILENT_FAIL_SUSPECT") || !strings.Contains(out, "found=0") {
		t.Fatalf("expected SILENT_FAIL_SUSPECT tripwire on stderr, got: %q", out)
	}
	if res.Found != 0 {
		t.Errorf("Found = %d, want 0", res.Found)
	}
}
