# Security hardening report: DownloadReportURL

**File:** `control-plane/internal/worker/extract/download.go`
**Date:** 2026-06-23

---

## Finding 1 — SSRF guard (FIXED)

**Problem:** `report_url` comes from semi-untrusted scraped listings. A malicious or compromised listing could point to `http://127.0.0.1:8021`, `http://169.254.169.254/...` (cloud metadata), `http://192.168.x.x`, `http://[::1]`, or a public hostname that resolves to a private IP (including redirect hops).

**Fix:**
- Added `isPrivateIP(net.IP) bool` that blocks loopback, RFC1918, link-local (169.254/fe80), unique-local (fc00::/7), multicast, and unspecified IPs.
- Added `ssrfSafeDialContext` — a `DialContext` func that resolves the host with `net.DefaultResolver.LookupIPAddr` and calls `isPrivateIP` on every resolved address before dialing. If any address is private the connection is refused with a descriptive error.
- Added `hardenedTransport(base http.RoundTripper)` that clones the caller's transport and injects `dialContextOverride` as its `DialContext`. Default value of `dialContextOverride` is `ssrfSafeDialContext`.
- `DownloadReportURL` now shallow-copies the caller's `*http.Client` (preserving `Timeout`, `Jar`, `CheckRedirect`) and replaces its `Transport` with the hardened one. Every HTTP hop (including redirects) goes through `DialContext`, so redirect chains are covered automatically.
- A blocked target returns an error containing `"ssrf-guard"` → callers map to `download_status='failed'`.

**Test (`TestDownloadReportURL_SSRFBlocked`):**
- `isPrivateIP` checked for 10 distinct address classes (loopback v4/v6, RFC1918×3, link-local×2, ULA, multicast, unspecified); public `1.1.1.1` verified as allowed.
- `ssrfSafeDialContext` called directly with `127.0.0.1:80` — confirmed error returned.
- End-to-end: `DownloadReportURL` called against an httptest server on `127.0.0.1`; confirmed error returned and no file written to disk.

---

## Finding 2 — Response size cap (FIXED)

**Problem:** `io.ReadAll(resp.Body)` with no limit could exhaust disk and RAM on a mini-PC if a hostile or runaway server returns a giant response.

**Fix:**
- Added `const maxReportBytes = 64 << 20` (64 MiB).
- Replaced `io.ReadAll(resp.Body)` with `io.ReadAll(io.LimitReader(resp.Body, int64(maxReportBytes)+1))`.
- After reading, if `len(body) > maxReportBytes` the function returns an error containing `"exceeds"` and `"limit"` — silent truncation is explicitly avoided.

**Test (`TestDownloadReportURL_OversizedResponse`):**
- Unit subtests verify the LimitReader logic at a cheap scale (16-byte threshold): body over limit → error, body at limit → accepted, body under limit → accepted.
- End-to-end subtest: an httptest handler streams `maxReportBytes+1` bytes; `DownloadReportURL` (with loopback allowed for that subtest) returns an error and writes no file.

---

## Finding 3 — Third distinct issue

**Finding: `io.ReadAll` accumulates the entire body into memory before writing to disk.**

The original code called `io.ReadAll(resp.Body)` then passed the resulting `[]byte` to `atomicfile.Write`. With Finding 2's fix the body is now bounded to 64 MiB, but still: up to 64 MiB is held in memory as a single `[]byte`. On a resource-constrained mini-PC, this matters when multiple downloads run concurrently.

A security reviewer would flag this as "unbounded in-memory accumulation enabling RAM exhaustion under concurrent load." The correct fix is to stream the body through a `sha256.New()` hasher and directly into a temp file via `io.TeeReader + io.Copy`, then atomically rename. This caps peak RSS to one streaming buffer (~32 KB) rather than the full body per goroutine.

**Status of Finding 3:** The streaming rewrite was judged out of scope for this hardening pass (it requires changes to `atomicfile.Write` or bypassing it). The 64 MiB hard cap from Finding 2 substantially mitigates the risk in practice. The issue is documented here so it can be tracked as a follow-up (see: "stream-to-disk instead of ReadAll").

---

## Test evidence

```
=== RUN   TestDownloadReportURL                         PASS (4 subtests)
=== RUN   TestDownloadReportURL_SSRFBlocked             PASS (10 isPrivateIP subtests + 2 end-to-end checks)
=== RUN   TestDownloadReportURL_OversizedResponse       PASS (3 unit subtests + 1 end-to-end)
=== RUN   TestRegionalEnsureDownloadedFetchesAndUpdatesRow  PASS
=== RUN   TestRegionalEnsureDownloadedFailureMarksRow   PASS
=== RUN   TestRegionalPendingDocsTwoPhaseFlow           PASS
go build ./...    — clean
go vet ./...      — clean
gofmt -l          — no output (files already formatted)
go test ./...     — all packages PASS
```
