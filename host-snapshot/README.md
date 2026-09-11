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
