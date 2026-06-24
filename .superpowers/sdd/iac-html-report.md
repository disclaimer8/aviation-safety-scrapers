# IAC html report-page extraction — implementation report

## What was changed

### New file: `internal/worker/extract/htmltext.go`
Pure, stdlib-only HTML-to-text helper. Three regexes: `scriptRe` strips `<script>`/`<style>` blocks, `htmlTagRe` strips all remaining tags (substituting a space so adjacent field values don't run together), `wsRunRe` collapses whitespace. `html.UnescapeString` then unescapes entities. Returns `""` for whitespace-only input.

### New file: `internal/worker/extract/htmltext_test.go`
9 sub-tests covering: script/style stripping, tag removal, entity unescaping, whitespace collapse, empty/whitespace-only/tags-only input, and a realistic IAC-style report page fixture that checks all visible field values are preserved and `analytics`/`display: none` are stripped.

### Modified: `internal/worker/extract/download.go`
Factored the guarded-fetch logic out of `DownloadReportURL` into a new unexported helper `fetchGuarded(ctx, client, rawURL) ([]byte, error)`:
- Checks scheme allow-list (http/https only)
- Clones the caller's `*http.Client` and overrides `Transport` with `hardenedTransport` (SSRF-safe dial context)
- Caps body at `maxReportBytes` via `io.LimitReader(…, max+1)` + explicit error
- `DownloadReportURL` now delegates to `fetchGuarded` and wraps the error with `"extract: download: %w"` to preserve the pre-refactor error prefix for callers

All 16 existing download tests pass unchanged.

### Modified: `internal/worker/extract/regional_source.go`

**Imports added:** `crypto/sha256`, `encoding/hex`, `path/filepath`, `strings`, `atomicfile` package.

**`PendingDocs` WHERE clause broadened:**
```sql
-- Before:
WHERE d.report_url IS NOT NULL AND d.report_url != ''
-- After:
WHERE ((d.report_url IS NOT NULL AND d.report_url != '')
       OR (d.original_url IS NOT NULL AND d.original_url != ''))
```
Also changed `d.report_url` scan to `coalesce(d.report_url,'')` so NULL report_url scans into `d.ArchivedURL` as `""` without a scan error.

**`EnsureDownloaded` branched on pdf vs html:**
```
if doc.ArchivedURL != "" {
    // existing PDF path — unchanged behaviour
} else {
    // html-page path:
    // 1. fetchGuarded(ctx, client, doc.OriginalURL)  ← SSRF-guarded
    // 2. htmlToText(body)                             ← strip tags/script/style
    // 3. reject empty text → download_status='failed'
    // 4. sha256(rawPageBytes) → <iso2>/<digest>.txt  ← content-addressed
    // 5. UPDATE SET download_status='downloaded', ocr_text_path=?, local_file_path=?
    // 6. doc.OCRTextPath = sql.NullString{Valid:true} ← signals core to skip OCR
}
```
The core's OCRTextPath-skip (`if !doc.OCRTextPath.Valid || doc.OCRTextPath.String == ""`) already exists in `core.go:34` — no core change needed.

### Modified: `internal/worker/extract/regional_source_test.go`

**Updated existing test:** `TestRegionalPendingDocsExcludesDocWithoutReportURL` → renamed `TestRegionalPendingDocsIncludesHtmlOnlyDoc`. The old assertion (docs with `original_url` only must be excluded) is now inverted: both the PDF doc and the html-only doc must be returned. This is the correct semantic after the feature change. Note: the schema has `original_url TEXT NOT NULL` so every row always has an original_url; the pdf vs html distinction is driven solely by whether `report_url` is populated.

**New helper:** `seedRegionalHtmlDoc` — inserts a staged row with `original_url` set and `report_url` NULL (IAC pattern), using body code `"IAC"` (which is in the schema's CHECK constraint).

**New tests:**
- `TestRegionalPendingDocsReturnsHtmlDoc` — asserts that an IAC-style row is returned by PendingDocs with `ArchivedURL=""` and correct `OriginalURL`.
- `TestRegionalEnsureDownloadedHtmlPage` — end-to-end: httptest server returns IAC HTML; after EnsureDownloaded, `doc.OCRTextPath.Valid=true`, the `.txt` file contains visible field text (RA-40440, 19.05.2026, Ан-2, Самолёт), `download_status='downloaded'`, DB `ocr_text_path` matches.
- `TestRegionalEnsureDownloadedHtmlPageFailsOnEmptyBody` — page with only whitespace after stripping → `download_status='failed'`, error returned.

## fetchGuarded factoring

`DownloadReportURL` went from ~55 lines to ~15 lines by delegating the network-level guard to `fetchGuarded`. The error prefix wrapping (`"extract: download: %w"`) preserves backward compatibility with any callers that match `"extract: download:"` in error strings. The html-page path in `EnsureDownloaded` calls `fetchGuarded` directly — same SSRF guard, same size cap, no code duplication.

## RED/GREEN evidence

**RED** (before implementation):
```
TestRegionalPendingDocsReturnsHtmlDoc: html-page doc 1 not returned by PendingDocs
TestRegionalEnsureDownloadedHtmlPage: html-page doc 1 not found in PendingDocs
TestRegionalEnsureDownloadedHtmlPageFailsOnEmptyBody: html-page doc 1 not found in PendingDocs
```

**GREEN** (after implementation): all 3 new tests + 9 HtmlText tests + all pre-existing tests pass.

## Final verification

```
go test ./internal/worker/extract/ -run 'Regional|HtmlText|Download' -v  → PASS (all)
go build ./...                                                             → ok
go vet ./...                                                               → ok
gofmt -l internal/worker/extract/                                         → (empty)
go test ./...                                                              → all 21 packages PASS
```

## Concerns

1. **IAC HTML structure assumed simple**: the `htmlToText` function is regex-based and handles flat HTML well. If IAC uses JS-rendered content (dynamic DOM), the fetched HTML would be the server-side skeleton only. From the brief, IAC pages are server-rendered — this assumption should be verified against a live page before production.

2. **`local_file_path` set to text path**: for the html path, both `local_file_path` and `ocr_text_path` are set to the same `.txt` path. The core reads `ocr_text_path` for text (correct). `local_file_path` is only used for idempotency (file-exists check) — setting it to the text path is consistent and correct.

3. **Body code CHECK constraint**: the test infrastructure uses `"IAC"` as body code (the actual IAC code). Expanding to other html-page bodies (if any are added) would require adding them to the migration CHECK.

4. **No two-phase test for html path**: the `TestRegionalPendingDocsTwoPhaseFlow` only tests the PDF path. A symmetrical test for the html path would be a useful addition but is not required by the brief.

## Files changed

- `/Users/denyskolomiiets/ass-extract/control-plane/internal/worker/extract/htmltext.go` (new)
- `/Users/denyskolomiiets/ass-extract/control-plane/internal/worker/extract/htmltext_test.go` (new)
- `/Users/denyskolomiiets/ass-extract/control-plane/internal/worker/extract/download.go` (fetchGuarded factored out)
- `/Users/denyskolomiiets/ass-extract/control-plane/internal/worker/extract/regional_source.go` (PendingDocs + EnsureDownloaded)
- `/Users/denyskolomiiets/ass-extract/control-plane/internal/worker/extract/regional_source_test.go` (updated + 3 new tests)
