# SHK ingest — smoke results (2026-06-04, Mac)

```
discover: 383 (sitemap1.xml.gz; JS listing NOT parseable)
```

| case | lang | tier | narrative |
|---|---|---|---|
| helicopter-accident-to-se-hlk-at-joesjo | en | pdf | 21K (rl2005_08e.pdf, RL 2005:08) |
| ...saab-340b-goulburn (ATSB) | sv* | pdf | 20K (*EN content, no e-suffix → mis-tag harmless) |

- Slug date prefixes = 2023-11 site-migration batch, NOT occurrence dates → stripped for case_id.
- Occurrence date from <time> DISPLAY text ("7 July 2004"; datetime attr is UTC-shifted a day).
- PDF pick: full-EN (e.pdf/_eng) > Summary ("Summary in English" must check summary FIRST) > Swedish (SV→EN at P3).
- Ongoing investigations: no PDF → row stays 'new', self-heals on weekly cycle.
