# CIAIA (Uruguay) ingest — smoke evidence

Source: gub.uy, Ministerio de Defensa Nacional, Comisión Investigadora de
Accidentes e Incidentes de Aviación (CIAIA). country_iso = UY.

Anchor-driven scraper: crawls a fixed seed list of server-rendered listing
tables and unions every distinct PDF anchor (URLs scraped exactly, never
constructed). Metadata derived from anchor text + filename.

## discover (live, 2026-06-07)
    $ python -m ciaiauy_ingest.cli discover --db ciaiauy.db
    discovered: 75
  - 75 reports, 74 with registration, 31 with caso-NNN ids, 0 duplicate case_ids
  - event_class: Accident 49 / Serious incident 26

## fetch + parse + build (live, 4 sample reports)
    fetched: 4   parsed: 4   built: 4
    caso-515  N527K     Accident          narr_chars=109921  tier=pdf
    caso-549  CX-BVK-R  Accident          narr_chars=21245   tier=pdf
    caso-538  EC-GPB    Accident          narr_chars=26088   tier=pdf
    caso-496  N496      Serious incident  narr_chars=1561    tier=pdf
  - narratives are Spanish text-layer ("MINISTERIO DE DEFENSA NACIONAL …
    COMISIÓN INVESTIGADORA … (C.I.A.I.A.) INFORME FINAL No. 549 …")

## tests
    $ python -m pytest -q
    54 passed
  - includes CIAIAC (Spain) non-bleed tests: 'ciaia' is a prefix of 'ciaiac';
    exact-match routing proven both directions; UY ids never match the
    A-NNN/YYYY Spanish Ref. shape; UY db has no ciaiac_* tables.

## case_id
  - 'caso-NNN' when a Caso number is reliably present in the filename
    (leading/dated/no.-prefixed; aircraft MODEL numbers like PA32RT-300T
    are NOT mistaken for Caso numbers), else the registration slug
    (e.g. 'cx-mgp'); collision-suffixed for uniqueness.
