# host-snapshot — a raw copy of what actually runs

This directory is **not part of the build**. Nothing here is tested, imported,
or covered by the registry. It exists because on 2026-09-11 a check of the
systemd units against the live mini-PC found that this repository describes a
machine that does not exist, and that a large part of the running system was
not in version control at all.

## What was found

| | in this repository | on the mini-PC |
|---|---|---|
| systemd units | 42, `WorkingDirectory=/opt/<code>`, `User=scraper` | **77**, `%h/<code>-ingest`, `User=a1` |
| units using `/opt` | all 42 | **0** |
| `deploy/run-cycle.sh` | — | **40 of 42 differ** |
| ingest source directories | 90 | **129** |
| of those, with a git remote | — | **0 of 129** |
| `$HOME/*.sh` operational scripts | 0 | **88** (78 of them `*-sync-to-prod.sh`) |

The 39 sources here had no copy in any repository. Neither did the 88 scripts:
`$HOME` on that box is not a git repository, the 29 `*-ingest` directories that
do contain a `.git` have **no remote**, and the only backup job on the machine
(`flightfinder-backup-pull`) copies data *onto* it from Hetzner rather than off
it. So roughly 47,000 lines of working code — including the whole delivery path
from scraper to production — existed on one disk, in one place.

The deployed `run-cycle.sh` scripts are not merely path variants of the ones in
`sources/*/deploy/`. They carry logic the repository never received: the
`OCR_REMOTE` offload, a measured incremental-vs-full discover strategy (bea),
and a **Phase 2** that syncs the built database into production via
`<code>-sync-to-prod.sh`. That phase is the link between these scrapers and the
site, and it was written down nowhere else.

## What this copy is, and is not

It is verbatim except for one thing: **the production SSH destinations are
redacted**, because this repository is public. The prod host, the Hetzner IP
and the mini-PC's LAN address are replaced by `${VAR:?set VAR to user@host}`
forms that fail loudly instead of defaulting somewhere — 125 files carried one
of them, in `export OCR_REMOTE=` and `PROD_SSH="${..:-..}"` shapes. Nothing else was edited — no tidying, no renaming, no conversion to
the four-verb shape. That work happens source by source, out of here and into
`sources/`.

Excluded: virtualenvs, `node_modules`, caches, and every harvested artefact
(`*.db`, `pdfs/`, logs). Only code.

`_deployed-units/` holds the 77 `*-cycle.service` / `.timer` files as they exist
in `/etc/systemd/system` — the authoritative version, against which the
templates in `sources/*/deploy/` are the stale copy.

## Before trusting anything in here

It is a snapshot of a running system, not reviewed code. It has not been
through the fixes the rest of this tree received in the code-review work:
fail-closed discover, the shared retry policy, the SSRF guard, robots.txt
enforcement, path sanitisation. Assume every finding in `CODE-REVIEW.md`
applies to these 39 sources too, and more besides.

## What CodeQL already found here

Adding this directory turned up 15 high-severity alerts on the first scan —
which is the point of having it in a repository at all. They are recorded here
as the starting worklist, and dismissed in the security tab as "not built or
run from this repository" so they do not block unrelated pull requests. None is
a credential; the snapshot was scanned for those separately before it landed.

**ReDoS in scraper code (6)** — real bugs, in the regexes these six use to
parse listings. Fix them as each source is converted into `sources/`:

    aaibzm-ingest/aaibzm_ingest/aaibzm.py:308
    aet-ingest/aet_ingest/aet.py:363
    beacg-ingest/beacg_ingest/beacg.py:325
    gcaagy-ingest/gcaagy_ingest/gcaagy.py:268
    jcaa-ingest/jcaa_ingest/jcaa.py:297
    ttcaa-ingest/ttcaa_ingest/ttcaa.py:211

**Incomplete URL substring sanitization (9)** — all in test files, the same
class already triaged and dismissed on `main` for `sources/tsb` and
`sources/bea`. Worth tightening when the tests are rewritten, not before:

    aaicnp-ingest/tests/test_aaicnp.py:122, 123, 228
    baaid-ingest/tests/test_pipeline.py:44
    baaid-ingest/tests/test_nonbleed.py:60
    bdca-ingest/tests/test_bdca.py:114
    dgcakw-ingest/tests/test_pipeline.py:44, 94

This list is a floor, not a ceiling. CodeQL sees what CodeQL sees; the findings
in `CODE-REVIEW.md` — silent truncation, missing retries, unsanitised paths —
are not things it checks for, and these 39 sources have had none of those fixes.

## What Dependabot found, including one npm cannot fix

Eight open alerts on the first scan. These are the first dependency report this
code has ever had: nothing was watching it while it lived on the mini-PC.

**`xlsx` — high, and there is no fix on npm (6 alerts).**

    laser-ingest, uas-ingest, wildlife-ingest

GHSA-5pgg-2g8v-p4x9 (ReDoS) and the prototype-pollution advisory both want
`xlsx >= 0.20.2`. The npm registry's latest is **0.18.5** — SheetJS stopped
publishing there and ships from its own CDN instead, so Dependabot will report
this for ever and can never resolve it. Verified against the registry on
2026-09-11.

This one matters beyond the archive: laser, uas and wildlife are live
FlightFinder data verticals, and they run this code on the ingest host today.
Closing it means either pulling `xlsx` from the SheetJS CDN with an integrity
pin, or reading those FAA spreadsheets with something else. Neither is a
Dependabot PR.

**`csv-parse` — medium, fixable (2 alerts).**

    ourairports-ingest, wildlife-ingest

`< 7.0.2`, prototype replacement reachable via `columns`. Dismissed here as
archived code rather than patched, because patching a snapshot stops it being
one — fix it when the source moves into `sources/`.

`host-snapshot/` is excluded from Dependabot's version updates by omission from
every glob in `.github/dependabot.yml`. Security updates ignore that config, so
an occasional PR against the archive is expected; close it and dismiss the
alert.
