#!/usr/bin/env python3
"""beasn — Senegal BEA aviation-accident ingest.

Source: bea.sn (Bureau d'Enquête et d'Analyse pour la Sécurité de l'Aviation civile, Sénégal).
The site's dynamic pages (SPIP CMS article/* listing) have a SQL server error.
But PDFs are accessible directly at bea.sn/IMG/pdf/<filename>.

Reports discovered from:
1. Archived SPIP article pages (Wayback): articles 44-47 showed direct PDF links
2. CDX scan: found one new 2025 interim declaration
3. Articles 39, 40, 42 are brief HTML summaries (pre-BEA-Sénégal era, 2000-2005) —
   too short to build narratives from

BEA Sénégal is operational since September 2015. All reports are in French.

offset: 121_000_000_000
source code: beasn
countryIso: SN
lang: fr
"""

import sys, os, re, time, sqlite3, subprocess, shlex, uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HOME   = Path(os.path.expanduser("~/beasn-ingest"))
DB     = str(HOME / "beasn.db")
PDFDIR = HOME / "pdfs"
FLOOR  = 600   # min chars narrative

COUNTRY = "SN"
LANG    = "fr"
LIVE_BASE = "https://bea.sn"
OCR_LANG  = "fra"
DELAY     = 2.0

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# ---------------------------------------------------------------------------
# REPORTS seed - derived from site recon 2026-06-22
# All PDFs are accessible directly at bea.sn/IMG/pdf/<filename>
# ---------------------------------------------------------------------------
REPORTS = [
    # article47: ULM Autogire MTO Sport 8.4-S FJAGM, 06 Feb 2022, Fatick region
    {
        "case_id": "beasn-FJAGM-2022-02-06",
        "year": "2022",
        "report_type": "Final report",
        "aircraft": "ULM Autogire MTO Sport 8.4-S",
        "registration": "FJAGM",
        "pdf_filename": "rapport_sur_l_accident_de_l_ulm_autogire_de_type_mto.pdf",
        "spip_article": "47",
        "event_date": "2022-02-06",
        "location": "2 kilomètres de Keur Martin, région de Fatick, Sénégal",
        "description": "Accident de l'ULM Autogire de type MTO Sport 8.4-S immatriculé FJAGM du constructeur Autogyro GmbH survenu le 06 février 2022 à 2 kilomètres de Keur Martin région de Fatick. Rapport final, mars 2023.",
    },
    # article46: Embraer 120 RT 6V-AIP, Transair, GTS 513, 17 May 2019
    {
        "case_id": "beasn-6V-AIP-2019-05-17",
        "year": "2019",
        "report_type": "Final report",
        "aircraft": "Embraer 120 RT",
        "registration": "6V-AIP",
        "pdf_filename": "rapport_sur_l_accident_de_transair.pdf",
        "spip_article": "46",
        "event_date": "2019-05-17",
        "location": "Aéroport International Blaise Diagne, Dakar, Sénégal",
        "description": "Incident de l'Embraer 120 RT immatriculé 6V-AIP exploité par Transair survenu en phase montée du vol GTS 513 au départ de l'Aéroport International Blaise Diagne à destination de Ziguinchor le 17 mai 2019. Rapport final, décembre 2020.",
    },
    # article45: Helicopter Scott-Bell 47G-3B-2A 6V-AJI, CASL, 09 Aug 2019
    {
        "case_id": "beasn-6V-AJI-2019-08-09",
        "year": "2019",
        "report_type": "Final report",
        "aircraft": "Scott-Bell 47G-3B-2A",
        "registration": "6V-AJI",
        "pdf_filename": "accident_de_l_helicoptere_de_type_sott-bell.pdf",
        "spip_article": "45",
        "event_date": "2019-08-09",
        "location": "Champ agricole à Saint-Louis du Sénégal",
        "description": "Accident de l'Hélicoptère de Type Scott-Bell 47G-3B-2A exploité par la Compagnie Agricole de Saint-Louis du Sénégal (CASL), immatriculé 6V-AJI dans un champ agricole à Saint-Louis du Sénégal le 09 août 2019. Rapport final, septembre 2020.",
    },
    # article44: Boeing 787-900 CN-RGX, Royal Air Maroc, AIBD, 21 Aug 2019
    {
        "case_id": "beasn-CN-RGX-2019-08-21",
        "year": "2019",
        "report_type": "Final report",
        "aircraft": "Boeing 787-900",
        "registration": "CN-RGX",
        "pdf_filename": "rapport_final_de_l_incident_de_la_ram.pdf",
        "spip_article": "44",
        "event_date": "2019-08-21",
        "location": "Aire de trafic de l'AIBD, Dakar, Sénégal",
        "description": "Heurt de la surface supérieure de l'entrée d'air du moteur d'un Boeing 787-900 de la Royal Air Maroc immatriculé CN-RGX par une passerelle télescopique sur l'aire de trafic de l'AIBD le 21 août 2019. Rapport final, juin 2020.",
    },
    # 2025 interim: 6V-AJE, 09 May 2024 accident (interim declaration 2025)
    {
        "case_id": "beasn-6V-AJE-2024-05-09",
        "year": "2024",
        "report_type": "Interim declaration",
        "aircraft": None,
        "registration": "6V-AJE",
        "pdf_filename": "de_claration_interme_diaire___accid_6v-aje_09_mai_2025_version_finale_16_mai_2025.pdf",
        "spip_article": None,
        "event_date": "2024-05-09",
        "location": "Sénégal",
        "description": "Déclaration Intermédiaire concernant l'accident de l'aéronef immatriculé 6V-AJE survenu le 09 mai 2024 (ACCID_6V-AJE-09 mai 2024). Déclaration intermédiaire publiée le 16 mai 2025.",
    },
]

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS beasn_reports (
  case_id        TEXT PRIMARY KEY,
  year           TEXT,
  report_type    TEXT,
  aircraft       TEXT,
  registration   TEXT,
  description    TEXT,
  pdf_filename   TEXT,
  spip_article   TEXT,
  event_date     TEXT,
  location       TEXT,
  pdf_path       TEXT,
  narrative_text TEXT,
  source_tier    TEXT,
  lang           TEXT DEFAULT 'fr',
  status         TEXT DEFAULT 'new',
  discovered_at  INTEGER,
  updated_at     INTEGER
);
CREATE TABLE IF NOT EXISTS beasn_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'SN',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'fr',
  built_at       INTEGER
);
"""

def now_ms():
    return int(time.time() * 1000)

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c

# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------
def _ocr_remote(pdf_path, lang, host):
    remote = "/tmp/ocr-%s.pdf" % uuid.uuid4().hex
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), "%s:%s" % (host, remote)],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            return ""
        cmd = (
            'f=$(mktemp); '
            'nice -n 19 ionice -c3 ocrmypdf --force-ocr --language %s '
            '--sidecar "$f" --output-type none %s - >/dev/null 2>&1; '
            'cat "$f"; rm -f "$f" %s'
        ) % (shlex.quote(lang), shlex.quote(remote), shlex.quote(remote))
        run = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=900)
        return run.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        print("[beasn ocr] error: %s" % e, flush=True)
        return ""


def ocr_extract(pdf_path, lang=OCR_LANG):
    if not pdf_path or not Path(pdf_path).exists():
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, lang, host)
    try:
        tmp = Path("/tmp") / ("ocr-%s.txt" % uuid.uuid4().hex)
        subprocess.run(
            ["ocrmypdf", "--force-ocr", "--language", lang,
             "--sidecar", str(tmp), "--output-type", "none",
             str(pdf_path), "-"],
            capture_output=True, timeout=600,
        )
        if tmp.exists():
            t = tmp.read_text("utf-8", "replace").strip()
            tmp.unlink(missing_ok=True)
            return t
    except Exception:
        pass
    return ""


def extract_text(pdf_path):
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(pdf_path), "-"],
            capture_output=True, timeout=60,
        )
        return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Stage: fetch
# ---------------------------------------------------------------------------
def fetch(c):
    """Download PDFs from live bea.sn/IMG/pdf/ (direct access works)."""
    import httpx
    rows = c.execute(
        "SELECT case_id, pdf_filename FROM beasn_reports WHERE status='new'"
    ).fetchall()
    fetched = 0
    client = httpx.Client(headers={"User-Agent": UA}, timeout=120.0, follow_redirects=True)
    PDFDIR.mkdir(parents=True, exist_ok=True)
    try:
        for row in rows:
            cid = row["case_id"]
            fn = row["pdf_filename"]
            pdf_path = PDFDIR / ("%s.pdf" % cid)
            url = "%s/IMG/pdf/%s" % (LIVE_BASE, fn)
            print("[beasn fetch] %s -> %s" % (cid, url), flush=True)
            try:
                r = client.get(url)
                if r.status_code != 200:
                    print("[beasn fetch] HTTP %d for %s" % (r.status_code, cid), flush=True)
                    c.execute(
                        "UPDATE beasn_reports SET status='fetch_error', updated_at=? WHERE case_id=?",
                        (now_ms(), cid)
                    )
                    c.commit()
                    time.sleep(DELAY)
                    continue
                pdf_path.write_bytes(r.content)
                print("[beasn fetch] OK %s (%d bytes)" % (cid, len(r.content)), flush=True)
                c.execute(
                    "UPDATE beasn_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                    (str(pdf_path), now_ms(), cid)
                )
                c.commit()
                fetched += 1
                time.sleep(DELAY)
            except Exception as e:
                print("[beasn fetch] error %s: %s" % (cid, e), flush=True)
                c.execute(
                    "UPDATE beasn_reports SET status='fetch_error', updated_at=? WHERE case_id=?",
                    (now_ms(), cid)
                )
                c.commit()
                time.sleep(DELAY)
    finally:
        client.close()
    return fetched


# ---------------------------------------------------------------------------
# Stage: parse
# ---------------------------------------------------------------------------
def parse(c):
    rows = c.execute(
        "SELECT case_id, pdf_path FROM beasn_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        if not pdf_path or not Path(pdf_path).exists():
            print("[beasn parse] PDF missing for %s" % cid, flush=True)
            continue

        txt = extract_text(pdf_path)
        tier = "pdf"
        if len(txt) < FLOOR:
            print("[beasn parse] pdftotext short (%d chars), OCR-ing %s" % (len(txt), cid), flush=True)
            txt = ocr_extract(pdf_path, OCR_LANG)
            tier = "ocr"

        if len(txt) < FLOOR:
            print("[beasn parse] still below floor (%d chars) for %s" % (len(txt), cid), flush=True)
            c.execute(
                "UPDATE beasn_reports SET narrative_text=?, source_tier='none', status='skipped', updated_at=? WHERE case_id=?",
                ("", now_ms(), cid)
            )
            c.commit()
            continue

        print("[beasn parse] OK %s: tier=%s chars=%d" % (cid, tier, len(txt)), flush=True)
        c.execute(
            "UPDATE beasn_reports SET narrative_text=?, source_tier=?, status='parsed', updated_at=? WHERE case_id=?",
            (txt, tier, now_ms(), cid)
        )
        c.commit()
        parsed += 1
    return parsed


# ---------------------------------------------------------------------------
# Stage: build
# ---------------------------------------------------------------------------
def site_slug(case_id, aircraft, registration, location):
    parts = []
    for s in [aircraft, registration, location]:
        if s:
            parts.append(re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-'))
    parts.append(re.sub(r'[^a-z0-9]+', '-', case_id.lower()).strip('-'))
    return '-'.join(p for p in parts if p)[:120]


def build(c):
    rows = c.execute(
        "SELECT case_id, year, report_type, aircraft, registration, description, "
        "event_date, location, pdf_filename, narrative_text, source_tier, lang "
        "FROM beasn_reports WHERE status='parsed'"
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE beasn_reports SET status='skipped', updated_at=? WHERE case_id=?",
                (now_ms(), r["case_id"])
            )
            c.commit()
            continue

        source_url = "%s/IMG/pdf/%s" % (LIVE_BASE, r["pdf_filename"])
        slug = site_slug(r["case_id"], r["aircraft"], r["registration"], r["location"])

        c.execute(
            "INSERT OR REPLACE INTO beasn_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, lang, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["case_id"],
                r["event_date"],
                r["aircraft"],
                r["registration"],
                None,
                r["location"] or "Sénégal",
                COUNTRY,
                narr,
                None,
                source_url,
                r["report_type"] or "Final report",
                slug,
                r["lang"] or LANG,
                now_ms(),
            )
        )
        c.execute(
            "UPDATE beasn_reports SET status='built', updated_at=? WHERE case_id=?",
            (now_ms(), r["case_id"])
        )
        c.commit()
        built += 1
    return built


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------
def seed(c):
    added = 0
    for rpt in REPORTS:
        try:
            c.execute(
                "INSERT OR IGNORE INTO beasn_reports "
                "(case_id, year, report_type, aircraft, registration, description, "
                "pdf_filename, spip_article, event_date, location, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rpt["case_id"], rpt["year"], rpt["report_type"],
                    rpt["aircraft"], rpt.get("registration"),
                    rpt["description"], rpt["pdf_filename"], rpt.get("spip_article"),
                    rpt["event_date"], rpt["location"],
                    "new", now_ms(), now_ms(),
                )
            )
            if c.execute("SELECT changes()").fetchone()[0] > 0:
                added += 1
        except Exception as e:
            print("[beasn seed] error for %s: %s" % (rpt["case_id"], e), flush=True)
    c.commit()
    return added


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    PDFDIR.mkdir(parents=True, exist_ok=True)
    c = conn()

    if mode in ("seed", "all"):
        n = seed(c)
        print("seeded: %d new" % n, flush=True)

    if mode in ("fetch", "all"):
        n = fetch(c)
        print("fetched: %d" % n, flush=True)

    if mode in ("parse", "all"):
        n = parse(c)
        print("parsed: %d" % n, flush=True)

    if mode in ("build", "all"):
        n = build(c)
        print("built: %d" % n, flush=True)

    rows = c.execute(
        "SELECT status, COUNT(*) as n FROM beasn_reports GROUP BY status ORDER BY n DESC"
    ).fetchall()
    print("reports:", [(r["status"], r["n"]) for r in rows], flush=True)
    n_acc = c.execute("SELECT COUNT(*) FROM beasn_accidents").fetchone()[0]
    print("accidents:", n_acc, flush=True)


if __name__ == "__main__":
    main()
