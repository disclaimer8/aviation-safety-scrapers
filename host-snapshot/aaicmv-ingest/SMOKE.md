# AAIC Maldives (aaicmv) — smoke evidence

Source body: Accident Investigation Coordinating Committee (AICC), Republic of Maldives.
Host: https://caa.gov.mv/accidents-incidents (single server-rendered table).

## Discovery
- `discover` → 45 reports parsed from the listing table (Final + Preliminary docs).
- case_id is INTRINSIC: `MV-<YYYY-NN>-<final|prelim|report>`, no encounter-order suffix.
  Final vs Preliminary of the same reference are distinct case_ids.

## Fetch / parse / build (live, 3 recent finals)
| case_id          | event_date | registration | country | narr_len |
|------------------|------------|--------------|---------|----------|
| MV-2024-03-final | 2024-10-13 | 8Q-TBB       | MV      | 28396    |
| MV-2024-02-final | 2024-06-09 | 8Q-TMO       | MV      | 38330    |
| MV-2023-03-final | 2023-10-25 | 8Q-RAL       | MV      | 26424    |

All English. site_slug lowercased (e.g. `crash-dhc-6-300-8q-tbb`).
Old (2001–2015) reports may be image-only scans → tier 'scanned' → skipped at build.

## Tests
36 passed (incl. 7 non-bleed vs aaicnp/aaib/aaid/aaiu and generic aaic).
