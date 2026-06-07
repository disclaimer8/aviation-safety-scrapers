# AAIU ingest — smoke results (2026-06-04, Mac)

```
discover --max-pages 1: 100 rows (x-wp-total: 560, 6 REST pages)
```

| case | tier | narrative |
|---|---|---|
| 2019-003 | pdf | 93K ch (A330 EI-LAX) |
| 2019-004 | pdf | 74K ch |

- Open WP REST API /wp-json/wp/v2/aaiu_report?per_page=100&page=1..6.
- Metadata from TITLE (formats drift; 12/560 legacy CAPS rows lack a
  report number → case_id wp-{id}); synopsis from content.rendered.
- PDF linked from the POST PAGE, under TWO paths: /wp-content/uploads/
  (modern) and /sites/default/files/report-attachments/ (legacy, literal
  spaces → percent-encode).
- ⚠️ aaiu.ie TLS: leaf served WITHOUT the Sectigo DV R36 intermediate —
  httpx/certifi fails ("unable to verify the first certificate") while
  curl recovers via AIA. Fix: pinned intermediate shipped in the package,
  combined CA bundle at client init.
- No-PDF posts: synopsis fallback (tier html).
