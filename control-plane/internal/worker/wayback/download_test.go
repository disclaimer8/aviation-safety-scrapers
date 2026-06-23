package wayback

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestDownloadStagedWritesFileAndChecksum(t *testing.T) {
	ctx, db := waybackTestDB(t)
	cid, jid := stageFixtureJob(t, ctx, db)
	archived := "https://web.archive.org/web/2010id_/http://x/a.pdf"
	if _, err := StageSnapshots(ctx, db, jid, cid, []Snapshot{
		{OriginalURL: "http://x/a.pdf", ArchivedURL: archived, Timestamp: "2010", Mimetype: "application/pdf", Digest: "D1", Length: 5},
	}); err != nil {
		t.Fatal(err)
	}
	docs, err := PendingDocs(ctx, db, cid)
	if err != nil || len(docs) != 1 {
		t.Fatalf("PendingDocs = %v, %v", docs, err)
	}

	body := []byte("PDF-A")
	f := &fixtureFetcher{Files: map[string][]byte{archived: body}}
	store := t.TempDir()
	if err := DownloadStaged(ctx, db, f, store, "ZZ", docs[0]); err != nil {
		t.Fatal(err)
	}

	wantPath := filepath.Join(store, "ZZ", "D1.pdf")
	got, err := os.ReadFile(wantPath)
	if err != nil {
		t.Fatalf("read %s: %v", wantPath, err)
	}
	if string(got) != "PDF-A" {
		t.Fatalf("file bytes = %q", got)
	}
	sum := sha256.Sum256(body)
	wantChecksum := hex.EncodeToString(sum[:])

	var status, path, checksum string
	if err := db.QueryRowContext(ctx,
		`SELECT download_status, local_file_path, checksum FROM staged_wayback_documents WHERE id=?`,
		docs[0].ID).Scan(&status, &path, &checksum); err != nil {
		t.Fatal(err)
	}
	if status != "downloaded" {
		t.Errorf("status = %q, want downloaded", status)
	}
	if path != wantPath {
		t.Errorf("local_file_path = %q, want %q", path, wantPath)
	}
	if checksum != wantChecksum {
		t.Errorf("checksum = %q, want %q", checksum, wantChecksum)
	}
}

func TestDownloadStagedMarksFailedOnFetchError(t *testing.T) {
	ctx, db := waybackTestDB(t)
	cid, jid := stageFixtureJob(t, ctx, db)
	archived := "https://web.archive.org/web/2010id_/http://x/a.pdf"
	if _, err := StageSnapshots(ctx, db, jid, cid, []Snapshot{
		{OriginalURL: "http://x/a.pdf", ArchivedURL: archived, Timestamp: "2010", Mimetype: "application/pdf", Digest: "D1", Length: 5},
	}); err != nil {
		t.Fatal(err)
	}
	docs, _ := PendingDocs(ctx, db, cid)
	f := &fixtureFetcher{GetErr: map[string]error{archived: errors.New("boom")}}
	if err := DownloadStaged(ctx, db, f, t.TempDir(), "ZZ", docs[0]); err == nil {
		t.Fatal("expected error from failed fetch")
	}
	var status string
	if err := db.QueryRowContext(ctx,
		`SELECT download_status FROM staged_wayback_documents WHERE id=?`, docs[0].ID).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "failed" {
		t.Fatalf("status = %q, want failed", status)
	}
}
