package wayback

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// fixtureOCRClient is the offline OCRClient for tests.
type fixtureOCRClient struct {
	Text string
	Err  error
}

func (f *fixtureOCRClient) OCR(ctx context.Context, pdf []byte) (string, error) {
	if f.Err != nil {
		return "", f.Err
	}
	return f.Text, nil
}

var _ OCRClient = (*fixtureOCRClient)(nil)
var _ OCRClient = (*httpOCRClient)(nil)

func TestPersistOCRText(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t) // helper defined in extracthelpers_test.go
	docID, countryID := seedDownloadedDoc(t, db, "US", "deadbeef")
	_ = countryID

	store := t.TempDir()
	path, err := PersistOCRText(ctx, db, store, "US", "deadbeef", docID, "REPORT TEXT")
	if err != nil {
		t.Fatalf("PersistOCRText: %v", err)
	}
	want := filepath.Join(store, "US", "deadbeef.txt")
	if path != want {
		t.Fatalf("path=%q want %q", path, want)
	}
	b, err := os.ReadFile(want)
	if err != nil {
		t.Fatalf("read text: %v", err)
	}
	if string(b) != "REPORT TEXT" {
		t.Fatalf("text=%q", string(b))
	}
	var status, gotPath string
	db.QueryRowContext(ctx, `
		SELECT extraction_status, ocr_text_path FROM staged_wayback_documents WHERE id=?`, docID).
		Scan(&status, &gotPath)
	if status != "ocr_done" || gotPath != want {
		t.Fatalf("status=%q ocr_text_path=%q", status, gotPath)
	}
}
