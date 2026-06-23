// Package app implements the aviation-coverage CLI command dispatcher.
// Run is the single entry point: it parses args, dispatches to the appropriate
// sub-command handler, and returns an exit code without calling os.Exit.
package app

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/export"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/fetch"
	aia "github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/importer/aia"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/importer/common"
	raio "github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/importer/raio"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/planner"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/seed"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/validation"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/worker/extract"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/worker/foreignsearch"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/worker/regional"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/worker/wayback"
)

// Exit codes.
const (
	exitOK      = 0
	exitFailure = 1
	exitUsage   = 2
)

// Run is the main entry point for the aviation-coverage CLI. It dispatches
// to sub-command handlers and returns an exit code. It never calls os.Exit.
func Run(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: aviation-coverage <command> [flags]")
		fmt.Fprintln(stderr, "commands: migrate, seed, import-aia, import-raio, validate, export, plan, process-wayback, process-wayback-extract, process-regional, process-foreign-search")
		return exitUsage
	}

	cmd := args[0]
	rest := args[1:]

	switch cmd {
	case "migrate":
		return runMigrate(ctx, rest, stderr)
	case "seed":
		return runSeed(ctx, rest, stderr)
	case "import-aia":
		return runImport(ctx, rest, stdout, stderr, "aia")
	case "import-raio":
		return runImport(ctx, rest, stdout, stderr, "raio")
	case "validate":
		return runValidate(ctx, rest, stdout, stderr)
	case "export":
		return runExport(ctx, rest, stderr)
	case "plan":
		return runPlan(ctx, rest, stdout, stderr)
	case "process-wayback":
		return runProcessWayback(ctx, rest, stderr)
	case "process-wayback-extract":
		return runProcessWaybackExtract(ctx, rest, stderr)
	case "process-regional":
		return runProcessRegional(ctx, rest, stderr)
	case "process-foreign-search":
		return runProcessForeign(ctx, rest, stderr)
	default:
		fmt.Fprintf(stderr, "unknown command %q\n", cmd)
		fmt.Fprintln(stderr, "commands: migrate, seed, import-aia, import-raio, validate, export, plan, process-wayback, process-wayback-extract, process-regional, process-foreign-search")
		return exitUsage
	}
}

// ── migrate ──────────────────────────────────────────────────────────────────

func runMigrate(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("migrate", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "migrate: --db is required")
		fs.Usage()
		return exitUsage
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "migrate: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	if err := migrations.Apply(ctx, db); err != nil {
		fmt.Fprintf(stderr, "migrate: %v\n", err)
		return exitFailure
	}
	return exitOK
}

// ── seed ─────────────────────────────────────────────────────────────────────

func runSeed(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("seed", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "seed: --db is required")
		fs.Usage()
		return exitUsage
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "seed: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	if _, err := seed.Apply(ctx, db); err != nil {
		fmt.Fprintf(stderr, "seed: %v\n", err)
		return exitFailure
	}
	return exitOK
}

// ── import-aia / import-raio ──────────────────────────────────────────────────

func runImport(ctx context.Context, args []string, stdout, stderr io.Writer, which string) int {
	fs := flag.NewFlagSet("import-"+which, flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	sourceFile := fs.String("source-file", "", "local file to use instead of fetching")
	sourceURL := fs.String("source-url", "", "URL to fetch (overrides the seeded default)")
	userAgent := fs.String("user-agent", "aviation-coverage/1.0", "HTTP User-Agent")
	timeout := fs.Duration("timeout", 30*time.Second, "HTTP fetch timeout")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintf(stderr, "import-%s: --db is required\n", which)
		fs.Usage()
		return exitUsage
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "import-%s: open db: %v\n", which, err)
		return exitFailure
	}
	defer db.Close()

	var input common.Input

	if *sourceFile != "" {
		// Offline mode: read bytes from file.
		body, err := os.ReadFile(*sourceFile)
		if err != nil {
			fmt.Fprintf(stderr, "import-%s: read source-file: %v\n", which, err)
			return exitFailure
		}
		url := *sourceURL
		if url == "" {
			url = defaultSourceURL(which)
		}
		input = common.Input{
			SourceURL: url,
			Body:      body,
			FetchedAt: time.Now().UTC(),
		}
	} else {
		// Live mode: fetch via HTTP.
		url := *sourceURL
		if url == "" {
			url = defaultSourceURL(which)
		}
		resp, err := fetch.Get(ctx, &http.Client{}, fetch.Request{
			URL:       url,
			UserAgent: *userAgent,
			Timeout:   *timeout,
			MaxBytes:  32 * 1024 * 1024, // 32 MiB
			Retries:   2,
		})
		if err != nil {
			fmt.Fprintf(stderr, "import-%s: fetch: %v\n", which, err)
			return exitFailure
		}
		input = common.Input{
			SourceURL:    url,
			Body:         resp.Body,
			FetchedAt:    resp.FetchedAt,
			FinalURL:     resp.FinalURL,
			StatusCode:   resp.StatusCode,
			ContentType:  resp.ContentType,
			ETag:         resp.ETag,
			LastModified: resp.LastModified,
		}
	}

	var result common.Result
	switch which {
	case "aia":
		result, err = aia.Import(ctx, db, input)
	case "raio":
		result, err = raio.Import(ctx, db, input)
	}
	if err != nil {
		fmt.Fprintf(stderr, "import-%s: %v\n", which, err)
		return exitFailure
	}

	if err := json.NewEncoder(stdout).Encode(result); err != nil {
		fmt.Fprintf(stderr, "import-%s: encode result: %v\n", which, err)
		return exitFailure
	}

	if result.Status == "failed" {
		return exitFailure
	}
	return exitOK
}

// defaultSourceURL returns the canonical ICAO URL for each importer.
func defaultSourceURL(which string) string {
	switch which {
	case "aia":
		return "https://www.icao.int/safety/airnavigation/AIG/Pages/AIA-States.aspx"
	case "raio":
		return "https://www.icao.int/safety/airnavigation/AIG/Pages/Regional-Accident-Incident-Investigation-Organizations.aspx"
	}
	return ""
}

// ── validate ─────────────────────────────────────────────────────────────────

func runValidate(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("validate", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	strictConflicts := fs.Bool("strict-conflicts", false, "treat open import conflicts as errors")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "validate: --db is required")
		fs.Usage()
		return exitUsage
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "validate: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	report := validation.Run(ctx, db, validation.Options{
		ConflictsAreErrors: *strictConflicts,
	})

	if err := json.NewEncoder(stdout).Encode(report); err != nil {
		fmt.Fprintf(stderr, "validate: encode report: %v\n", err)
		return exitFailure
	}

	if report.HasErrors() {
		return exitFailure
	}
	return exitOK
}

// ── export ────────────────────────────────────────────────────────────────────

func runExport(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("export", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	format := fs.String("format", "", "output format (only 'json' is supported)")
	output := fs.String("output", "", "output file path (required)")
	generatedAt := fs.String("generated-at", "", "RFC3339 timestamp for generated_at (default: now)")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "export: --db is required")
		fs.Usage()
		return exitUsage
	}
	if *format != "json" {
		fmt.Fprintf(stderr, "export: --format must be 'json', got %q\n", *format)
		return exitUsage
	}
	if *output == "" {
		fmt.Fprintln(stderr, "export: --output is required")
		fs.Usage()
		return exitUsage
	}

	var genAt time.Time
	if *generatedAt != "" {
		t, err := time.Parse(time.RFC3339, *generatedAt)
		if err != nil {
			fmt.Fprintf(stderr, "export: --generated-at: invalid RFC3339 value %q: %v\n", *generatedAt, err)
			return exitUsage
		}
		genAt = t.UTC()
	} else {
		genAt = time.Now().UTC()
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "export: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	if err := export.WriteJSON(ctx, db, *output, genAt); err != nil {
		fmt.Fprintf(stderr, "export: %v\n", err)
		return exitFailure
	}
	return exitOK
}

// ── plan ─────────────────────────────────────────────────────────────────────

func runPlan(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("plan", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	enqueue := fs.Bool("enqueue", false, "write pending crawl_jobs instead of dry-run")
	limit := fs.Int("limit", 0, "cap to the top-N ranked countries (0 = no cap)")
	generatedAt := fs.String("generated-at", "", "RFC3339 timestamp for generated_at (default: now)")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "plan: --db is required")
		fs.Usage()
		return exitUsage
	}

	var nowT time.Time
	if *generatedAt != "" {
		t, err := time.Parse(time.RFC3339, *generatedAt)
		if err != nil {
			fmt.Fprintf(stderr, "plan: --generated-at: invalid RFC3339 value %q: %v\n", *generatedAt, err)
			return exitUsage
		}
		nowT = t.UTC()
	} else {
		nowT = time.Now().UTC()
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "plan: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	p, err := planner.BuildPlan(ctx, db, nowT.UnixMilli(), *limit)
	if err != nil {
		fmt.Fprintf(stderr, "plan: %v\n", err)
		return exitFailure
	}

	if *enqueue {
		inserted, err := planner.Enqueue(ctx, db, p)
		if err != nil {
			fmt.Fprintf(stderr, "plan: enqueue: %v\n", err)
			return exitFailure
		}
		fmt.Fprintf(stderr, "enqueued %d, skipped %d\n", inserted, len(p.Jobs)-inserted)
		return exitOK
	}

	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(p); err != nil {
		fmt.Fprintf(stderr, "plan: encode: %v\n", err)
		return exitFailure
	}
	return exitOK
}

// ── process-wayback-extract ──────────────────────────────────────────────────

func runProcessWaybackExtract(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("process-wayback-extract", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	limit := fs.Int("limit", 0, "max documents to process (0 = no cap)")
	storeDir := fs.String("store-dir", "./wayback-store", "directory for OCR text artifacts")
	ocrEndpoint := fs.String("ocr-endpoint", "http://127.0.0.1:8021/ocr", "OCR HTTP endpoint")
	llmEndpoint := fs.String("llm-endpoint", "http://127.0.0.1:11434/api/generate", "Ollama generate endpoint")
	llmModel := fs.String("llm-model", "qwen3.6-rw", "LLM model name")
	maxInputChars := fs.Int("max-input-chars", 24000, "truncate OCR text to this many chars before LLM")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "process-wayback-extract: --db is required")
		fs.Usage()
		return exitUsage
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "process-wayback-extract: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	ocr := wayback.NewHTTPOCRClient(*ocrEndpoint, 600*time.Second)
	llm := wayback.NewHTTPLLMClient(*llmEndpoint, *llmModel, *maxInputChars, 120*time.Second)
	stats, err := extract.ProcessExtractPending(ctx, db, ocr, llm, *storeDir, *limit, extract.WaybackSource{})
	if err != nil {
		fmt.Fprintf(stderr, "process-wayback-extract: %v\n", err)
		return exitFailure
	}
	fmt.Fprintf(stderr, "extracted=%d skipped=%d failed=%d\n", stats.Extracted, stats.Skipped, stats.Failed)
	return exitOK
}

// ── process-wayback ──────────────────────────────────────────────────────────

func runProcessWayback(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("process-wayback", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	limit := fs.Int("limit", 0, "max pending jobs to process (0 = no cap)")
	storeDir := fs.String("store-dir", "./wayback-store", "directory for downloaded PDFs")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "process-wayback: --db is required")
		fs.Usage()
		return exitUsage
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "process-wayback: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	fetcher := wayback.NewHTTPFetcher(30 * time.Second)
	processed, err := wayback.ProcessPending(ctx, db, fetcher, *storeDir, *limit)
	if err != nil {
		fmt.Fprintf(stderr, "process-wayback: %v\n", err)
		return exitFailure
	}
	fmt.Fprintf(stderr, "processed %d\n", processed)
	return exitOK
}

// ── process-regional ──────────────────────────────────────────────────────────

func runProcessRegional(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("process-regional", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	limit := fs.Int("limit", 0, "max pending jobs to process (0 = no cap)")
	sourceFile := fs.String("source-file", "", "out-of-band listing export (for Cloudflare/TLS-blocked bodies)")
	renderEndpoint := fs.String("render-endpoint", "", "browser-render service URL (e.g. http://127.0.0.1:18030/render) used to fetch JS/Cloudflare-gated listings live")
	body := fs.String("body", "", "restrict to one body (ECCAA|BAGAIA|IAC); required with --source-file")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "process-regional: --db is required")
		fs.Usage()
		return exitUsage
	}
	// An out-of-band export is body-specific: feeding it to the other bodies'
	// clients would mis-parse its relative links against the wrong origin and
	// stage records under the wrong body. Require --body to scope the run.
	if *sourceFile != "" && *body == "" {
		fmt.Fprintln(stderr, "process-regional: --body is required with --source-file (an export is body-specific)")
		fs.Usage()
		return exitUsage
	}
	switch *body {
	case "", "ECCAA", "BAGAIA", "IAC":
	default:
		fmt.Fprintf(stderr, "process-regional: invalid --body %q (want ECCAA|BAGAIA|IAC)\n", *body)
		return exitUsage
	}

	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "process-regional: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	clients := regional.Clients{
		ECCAA:  regional.NewECCAAClient(30*time.Second, *sourceFile, *renderEndpoint),
		BAGAIA: regional.NewBAGAIAClient(30*time.Second, *sourceFile, *renderEndpoint),
		IAC:    regional.NewIACClient(30*time.Second, *sourceFile, *renderEndpoint),
	}
	processed, err := regional.ProcessPending(ctx, db, clients, *limit, *body)
	if err != nil {
		fmt.Fprintf(stderr, "process-regional: %v\n", err)
		return exitFailure
	}
	fmt.Fprintf(stderr, "processed %d\n", processed)
	return exitOK
}

// ── process-foreign-search ───────────────────────────────────────────────────

func runProcessForeign(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("process-foreign-search", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "path to SQLite database file (required)")
	limit := fs.Int("limit", 0, "max pending jobs to process (0 = no cap)")
	sourceFile := fs.String("source-file", "", "ATSB out-of-band export file (required for atsb_search jobs)")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}
	if *dbPath == "" {
		fmt.Fprintln(stderr, "process-foreign-search: --db is required")
		fs.Usage()
		return exitUsage
	}
	db, err := database.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "process-foreign-search: open db: %v\n", err)
		return exitFailure
	}
	defer db.Close()

	clients := foreignsearch.Clients{
		NTSB: foreignsearch.NewNTSBClient(30 * time.Second),
		BEA:  foreignsearch.NewBEAClient(30 * time.Second),
		ATSB: foreignsearch.NewATSBClient(*sourceFile),
	}
	processed, err := foreignsearch.ProcessPending(ctx, db, clients, *limit)
	if err != nil {
		fmt.Fprintf(stderr, "process-foreign-search: %v\n", err)
		return exitFailure
	}
	fmt.Fprintf(stderr, "processed %d\n", processed)
	return exitOK
}
