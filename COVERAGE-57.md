<!-- Point-in-time assessment, not a live document. -->

# Coverage assessment — the 57 territories marked `coverage_status='unknown'`

Produced 2026-09-11 by a research agent, every URL fetched and its status code
recorded. Spot-checked by hand before acting on it: Hong Kong 200 / 176 PDF
links, Guyana 200 / 29, Eswatini 200 / 11, Belize 200 (its links are detail
pages, not direct PDFs).

## What has been done since

Promoted into `sources/` on 2026-09-11 and 09-12 — the rows below calling them
unassessed are now out of date:

    Hong Kong (aaiahk)   Guyana (gcaagy)   Belize (bdca)

## What is still open, in order of what it would take

1. **37 DELEGATED territories** — the largest item and the cheapest per
   territory. Their accidents are ALREADY in our data; they sit under the
   investigating authority's country rather than the territory's, which is the
   `country_iso is the bureau's country, not the event's` problem. The
   authorities absorbing them are all ones we scrape:

       BEA 12 · AAIB 12 · NTSB 5 · OVV 4 · HCLJ 2 · STSB 1 · ANSV 1

   This is a location→ISO2 tagging job in the data layer, not scraper work.

2. **Eswatini and São Tomé** — real archives, ~8 and ~7 documents, no scraper.
   Small enough that they were not worth writing one for while larger sources
   were unpromoted.

3. **Five UNRESOLVED** — Eritrea, Lesotho, Mauritius, Niue, Palestine. The
   agent could not determine whether an authority exists, which is a useful
   answer: a wrong URL costs more than a gap.

Five entries below are mis-flagged in the coverage database rather than
genuinely unassessed — AR (`jst`), BR (`cenipa`), SG (`tsib`), MO (`aacm`),
SC (`scaa`) all have working scrapers already.

---

Assessed 2026-09-11. Every URL below was fetched (curl/WebFetch) and the status code is the one actually received that day.

**Coverage: 57 of 57 checked.** 52 resolved, 5 UNRESOLVED (ER, LS, MU, NU, PS).

**Five of the 57 are mis-flagged — they already have scrapers in this repo:** AR (`sources/jst`), BR (`sources/cenipa`), SG (`sources/tsib`), MO (`sources/aacm`), SC (`sources/scaa`). Only AR/BR were named in the brief; SG/MO/SC were found in `registry.yaml`.

## Summary table

| ISO | Country | Verdict | Authority | Docs |
|---|---|---|---|---|
| AR | Argentina | OWN_ARCHIVE (scraper exists) | JST | 3,268 |
| BR | Brazil | OWN_ARCHIVE (scraper exists) | CENIPA | 0 (403 today) |
| SG | Singapore | OWN_ARCHIVE (scraper exists) | TSIB | 104 |
| HK | Hong Kong | OWN_ARCHIVE | AAIA | 78 |
| GY | Guyana | OWN_ARCHIVE | GCAA / AAID | 29 |
| BZ | Belize | OWN_ARCHIVE | BDCA / AIU | 8 |
| SZ | Eswatini | OWN_ARCHIVE | ESWACAA / AAIID | 8 (1 final, 5 interim, 2 pending) |
| ST | Sao Tome & Principe | OWN_ARCHIVE | INAC | 7 |
| MO | Macao | CONTACT_ONLY (scraper exists via Wayback) | AACM | 0 live / 4 archived |
| SC | Seychelles | CONTACT_ONLY (scraper exists, yields 0) | SCAA | 0 |
| KM | Comoros | CONTACT_ONLY | ANACM | 0 |
| DJ | Djibouti | CONTACT_ONLY | AAC Djibouti | 0 |
| GW | Guinea-Bissau | CONTACT_ONLY | AACGB | 0 |
| SS | South Sudan | CONTACT_ONLY | SSCAA | 0 |
| CK | Cook Islands | CONTACT_ONLY | Ministry of Transport (Chief Accident Investigator) | 0 |
| AI | Anguilla | DELEGATED → UK AAIB | AAIB | 1 |
| BM | Bermuda | DELEGATED → UK AAIB | AAIB | 1 |
| KY | Cayman Islands | DELEGATED → UK AAIB | AAIB | 7 |
| FK | Falkland Islands | DELEGATED → UK AAIB | AAIB | 4 |
| GI | Gibraltar | DELEGATED → UK AAIB | AAIB | 1 |
| MS | Montserrat | DELEGATED → UK AAIB | AAIB | 7 |
| SH | St Helena/Ascension/Tristan | DELEGATED → UK AAIB | AAIB | 1 |
| TC | Turks & Caicos | DELEGATED → UK AAIB | AAIB | 6 |
| VG | British Virgin Islands | DELEGATED → UK AAIB | AAIB | 9 |
| GG | Guernsey | DELEGATED → UK AAIB | AAIB | 29 |
| JE | Jersey | DELEGATED → UK AAIB | AAIB | 28 |
| IM | Isle of Man | DELEGATED → UK AAIB | AAIB | 27 |
| GF | French Guiana | DELEGATED → BEA | BEA | 3 |
| GP | Guadeloupe | DELEGATED → BEA | BEA | 5 |
| MQ | Martinique | DELEGATED → BEA | BEA | ≥10 |
| RE | Réunion | DELEGATED → BEA | BEA | 8 |
| YT | Mayotte | DELEGATED → BEA | BEA | 1 |
| NC | New Caledonia | DELEGATED → BEA | BEA | 8 |
| PF | French Polynesia | DELEGATED → BEA | BEA | 7 |
| PM | St Pierre & Miquelon | DELEGATED → BEA | BEA | 1 |
| BL | St Barthélemy | DELEGATED → BEA | BEA | ≥10 |
| MF | St Martin | DELEGATED → BEA | BEA | ≥10 (noisy) |
| WF | Wallis & Futuna | DELEGATED → BEA | BEA | 0 |
| MC | Monaco | DELEGATED → BEA | BEA | 2 |
| AW | Aruba | DELEGATED → Dutch Safety Board (on request) | OVV | 0 |
| CW | Curaçao | DELEGATED → Dutch Safety Board (on request) | OVV | 0 |
| SX | Sint Maarten | DELEGATED → Dutch Safety Board (on request) | OVV | 0 |
| BQ | Bonaire/Sint Eustatius/Saba | DELEGATED → Dutch Safety Board | OVV | 1 |
| AS | American Samoa | DELEGATED → NTSB | NTSB | 0 (not countable) |
| GU | Guam | DELEGATED → NTSB | NTSB | 0 (not countable) |
| MP | Northern Mariana Is. | DELEGATED → NTSB | NTSB | 0 (not countable) |
| PR | Puerto Rico | DELEGATED → NTSB | NTSB | 0 (not countable) |
| VI | US Virgin Islands | DELEGATED → NTSB | NTSB | 0 (not countable) |
| FO | Faroe Islands | DELEGATED → AIB Denmark | HCLJ | 0 (JS search) |
| GL | Greenland | DELEGATED → AIB Denmark | HCLJ | 0 (JS search) |
| LI | Liechtenstein | DELEGATED → Swiss STSB | STSB/SUST | 0 |
| SM | San Marino | DELEGATED → ANSV Italy | ANSV | 0 |
| ER | Eritrea | UNRESOLVED | — | 0 |
| LS | Lesotho | UNRESOLVED | — | 0 |
| MU | Mauritius | UNRESOLVED | — | 0 |
| NU | Niue | UNRESOLVED | — | 0 |
| PS | Palestine | UNRESOLVED | — | 0 |

## Per-country detail

### Already covered by this repo

**AR Argentina** — OWN_ARCHIVE. Authority: Junta de Seguridad en el Transporte (JST). URL (from `sources/jst/jst_ingest/jst.py`, `MANIFEST_URL`): `https://so.jst.gob.ar/static/informes/Index.json`; PDFs at `https://so.jst.gob.ar/static/informes/{path}`. http 200. Documents: 3,268 report entries across 1,990 expedientes (manifest parsed). Format PDF. Evidence: the manifest is a dict keyed by 8-digit expediente → `[{tipo, path}]`.

**BR Brazil** — OWN_ARCHIVE. Authority: CENIPA (Centro de Investigação e Prevenção de Acidentes Aeronáuticos). URL (from `sources/cenipa/cenipa_ingest/cenipa.py`, `LISTING_URL`): `https://sistema.cenipa.fab.mil.br/cenipa/paginas/relatorios/relatorios.php`, paginated via `?&?&pag=N`. http **403** today, both with a generic UA and with the scraper's own Chrome/124 UA — the site currently blocks this machine; documents 0 counted today. Format: HTML table → PDF.

**SG Singapore** — OWN_ARCHIVE; `sources/tsib` exists. Authority: Transport Safety Investigation Bureau (TSIB), Ministry of Transport. URL: `https://www.mot.gov.sg/what-we-do/transport-investigations/aviation/aviation-reports/` http 200. Documents: 104 (filter sidebar states 104 reports, 2000–2025; 10 per page, 11 pages). Format PDF. Evidence: sample title "Turbulence event, Airbus A350-900, 27 June 2025". Note the scraper's asset host `isomer-user-content.by.gov.sg` returned 403 to curl.

**MO Macao** — CONTACT_ONLY (live); `sources/aacm` exists and ingests 4 reports via Wayback. Authority: Civil Aviation Authority of Macao SAR (AACM). URL: `https://www.aacm.gov.mo/en/statute/AviationRegulations/AccidentInvestigation` http 200 — lists only Law 2/2013, no reports; site search for "investigation report"/"accident" returns only navigation. One orphan report PDF exists at `https://www.aacm.gov.mo/static/2025/01/22/f6620cb4-d488-49d1-9dd6-de79c70edbe5.pdf` (200; title "ACCIDENT INVESTIGATION REPORT AIRCRAFT ACCIDENT No. ACCID01/06 … Airbus A321 during push back … 4th March 2006"). Documents 0 on any listing page. Format PDF. The scraper header confirms: "The live site is a Vue SPA with a WAF … 4 confirmed reports … retrieved from Wayback".

**SC Seychelles** — CONTACT_ONLY; `sources/scaa` exists and by design yields 0. Authority: Seychelles Civil Aviation Authority (SCAA). URL: `https://www.scaa.sc/index.php/regulatory/other-regulations` http 200 (curl UA; browser UAs get 403). Documents 0 — the only accident-related PDF is the regulation itself, `/files/THE CIVIL AVIATION (INVESTIGATION OF ACCIDENTS) REGULATIONS.pdf`. Format PDF (regulation only). Evidence: scraper docstring "FINDING (2026-06-09): SCAA publishes NO aircraft accident/incident investigation final reports on its website"; a probe of all 7 SCAA sections found 0 report PDFs.

### OWN_ARCHIVE — new

**HK Hong Kong** — Authority: Air Accident Investigation Authority (AAIA), Transport and Logistics Bureau. URL: `https://www.tlb.gov.hk/aaia/eng/investigation_reports/index.html` http 200. Documents: 78 (single page, 2016–2026, each entry with PLR/ITR/IVR PDFs). Format PDF. Evidence: newest entry "Abnormal Runway Contact of Airbus A330-343 at Hong Kong International Airport" (3 July 2026). Old `thb.gov.hk` host is dead (000).

**GY Guyana** — Authority: Guyana Civil Aviation Authority, Accident Investigation Department (AAID; older reports headed GAAIIU). URL: `https://www.gcaa-gy.org/AAID.html` http 200. Documents: 29, all PDF. Evidence: entries such as "8R-GFA Final Report", "Fly Jamaica Accident Final Report", "Caribbean Airlines 9Y-PBM (Report GCAA 2/5/1/63)".

**BZ Belize** — Authority: Belize Department of Civil Aviation, Accident Investigation Unit (AIU). URL: `https://www.civilaviation.gov.bz/index.php/accident-investigation-unit-aiu/accident-reports` http 200. Documents: 8 PDFs (N402BL 1991, V3-HDV, V3-HDY, V3-HFD ×2, N936AN 2019, V3-HGX 2017, V3-HIN 2023). Format PDF. A second "published-reports" URL from search redirects to a 404 page.

**SZ Eswatini** — Authority: Eswatini Civil Aviation Authority, Aircraft Accident and Incident Investigations Department (AAIID). URL: `https://www.eswacaa.co.sz/aaiid/` http 200. Documents: 8 (final 17-001 ZS-HAJ; 21-001 V5-HOC and 23-001 ZS-EIX pending with 5 interim statements). Format PDF. Evidence: "AAIID will produce and publish on the website, a final report of the investigation."

**ST Sao Tome and Principe** — Authority: INAC (Instituto Nacional de Aviação Civil); investigation body created by Decreto 25/2020 (name not stated on page). URL: `https://www.inac.st/teste/index.php/acidente-e-incidente-de-aeronave` http 200. Documents: 7 reports (CS-TST 2023, Twin Otter 2006, UR-ALG SAAB, UR-CKC 2017, S9-AUN 2018, TR-ABS 2019, CS-DVQ serious incident) plus 2 regulatory PDFs. Format PDF. Note the `/teste/` path — looks like a staging path that is live.

### CONTACT_ONLY — new

**KM Comoros** — ANACM (Agence Nationale de l'Aviation Civile et de la Météorologie). `https://anacm-comores.com/` http 200. Documents 0 — no investigation/report section anywhere on the site.

**DJ Djibouti** — Autorité de l'Aviation Civile de Djibouti. `https://djibaviationcivile.com/` http 200 but JS-only shell (header + nav icon); documents 0. Not verifiable beyond existence.

**GW Guinea-Bissau** — Autoridade de Aviação Civil da Guiné-Bissau (AACGB). `https://aacgb.gw/planos-e-relatorios.php` http 200. Documents 0. Evidence: "Os planos e relatórios estão sendo compilados e serão publicados em breve." Not a BAGAIA member.

**SS South Sudan** — South Sudan Civil Aviation Authority. `https://sscaa.gov.ss/` http 200. Documents 0 — no AIB/investigation section (Air Navigation, Safety Oversight, Security, Regulations, Flight Info only).

**CK Cook Islands** — Ministry of Transport; Civil Aviation Act 2002 provides for a Chief Accident Investigator. URL: AIP Cook Islands GEN 1.1, `https://www.transport.gov.ck/wp-content/uploads/2024/06/aip_cookislands_master_13jun24_bkmk.pdf` http 200. Documents 0. Evidence: "The Minister of Transport must appoint a Chief Accident Investigator when this is recommended by the Secretary for Transport. The functions of the Chief Accident Investigator are defined in the Civil Aviation Act 2002." The ministry's accident-reporting page (200) covers maritime only. TAIC's AO-2024-001 (Timaru) is not a Cook Islands case.

### DELEGATED — UK AAIB (`sources/aaib` exists)

Delegation statement: `https://www.gov.uk/government/news/how-we-work` http 200 — "The Air Accidents Investigation Branch investigates civil aircraft accidents and serious incidents within the UK and its overseas territories and crown dependencies." The OT guidance PDF (`https://assets.publishing.service.gov.uk/government/uploads/system/uploads/attachment_data/file/524516/Guidance_Overseas_Territories.pdf`, 200) lists regulations made for Anguilla, Bermuda, BVI, Cayman Islands, Falkland Islands, Montserrat, TCI, and "The Government of Gibraltar enabled similar regulations in January 2009." Documents below are keyword hits on `https://www.gov.uk/aaib-reports?keywords=…` (all 200) — keyword search, so it includes aircraft merely registered/operating there; format HTML + PDF.

- **AI** 1 ("Cessna 402, VP-AAK Heavy landing due to windshear, Anguilla Airport").
- **BM** 1 (Cessna 172M N9085H). Delegation restated at `https://www.bcaa.bm/accident-investigation-regulations` (200): "The Air Accidents Investigation Branch (AAIB) is the accident investigation authority for Bermuda…".
- **KY** 7 (e.g. "EC135T1, VP-CPS").
- **FK** 4 ("BN2B-26 Islander, VP-FBM Hard landing in undershoot").
- **GI** 1 ("Boeing 757-2T7, G-MONE, 17 March 2006").
- **MS** 7 ("BN 2B-26 Islander, J8-VBI Runway excursion").
- **SH** 1 — `https://www.gov.uk/aaib-reports/aaib-investigation-to-embraer-190ar-zs-yad` (200): "The AAIB, as the nominated State Accident Investigation Authority for the British Overseas Territories of Saint Helena, Ascension and Tristan da Cunha … under the St Helena Civil Aviation (Investigation of Air Accidents and Incidents) Regulations 2019".
- **TC** 6 ("Cessna 402C, VQ-TIN Wheels up landing, Ambergris Cay").
- **VG** 9 ("Cessna 402C, N603AB Overrun on landing, Gorda Airport").
- **GG** 29, **JE** 28, **IM** 27 (Crown Dependencies; e.g. "DHC-8-402, G-FLBB Pressurisation failure").

### DELEGATED — BEA France (`sources/bea` exists)

The BEA legal-framework page (`https://bea.aero/le-bea/cadre-juridique/`) does not spell out overseas competence; the evidence is the BEA database itself: the state-of-occurrence facet is "France (métropole et outre-mer)" (3,137) and free-text searches on `https://bea.aero/les-enquetes/evenements-notifies/` (200, param `tx_news_pi1[search][subject]`) return BEA-led cases, e.g. "Incident de l'Airbus A330 immatriculé EC-ORP exploité par Wamos Air le 03/09/2026 vers AD La Réunion – Roland Garros — Enquête en charge France - BEA". Format HTML cards → PDF reports. First-page hit counts (page shows max 10; no pagination detected): **GP** 5, **MQ** 10, **GF** 3, **RE** 8, **YT** 1, **NC** 8, **PF** 7, **PM** 1, **BL** 10, **MF** 10 (term "Saint-Martin" also matches metropolitan places), **WF** 0.

**MC Monaco** — DELEGATED to BEA by treaty. URL: `https://legimonaco.mc/projet/955/` http 200 — "La réalisation de l'enquête technique peut être déléguée à un État tiers … a d'ailleurs fait l'objet d'un accord entre la Principauté de Monaco et la République Française en date du 24 janvier 1991." BEA search facet shows "Monaco (2)" → documents 2.

### DELEGATED — Dutch Safety Board (`sources/ovv` exists)

Kingdom Act, `https://onderzoeksraad.nl/wp-content/uploads/2023/11/kingdom_act_dutch_safety_board.pdf` http 200: the Board may investigate "occurrences on, above or under the territory of Aruba, Curaçao or Sint Maarten … if the Board is requested by the government of Aruba, Curaçao or Sint Maarten, respectively" → **AW, CW, SX** conditional delegation, documents 0. **BQ**: `https://onderzoeksraad.nl/en/update/interim-statement-britten-norman-bn-2b-20-islander/` http 200 — "investigation by the Dutch Safety Board into the accident involving a Britten-Norman BN-2B-20 Islander on Juancho E. Yrausquin Airport, Saba, Caribbean Netherlands on 13 February 2023"; documents 1 (HTML). Note: onderzoeksraad.nl returns 403 to WebFetch; curl with a browser UA works.

### DELEGATED — NTSB (`sources/ntsbcarol` exists)

**AS, GU, MP, PR, VI** — `https://www.ntsb.gov/Pages/AviationQueryv2.aspx` http 200: "contains civil aviation accidents and selected incidents that occurred from 1962 to present within the United States, its territories and possessions"; the State dropdown lists AS, GU, MP, PR, VI. Per-territory counts not obtained — the CAROL query API (`https://data.ntsb.gov/carol-main-public/api/Query/Main`) returned 500 on a State filter; documents 0 counted. The existing CAROL scraper should already hold them if it stores `Event.State`.

### DELEGATED — other

**FO Faroe Islands, GL Greenland** — Accident Investigation Board Denmark (Havarikommissionen, HCLJ; `sources/aibdk` exists). `https://en.havarikommissionen.dk/aviation` http 200: "The investigation areas of responsibility of the Aviation Unit are Denmark, Greenland, and the Faroe Islands." The report search (`https://en.havarikommissionen.dk/investigation-results/search-aviation`, 200) is a Blazor app — its location list includes Grønland, Nuuk (BGGH), Narsarsuaq (BGBW), Kangerlussuaq (BGSF), Ilulissat and "Vagar, Fareo Is.", but results render client-side, so documents 0 counted.

**LI Liechtenstein** — Swiss Transportation Safety Investigation Board (STSB/SUST; `sources/sust` exists). `https://www.sust.admin.ch/dam/de/sd-web/pDxHYrhtpx9h/Verwaltungsvereinbarung_FL-SUST.pdf` http 200 (linked from `https://www.sust.admin.ch/de/die-sust-rechtliche-grundlagen-und-vereinbarungen`, 200): "Die SUST ist die für Liechtenstein zuständige Untersuchungsstelle…", in force 1 June 2024. Documents 0.

**SM San Marino** — ANSV Italy (`sources/ansv` exists). `https://www.smar.aero/wp-content/uploads/2021/12/SSP-SM-Rev-07.pdf` http 200: "San Marino delegates the function of Accident and Incident Investigation to the Italian aircraft accident investigation agency … ANSV … through a Memorandum of Understanding". ANSV news page (`https://ansv.it/lansv-continuera-a-svolgere-le-inchieste-sugli-eventi-aeronautici-della-repubblica-di-san-marino/`) confirms renewal 30 Sept 2024 (WebFetch returned content; curl to ansv.it failed at TLS, code 000). Documents 0.

### UNRESOLVED

**ER Eritrea** — no official CAA website found; searches return Ethiopia. No URL fetched; documents 0.

**LS Lesotho** — `https://www.gov.ls/directory/civil-aviation-lesotho/` http 200 gives only contacts for the Department of Civil Aviation; its own site `civilair.org.ls` does not resolve (000). No investigation body identified.

**MU Mauritius** — Department of Civil Aviation site `https://civil-aviation.govmu.org/` http 200 (the `civilaviation.` host does not resolve, 000) has no safety/investigation section; investigation authority not identified.

**NU Niue** — no Niue authority page found; the NZ CAA agreements page (`https://www.aviation.govt.nz/about-us/who-we-work-with/international-agreements-and-arrangements/`, 200) contains no "Niue" text server-side. Not determined.

**PS Palestine** — Ministry of Transport page for the Civil Aviation Authority (`https://www.mot.gov.ps/en/...`) refused connection (000); Gaza airport closed since 2001. Not determined.

## Worth a scraper first

1. **HK — AAIA** (78 reports, one static page, PDFs, no pagination). Largest uncovered own archive; trivially scrapable.
2. **GY — GCAA AAID** (29 PDFs on one static page). Small but clean; Caribbean/South America coverage gap.
3. **BZ, SZ, ST** (8 / 8 / 7 PDFs each, single static pages). Cheap; batch them as one "small-static-list" scraper family. ST's `/teste/` path may move — pin it.
4. **Delegated territories need no new scrapers, only tagging**: AAIB, BEA, NTSB CAROL, OVV, AIB-DK, STSB, ANSV scrapers already exist. The actionable work is a location→ISO2 mapping in those pipelines (e.g. AAIB "Guernsey"/"Isle of Man"/"Jersey" ≈ 80 reports; BEA "France (métropole et outre-mer)" needs a lieu-based split; CAROL `Event.State` ∈ {PR, VI, GU, AS, MP}).
5. Do not spend time on KM/DJ/GW/SS/CK/SC/MO — nothing to scrape live; SC and MO are already handled (0 by design / Wayback).
