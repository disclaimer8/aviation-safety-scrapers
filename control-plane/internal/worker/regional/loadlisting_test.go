package regional

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLoadListingRenderPath(t *testing.T) {
	ctx := context.Background()

	// A stub render service: echoes the requested URL inside rendered HTML.
	var gotURL string
	render := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/render" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		var body struct {
			URL  string  `json:"url"`
			Wait float64 `json:"wait"`
		}
		json.NewDecoder(r.Body).Decode(&body)
		gotURL = body.URL
		io.WriteString(w, "<html>rendered "+body.URL+"</html>")
	}))
	defer render.Close()

	// A plain origin that should NOT be hit when the render endpoint is set.
	plainHit := false
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		plainHit = true
		io.WriteString(w, "<html>plain</html>")
	}))
	defer origin.Close()

	// renderEndpoint set → fetched through the render service, plain origin untouched.
	out, err := loadListing(ctx, 30*time.Second, "", render.URL+"/render", origin.URL)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != "<html>rendered "+origin.URL+"</html>" {
		t.Fatalf("render path not used: %q", out)
	}
	if gotURL != origin.URL {
		t.Errorf("render service got url %q, want %q", gotURL, origin.URL)
	}
	if plainHit {
		t.Error("plain origin must not be fetched when render endpoint is set")
	}

	// sourceFile wins over the render endpoint.
	f := filepath.Join(t.TempDir(), "x.html")
	os.WriteFile(f, []byte("<html>from file</html>"), 0o644)
	out, err = loadListing(ctx, 30*time.Second, f, render.URL+"/render", origin.URL)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != "<html>from file</html>" {
		t.Fatalf("source-file should win over render, got %q", out)
	}

	// No render endpoint, no source file → plain GET.
	out, err = loadListing(ctx, 30*time.Second, "", "", origin.URL)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != "<html>plain</html>" || !plainHit {
		t.Fatalf("plain fallback not used: %q", out)
	}
}
