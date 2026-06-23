package extract

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestDownloadReportURL(t *testing.T) {
	pdfBytes := []byte("%PDF-1.4 fake pdf content for testing")
	sum := sha256.Sum256(pdfBytes)
	wantDigest := hex.EncodeToString(sum[:])

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/report.pdf":
			w.Header().Set("Content-Type", "application/pdf")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(pdfBytes)
		case "/notfound.pdf":
			w.WriteHeader(http.StatusNotFound)
		default:
			w.WriteHeader(http.StatusInternalServerError)
		}
	}))
	defer srv.Close()

	tests := []struct {
		name     string
		rawURL   string
		wantErr  bool
		wantPath bool
		wantDig  string
	}{
		{
			name:     "200 PDF written with correct digest",
			rawURL:   srv.URL + "/report.pdf",
			wantErr:  false,
			wantPath: true,
			wantDig:  wantDigest,
		},
		{
			name:    "javascript scheme rejected without fetch",
			rawURL:  "javascript:alert(1)",
			wantErr: true,
		},
		{
			name:    "ftp scheme rejected without fetch",
			rawURL:  "ftp://example.com/file.pdf",
			wantErr: true,
		},
		{
			name:    "404 response returns error",
			rawURL:  srv.URL + "/notfound.pdf",
			wantErr: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			storeDir := t.TempDir()
			client := &http.Client{}
			localPath, digest, err := DownloadReportURL(context.Background(), client, tc.rawURL, storeDir, "ZZ")

			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got localPath=%q digest=%q", localPath, digest)
				}
				return
			}

			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			wantFilePath := filepath.Join(storeDir, "ZZ", wantDigest+".pdf")
			if localPath != wantFilePath {
				t.Errorf("localPath = %q, want %q", localPath, wantFilePath)
			}
			if digest != tc.wantDig {
				t.Errorf("digest = %q, want %q", digest, tc.wantDig)
			}

			got, err := os.ReadFile(localPath)
			if err != nil {
				t.Fatalf("read written file: %v", err)
			}
			if string(got) != string(pdfBytes) {
				t.Errorf("file content = %q, want %q", got, pdfBytes)
			}
		})
	}
}
