package extract

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/atomicfile"
)

// DownloadReportURL fetches rawURL (http/https only), writes the response body
// to <storeDir>/<iso2>/<sha256hex>.pdf via an atomic write, and returns the
// local file path and its SHA-256 hex digest. Non-http(s) schemes, non-200
// responses, and I/O failures all return a descriptive error that callers can
// map to download_status='failed'.
func DownloadReportURL(ctx context.Context, client *http.Client, rawURL, storeDir, iso2 string) (localPath, digest string, err error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return "", "", fmt.Errorf("extract: download: parse URL %q: %w", rawURL, err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", "", fmt.Errorf("extract: download: scheme %q not allowed (must be http or https): %s", u.Scheme, rawURL)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return "", "", fmt.Errorf("extract: download: build request %s: %w", rawURL, err)
	}

	resp, err := client.Do(req)
	if err != nil {
		return "", "", fmt.Errorf("extract: download: GET %s: %w", rawURL, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", "", fmt.Errorf("extract: download: GET %s: status %d", rawURL, resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", "", fmt.Errorf("extract: download: read body %s: %w", rawURL, err)
	}

	sum := sha256.Sum256(body)
	hexDigest := hex.EncodeToString(sum[:])

	dir := filepath.Join(storeDir, iso2)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", "", fmt.Errorf("extract: download: mkdir %s: %w", dir, err)
	}

	destPath := filepath.Join(dir, hexDigest+".pdf")
	if err := atomicfile.Write(destPath, body); err != nil {
		return "", "", fmt.Errorf("extract: download: write %s: %w", destPath, err)
	}

	return destPath, hexDigest, nil
}
