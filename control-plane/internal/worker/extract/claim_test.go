package extract

import (
	"context"
	"testing"
)

// process-extract had no document claim at all. Crawl jobs have had one since
// 012_atomic_claim; extract selected pending rows and processed them with no
// guard, so two overlapping passes could both FindDuplicateEvent on an empty
// snapshot and both insert — the duplicate events the dedup keys exist to
// prevent. MaxOpenConns(1) serialises writes within a process, not across two.
func TestClaimDocIsExclusive(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")

	first, err := WaybackSource{}.ClaimDoc(ctx, db, docID)
	if err != nil {
		t.Fatalf("ClaimDoc: %v", err)
	}
	if !first {
		t.Fatal("the first pass must get the document")
	}

	second, err := WaybackSource{}.ClaimDoc(ctx, db, docID)
	if err != nil {
		t.Fatalf("ClaimDoc: %v", err)
	}
	if second {
		t.Fatal("a second pass took a document the first pass already holds")
	}
}

func TestReleaseDocMakesTheDocumentClaimableAgain(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")

	if ok, _ := (WaybackSource{}).ClaimDoc(ctx, db, docID); !ok {
		t.Fatal("first claim failed")
	}
	WaybackSource{}.ReleaseDoc(ctx, db, docID)

	ok, err := WaybackSource{}.ClaimDoc(ctx, db, docID)
	if err != nil {
		t.Fatalf("ClaimDoc: %v", err)
	}
	if !ok {
		t.Fatal("a released document must be immediately claimable — a failed " +
			"document should not sit out the staleness window")
	}
}

// A process that dies mid-document must not strand it. Same rule claimJob uses
// for a stale 'running' crawl job.
func TestAStaleClaimIsReclaimable(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")

	if ok, _ := (WaybackSource{}).ClaimDoc(ctx, db, docID); !ok {
		t.Fatal("first claim failed")
	}
	// Age the claim past the window.
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_wayback_documents
		   SET extraction_claimed_at = CAST(unixepoch('subsec') * 1000 AS INTEGER) - ?
		 WHERE id = ?`, claimStaleAfterMS+1000, docID); err != nil {
		t.Fatal(err)
	}

	ok, err := WaybackSource{}.ClaimDoc(ctx, db, docID)
	if err != nil {
		t.Fatalf("ClaimDoc: %v", err)
	}
	if !ok {
		t.Fatal("a stale claim must be reclaimable")
	}
}

// Every adapter must implement the claim, not just wayback.
func TestEveryStagedDocSourceImplementsTheClaim(t *testing.T) {
	var _ StagedDocSource = WaybackSource{}
	var _ StagedDocSource = RegionalSource{}
	var _ StagedDocSource = ForeignSource{}
	var _ StagedDocSource = ManufacturerSource{}
}
