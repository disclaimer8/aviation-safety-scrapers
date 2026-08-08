#!/usr/bin/env python3
"""caoiri — Iran CAO Aircraft Accident Investigation Board (AAIB)
Country: IR, Language: EN (commercial) / FA (GA/training)

Live site: aig.cao.ir — BLOCKED for all non-IR IPs (WAF IP/geo ban, Server Code 6980)
           www.cao.ir — same block (returns 403 from Mac/minipc/hetzner/FlareSolverr)
Strategy: Wayback Machine archived PDFs only. DO NOT probe cao.ir or aig.cao.ir directly.

Source of truth: https://www.cao.ir/web/accidents/reports (now blocked, archived 2017-2021)
March 2020 snapshot shows 20 reports (2018-11-16 to 2020-02-10) on page 1.
Additional PDFs from 2021 snapshots cover 2020-2021 events.
Older reports: 2009 IL-62M Mashhad, 2015 B747-300 EP-MNE via ICAO mirror.

case_id: 'caoiri-' + state_file_number (e.g. 'caoiri-A971228EPIDG')
         fallback: 'caoiri-' + registration.lower().replace('/', '-')

Probe matrix (all vantages tested 2026-06-10):
  Mac (residential-ish): aig.cao.ir → 403 "blocked from your IP or your location" (Server Code 6980)
  minipc (home):         aig.cao.ir → 403 (same WAF)
  hetzner (DE DC):       aig.cao.ir → 403 (same WAF) / caa.gov.ir → ArvanCloud reCAPTCHA
  FlareSolverr (hetzner):aig.cao.ir → 403 (IP-level block, not JS challenge — cannot bypass)
  Wayback CDX:           34 substantial PDFs archived from www.cao.ir/web/accidents/reports

Language mix: English for commercial aviation (airlines, jets), Farsi for GA/ultralight.
All PDFs are text-based (not scanned) — no OCR needed. PS752 (39MB) is copy-protected
(AES encrypted, copy=no) — stored with metadata only, narrative left NULL.

Politeness: 3s base delay + exponential backoff on 429/503 (Wayback can throttle).
OCR_REMOTE: not needed for this source (all text PDFs). tesseract-ocr-fas not
            installed on hetzner by default (apt-get install tesseract-ocr-fas available
            if Farsi text ever needs fallback OCR).
"""

import sys, os, re, time, sqlite3, subprocess, json, hashlib

HOME = os.path.expanduser("~/caoiran-ingest")
DB = os.path.join(HOME, "caoiri.db")
PDFDIR = os.path.join(HOME, "pdfs")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

DELAY = 3.0
NARRATIVE_FLOOR = 300

# ── Farsi script Unicode ranges (to detect FA reports) ─────────────────────
_FA_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

# ── State file number pattern: A + Persian/Gregorian year + month + day + REG ──
_SFNUM_RE = re.compile(
    r"\b([A-Z]\d{8,12}[A-Z0-9]+)\b"  # e.g. A971228EPIDG, A13940723EPMNE
)
# ── Registration: Iranian EP-XXX or foreign ──
_REG_RE = re.compile(
    r"\b(EP-[A-Z0-9]{3,4}|[A-Z]{1,2}-[A-Z0-9]{3,4}|UP-[A-Z][0-9]{4}|"
    r"[A-Z]-[A-Z]{4}|UR-[A-Z]{3}|N\d{3,5}[A-Z]{0,2})\b"
)
# ── Date: various ISO/dot/dash patterns ──
_DATE_RE = re.compile(
    r"\b(20[0-9]{2})[.\-/]([01]?[0-9])[.\-/]([0-3]?[0-9])\b"
    r"|"
    r"\b([0-3]?[0-9])\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
    r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)[,\.]?\s+(20[0-9]{2})\b",
    re.IGNORECASE,
)
_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ── Known Wayback PDFs from www.cao.ir/web/accidents/reports ─────────────────
# All confirmed accessible via Wayback 2026-06-10. Sizes in bytes.
# DO NOT probe www.cao.ir directly — all return 403 for non-IR IPs.
KNOWN_PDFS = [
    {
        "wb_url": "https://web.archive.org/web/20171114043412/http://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=S0lzdHo2dTNvZm4zM0hFZmlOOEFPWjVCQXZKSzl2RGtZVlBDNnVFV0d2emM0Qm95SUkrTHNXUktxQlBHNjdjR2ZCMkpDc1BjVGVIZwp4WkR1ZzE2bVdRPT0=.pdf",
        "approx_size": 4993609, "notes": "2009-07-24 IL-62M UP-I6208 Mashhad - ENGLISH confirmed",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184155/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=NDRpMHR0L2UzOUJFcTlzYThwdzhWSjVCQXZKSzl2RGtZVlBDNnVFV0d2eTBTZmR6aldINVVaVE1wUktiY0ovVFFGRk1acWFHb2ZhNQowTndNdExiV293PT0=.pdf",
        "approx_size": 5507426, "notes": "2019-03-19 F28 EP-IDG Gear Up Mehrabad - ENGLISH confirmed",
    },
    {
        # ICAO mirror - accessible from hetzner, not from minipc (403). Fetch via hetzner manually if needed.
        # "wb_url": "https://www.icao.int/sites/default/files/safety/airnavigation/AIG/Documents/Safety-Recommendations-to-ICAO/Final-Reports/IRN20151015_final_report.pdf",
        # Use Wayback archived copy instead
        "wb_url": "https://web.archive.org/web/20251119062349/https://www.icao.int/sites/default/files/safety/airnavigation/AIG/Documents/Safety-Recommendations-to-ICAO/Final-Reports/IRN20151015_final_report.pdf",
        "approx_size": 5329293, "notes": "2015-10-15 B747-300 EP-MNE Mahan Air Mehrabad - ENGLISH (ICAO via Wayback)",
    },
    {
        "wb_url": "https://web.archive.org/web/20190306044621/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=TTRJZmhRNWErN0s1NkUybUlrMWZYWjVCQXZKSzl2RGtZVlBDNnVFV0d2ejlCNEgzNlZDVGdGZ05CSkZzODNXMndSK2d0UFdRSFRrVwpnSnlzRkpxaDh3PT0=.pdf",
        "approx_size": 6845780, "notes": "Large report ~2018",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184206/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=YWo3a0EzekNBN01zUi9hKzdVYlVMSjVCQXZKSzl2RGtZVlBDNnVFV0d2eXd5Tk8reklJby8vNmFkV0U1THkrdSs4Smg4bDQzTmpPMApjZXhMbzJQdEpRPT0=.pdf",
        "approx_size": 4017835, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184251/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=by83cWswWVU1NGNab3pZTzA2TUtuNTVCQXZKSzl2RGtZVlBDNnVFV0d2eUl0ZHU5R0loOGtOWDFhVlV4RUVTU01sS25DSG9MNU5Ndwprek1TUUF6THd3PT0=.pdf",
        "approx_size": 569540, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184236/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=cTVoNHNhNGM5eHRtUyt2UmNmTStDWjVCQXZKSzl2RGtZVlBDNnVFV0d2ejJaZnczNVZJTEVRcndlOXJBOEdJRTloT2R2L3h4NUNZcwpIOUZjR2NwS3pRPT0=.pdf",
        "approx_size": 2750691, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184200/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=Rkx4Z3FPVDlSaGZMWTlzbGJ0NUw2cDVCQXZKSzl2RGtZVlBDNnVFV0d2eHBjaHBkYlBsSWMyWGFkaUZWTzZ3bVhDaEpaelZCMW43dQpoVkN4MHdIdG9RPT0=.pdf",
        "approx_size": 1294235, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184158/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=YkRpZXJuL2R4YlhRQjBjNk5FUTNHWjVCQXZKSzl2RGtZVlBDNnVFV0d2eHUwTXdEMTlab0xnL1Y4QXN2VVAxK2JNWmdLQ1B2M0ZsSAo0UG5BeGZmenNRPT0=.pdf",
        "approx_size": 1213881, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184231/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=RFpLRk5zMTJjRlI1L0NyVEZzbEMvNTVCQXZKSzl2RGtZVlBDNnVFV0d2emNac2FNODVBcy9JbW1OMy9iblpyN2x0eVFyaUhiTTZqNApBMGNJWURVc0hBPT0=.pdf",
        "approx_size": 1399845, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184208/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=QjNNSzBKZ1hnM2hZOUYzaEhaeks3SjVCQXZKSzl2RGtZVlBDNnVFV0d2d2s2UGZaamkzUnlJY1JjQjJadHNsSW5QTW9HVjdSSmtWZApLWUg4QzE3alVnPT0=.pdf",
        "approx_size": 549875, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20190923123642/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=QjNNSzBKZ1hnM2hZOUYzaEhaeks3SjVCQXZKSzl2RGtZVlBDNnVFV0d2eXFzMlAyQmRtNWVNM1p4RUhFZmdIK0oxOFQwbWpxUVV0egpFTWlWK3VJbC9BPT0=.pdf",
        "approx_size": 300790, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184153/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=U1hLWk1zVG5MaEUrUnYvb1l3UFQ2NTVCQXZKSzl2RGtZVlBDNnVFV0d2eEhFQmUvRlo4cDZEU2gzZFdraGVHWktoQTNoSzg4M0ljMwo4ZzlDUjl0SWJBPT0=.pdf",
        "approx_size": 384465, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184240/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=NDJHMWU3d0VuK3ArVThLQ2c3TjZXcDVCQXZKSzl2RGtZVlBDNnVFV0d2ek1uaTVXNGwrUjgvUXI2YVIwSGJMTUlWSWFvYWR0aDBUMgo1eEJvbEhGYzh3PT0=.pdf",
        "approx_size": 388686, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184245/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=cUN0bzE4NXNHK09vcjc5dTJlTWpZNTVCQXZKSzl2RGtZVlBDNnVFV0d2d0lER0RrSUtBN1hqTFNqRmVHNFhpVkJxUHZNV1huMEZpKwo5dlZOeExFYkpBPT0=.pdf",
        "approx_size": 258913, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184221/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=dExwYzZsK2NMVDdVZzN4QW41SG1KSjVCQXZKSzl2RGtZVlBDNnVFV0d2ek8xOWxqc0xNSEl3QXZqOC8zWEx3Vmx0QWJqU3BXNWZmdwpTMlkybUh0UnpBPT0=.pdf",
        "approx_size": 238524, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184216/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=Wm1yaWFIYTFOWnNEd2hWRWE5UUlhNTVCQXZKSzl2RGtZVlBDNnVFV0d2d2pHV0Vsb2N2Ti9sZTdFc1Y3Q2U0RjliRXQ1SUZXeXNSUApkblA2MXJUTWNnPT0=.pdf",
        "approx_size": 255825, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184234/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=UUR1YjdtVzlVNUY3Ty9aVDZoWU4xSjVCQXZKSzl2RGtZVlBDNnVFV0d2eE1xb3loc2JwQWtLczBDbC9ZRWdkc2JvN3NuYmsxYVkrQgpneGZSYTZQUTdnPT0=.pdf",
        "approx_size": 742011, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184259/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=UWxkTTZYaklhUHNCTmZuRlR3b3I0SjVCQXZKSzl2RGtZVlBDNnVFV0d2emF5NVdRVElubDEyOVV4NXZRR0FoV1p3UVMyOHBTQlhNYgpvWDJqRWM0TkVBPT0=.pdf",
        "approx_size": 237813, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184304/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=VUR1dzNTY0w1a3RKblA1eWhabURWSjVCQXZKSzl2RGtZVlBDNnVFV0d2eGZpVW9FaUYrUkt4TkhlUGU0ZHlQSlFYTmMwNnNrTWxpUQpkNzl0eGpNSDJ3PT0=.pdf",
        "approx_size": 262997, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184307/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=VXZFK2NYR2k2bTA5V0Z5OFp0ZEVOWjVCQXZKSzl2RGtZVlBDNnVFV0d2d3dXYWhVME1EUU9IT1NobU5HTUZGYlBldkg4SDA1dVZNMwo1c1ZnVktZc2V3PT0=.pdf",
        "approx_size": 512295, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184248/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=RmJmdVZxd3hWaGVYTit4V1hlcEZaNTVCQXZKSzl2RGtZVlBDNnVFV0d2eld1QjU4L1RmbjRCTU1xdldmaGJMRTBJSlVEUUVZUUtYMApQZWdhdy94S0pRPT0=.pdf",
        "approx_size": 444820, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184245/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=T2grclNYUDdiZlVNb1lnSHk2OENOWjVCQXZKSzl2RGtZVlBDNnVFV0d2endZd2FmV2EzRmtjUlo2Vm9ZNFc3QzNpVUQ3Y0haRUN0QQp6d1FmSmNheXhnPT0=.pdf",
        "approx_size": 183260, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20200325184230/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=Rk5FeDNtTjJYSTBZV3lFSnZERDNRSjVCQXZKSzl2RGtZVlBDNnVFV0d2d3Jaek1HK3FSY1ptTUF5KzdsUjluQnJGcFNiWGZiOEdNLwo3SFhIRk1zUm9RPT0=.pdf",
        "approx_size": 80579, "notes": "",
    },
    # 2021 PDFs (newer reports)
    {
        "wb_url": "https://web.archive.org/web/20210222154956/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=RFpLRk5zMTJjRlI1L0NyVEZzbEMvNTVCQXZKSzl2RGtZVlBDNnVFV0d2elFkYkk2amZzOHQxdEdsTDBlTmllNkYwZ0c5ZUVVN3VuRwp2bGQwTlJQd29BPT0=.pdf",
        "approx_size": 777166, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20210222154958/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=RFpLRk5zMTJjRlI1L0NyVEZzbEMvNTVCQXZKSzl2RGtZVlBDNnVFV0d2emw4Y3dzUzJvQkl3REtlcExyN1BOUGdMS0JaZTZnU0M1MwpkRVZmUlZNQU5nPT0=.pdf",
        "approx_size": 837740, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20210209052544/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=RFpLRk5zMTJjRlI1L0NyVEZzbEMvNTVCQXZKSzl2RGtZVlBDNnVFV0d2emFYWllPdVo3aVpEMW9ad0R3SWhxdWdMSzlaUjlERjRWRApmK1JHclQrOXVnPT0=.pdf",
        "approx_size": 553705, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20210317140234/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=RFpLRk5zMTJjRlI1L0NyVEZzbEMvNTVCQXZKSzl2RGtZVlBDNnVFV0d2eUcwc08zbTZtdVlWSWxPanBMdTF6b1FUNUhJamZtekxMTAozbzUvYm9zMUxRPT0=.pdf",
        "approx_size": 15546832, "notes": "Large report 15MB",
    },
    {
        "wb_url": "https://web.archive.org/web/20210624181020/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=RFpLRk5zMTJjRlI1L0NyVEZzbEMvNTVCQXZKSzl2RGtZVlBDNnVFV0d2eWErSzVqQlh5OTV1bWFnc1JRd0JpWGdQd21LM04rWGNldApTbnM2UGZXcGl3PT0=.pdf",
        "approx_size": 3196588, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20211201070713/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=VFBPbHRheURmQ3Zld3NocDVnYVo1SjVCQXZKSzl2RGtZVlBDNnVFV0d2d1g0YlhMb2VKRGtab0kvM000SDgxVVh5MFF4N0IxUllrMQpta3l6VTlIQS93PT0=.pdf",
        "approx_size": 1784612, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20211009191021/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=TTRJZmhRNWErN0s1NkUybUlrMWZYWjVCQXZKSzl2RGtZVlBDNnVFV0d2eWFHcUZOKzlITkNiUzJRd056R3BLaVVTUWhvVFVvWmJTWgpPaDRiekRPTnp3PT0=.pdf",
        "approx_size": 9018934, "notes": "",
    },
    {
        "wb_url": "https://web.archive.org/web/20211010190332/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=TTRJZmhRNWErN0s1NkUybUlrMWZYWjVCQXZKSzl2RGtZVlBDNnVFV0d2elZSQzE1YnZ6a3NKYUdhVXRBVTFwRW9tdWhoeEg0TldxRwphaEVWNnVUWnVnPT0=.pdf",
        "approx_size": 1096305, "notes": "",
    },
    # PS752 - copy-protected AES-encrypted PDF; store metadata only
    {
        "wb_url": "https://web.archive.org/web/20210517145016/https://www.cao.ir/web/accidents/reports?p_p_id=NetFormGetFile_WAR_NetForm&p_p_lifecycle=2&p_p_resource_id=getFile&_NetFormGetFile_WAR_NetForm_file=cExUbENUa1o4bHQ5citoN3ZlUldnWVRxRmNTTlA4bHEyL1daY3RWUHNkcTFybFJaeHNYc0gwTSs0MEMzcjFjeTl3K1EwRVZ4MDk3NgpvTTJwUkxLRFpRPT0=.pdf",
        "approx_size": 39935546, "notes": "PS752 B737-800 UR-PSR 2020-01-08 copy-protected skip_text",
    },
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS caoiri_reports (
  case_id          TEXT PRIMARY KEY,
  state_file_num   TEXT,
  registration     TEXT,
  source_url       TEXT,
  archive_url      TEXT,
  archive_ts       TEXT,
  pdf_path         TEXT,
  event_date       TEXT,
  aircraft         TEXT,
  operator         TEXT,
  location         TEXT,
  narrative_text   TEXT,
  probable_cause   TEXT,
  lang             TEXT DEFAULT 'en',  -- 'en' or 'fa'
  report_type      TEXT,               -- Final / Preliminary
  occurrence_type  TEXT,               -- Accident / Incident
  status           TEXT DEFAULT 'new',
  skip_reason      TEXT,
  discovered_at    INTEGER,
  updated_at       INTEGER
);
CREATE TABLE IF NOT EXISTS caoiri_accidents (
  case_id          TEXT PRIMARY KEY,
  event_date       TEXT,
  aircraft         TEXT,
  registration     TEXT,
  operator         TEXT,
  location         TEXT,
  country          TEXT DEFAULT 'IR',
  narrative_text   TEXT,
  probable_cause   TEXT,
  source_url       TEXT,
  report_type      TEXT DEFAULT 'Final Report',
  site_slug        TEXT,
  lang             TEXT DEFAULT 'en',
  built_at         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_caoiri_status ON caoiri_reports(status);
CREATE INDEX IF NOT EXISTS idx_caoiri_date   ON caoiri_reports(event_date);
"""


def now_ms():
    return int(time.time() * 1000)


def db_conn():
    os.makedirs(HOME, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c


def http_client():
    import httpx
    return httpx.Client(
        headers={"User-Agent": UA},
        timeout=180.0,
        follow_redirects=True,
    )


def wayback_fetch(cl, url, retries=3):
    """Fetch from Wayback with polite delay and backoff."""
    delay = DELAY
    for attempt in range(retries):
        try:
            r = cl.get(url)
            if r.status_code in (429, 503):
                wait = 30 * (2 ** attempt)
                print(f"  [throttle] {r.status_code} — sleep {wait}s", flush=True)
                time.sleep(wait)
                continue
            time.sleep(delay)
            return r
        except Exception as e:
            print(f"  [error] {e}", flush=True)
            if attempt < retries - 1:
                time.sleep(30)
            else:
                raise
    return None


def pdf_id_from_url(url):
    """Stable short hash from URL for filename."""
    return hashlib.md5(url.encode()).hexdigest()[:12]


def pdftotext(path, max_pages=None):
    """Extract text from PDF. Returns (text, lang, is_copy_protected)."""
    cmd = ["pdftotext"]
    if max_pages:
        cmd += ["-l", str(max_pages)]
    cmd += [path, "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    text = r.stdout.strip()
    # Detect copy-protected
    if not text and r.returncode != 0:
        # Check if encrypted
        ri = subprocess.run(["pdfinfo", path], capture_output=True, text=True)
        if "copy:no" in ri.stdout:
            return "", "en", True
    # Detect language
    fa_chars = len(_FA_RE.findall(text))
    ascii_chars = sum(1 for c in text if ord(c) < 128 and c.isalpha())
    lang = "fa" if fa_chars > ascii_chars else "en"
    return text, lang, False


def extract_metadata(text, wb_url):
    """Extract case_id, registration, date, aircraft, operator, location from text."""
    # State file number
    sfm = _SFNUM_RE.search(text)
    state_file = sfm.group(1) if sfm else None

    # Registration
    regs = _REG_RE.findall(text)
    registration = regs[0] if regs else None

    # Aircraft model — look for common patterns after "Model:" or "Aircraft:"
    aircraft = None
    m = re.search(r"(?:Aircraft(?:\s+Type)?|Model)\s*[:\-]\s*([^\n\r]{3,40})", text, re.IGNORECASE)
    if m:
        aircraft = m.group(1).strip().split("\n")[0].strip()

    # Operator
    operator = None
    m = re.search(r"Operator\s*[:\-]\s*([^\n\r]{3,50})", text, re.IGNORECASE)
    if m:
        operator = m.group(1).strip()

    # Location
    location = None
    for pat in [
        r"Place\s+of\s+[Oo]ccurrence\s*[:\-]\s*([^\n\r]{5,80})",
        r"Location\s*[:\-]\s*([^\n\r]{5,80})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            location = m.group(1).strip()
            break

    # Date — handle: "Oct. 15th 2015", "24.Jul.2009", "19 Mar, 2019", "2020-01-08"
    event_date = None
    # Look for "Date of Occurrence:" label then the actual date on same or next line
    m = re.search(
        r"(?:Date\s+of\s+[Oo]ccurrence|Date)\s*[:\-]?\s*\n?\s*([^\n\r]{3,40})",
        text, re.IGNORECASE
    )
    if m:
        raw = m.group(1).strip()
        # Remove ordinal suffixes: 15th→15, 1st→1, 2nd→2, 3rd→3
        raw = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE)
        # Try YYYY-MM-DD
        dm = re.search(r"\b(20[012]\d)[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b", raw)
        if dm:
            event_date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
        # Try DD.Mon.YYYY or DD Mon YYYY or Mon DD, YYYY
        if not event_date:
            dm = re.search(
                r"\b(\d{1,2})[.\s,]+([A-Za-z]{3})[.\s,]+(\d{4})\b"
                r"|"
                r"\b([A-Za-z]{3})[\s.]+(\d{1,2})[,\s]+(\d{4})\b",
                raw
            )
            if dm:
                if dm.group(1):  # DD Mon YYYY
                    mo = _MONTH_MAP.get(dm.group(2)[:3].lower())
                    if mo:
                        event_date = f"{dm.group(3)}-{mo:02d}-{int(dm.group(1)):02d}"
                elif dm.group(4):  # Mon DD YYYY
                    mo = _MONTH_MAP.get(dm.group(4)[:3].lower())
                    if mo:
                        event_date = f"{dm.group(6)}-{mo:02d}-{int(dm.group(5)):02d}"

    # Report type
    report_type = "Final" if "Final" in text[:500] else "Preliminary"
    occurrence_type = "Accident" if "Accident" in text[:500] else "Incident"

    # case_id
    if state_file:
        case_id = "caoiri-" + state_file.lower()
    elif registration:
        case_id = "caoiri-" + registration.lower().replace("/", "-")
    else:
        # fallback from URL hash
        h = hashlib.md5(wb_url.encode()).hexdigest()[:8]
        case_id = f"caoiri-{h}"

    return {
        "case_id": case_id,
        "state_file_num": state_file,
        "registration": registration,
        "aircraft": aircraft,
        "operator": operator,
        "location": location,
        "event_date": event_date,
        "report_type": report_type,
        "occurrence_type": occurrence_type,
    }


def build_narrative(text, lang):
    """Clean and truncate narrative text."""
    # Remove header boilerplate (first ~10 lines often just org header)
    lines = text.split("\n")
    body_lines = []
    found_body = False
    for line in lines:
        stripped = line.strip()
        if not found_body:
            # Skip until we hit actual content
            if len(stripped) > 30 and not any(x in stripped for x in [
                "Civil Aviation Organization", "Aircraft Accident Investigation",
                "AAIB", "State File Number", "Type of Occurrence",
                "Date of Occurrence", "Date of Issue", "Registration",
                "Mehrabad International Airport", "Tel.:", "Fax:", "http://",
            ]):
                found_body = True
        if found_body:
            body_lines.append(line)
    narrative = "\n".join(body_lines).strip()
    if len(narrative) < NARRATIVE_FLOOR:
        # Fall back to raw text if body extraction failed
        narrative = text.strip()
    return narrative


def cmd_fetch(args):
    """Download and process all known PDFs from Wayback."""
    c = db_conn()
    cl = http_client()
    os.makedirs(PDFDIR, exist_ok=True)

    total = len(KNOWN_PDFS)
    ok = skip = err = 0

    for i, entry in enumerate(KNOWN_PDFS, 1):
        wb_url = entry["wb_url"]
        notes = entry["notes"]
        pdf_id = pdf_id_from_url(wb_url)
        pdf_path = os.path.join(PDFDIR, f"{pdf_id}.pdf")

        print(f"[{i}/{total}] {pdf_id} ({entry['approx_size']//1024}KB) {notes[:40]}", flush=True)

        # Already processed?
        existing = c.execute(
            "SELECT status FROM caoiri_reports WHERE pdf_path = ?", (pdf_path,)
        ).fetchone()
        if existing and existing["status"] in ("ok", "skip"):
            print(f"  → already {existing['status']}, skipping", flush=True)
            skip += 1
            continue

        # Download PDF
        if not os.path.exists(pdf_path):
            print(f"  downloading...", flush=True)
            try:
                r = wayback_fetch(cl, wb_url)
                if r is None or r.status_code != 200:
                    code = r.status_code if r else "None"
                    print(f"  [WARN] HTTP {code}, skipping", flush=True)
                    err += 1
                    continue
                with open(pdf_path, "wb") as f:
                    f.write(r.content)
                print(f"  saved {len(r.content)//1024}KB", flush=True)
            except Exception as e:
                print(f"  [ERROR] {e}", flush=True)
                err += 1
                continue
        else:
            print(f"  pdf already downloaded", flush=True)

        # Extract text
        is_ps752 = "copy-protected" in notes or "copy_protected" in notes or "skip_text" in notes
        if is_ps752:
            narrative = ""
            lang = "en"
            is_protected = True
            meta = {
                "case_id": "caoiri-ps752",
                "state_file_num": None,
                "registration": "UR-PSR",
                "aircraft": "Boeing 737-800",
                "operator": "Ukraine International Airlines",
                "location": "Tehran Imam Khomeini Airport",
                "event_date": "2020-01-08",
                "report_type": "Final",
                "occurrence_type": "Accident",
            }
        else:
            text, lang, is_protected = pdftotext(pdf_path)
            if is_protected:
                print(f"  [WARN] copy-protected PDF, no text", flush=True)
                narrative = ""
                meta = extract_metadata("", wb_url)
            elif len(text) < NARRATIVE_FLOOR:
                print(f"  [WARN] text too short ({len(text)} chars), skip", flush=True)
                c.execute("""
                    INSERT OR REPLACE INTO caoiri_reports
                    (case_id, source_url, archive_url, pdf_path, lang, status, skip_reason, discovered_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (
                    f"caoiri-{pdf_id}", f"https://www.cao.ir/web/accidents/reports",
                    wb_url, pdf_path, "?", "skip", f"text_too_short:{len(text)}",
                    now_ms(), now_ms()
                ))
                c.commit()
                skip += 1
                continue
            else:
                meta = extract_metadata(text, wb_url)
                narrative = build_narrative(text, lang)

        # Write to DB
        case_id = meta["case_id"]
        # Check collision
        existing_id = c.execute(
            "SELECT case_id FROM caoiri_reports WHERE case_id = ?", (case_id,)
        ).fetchone()
        if existing_id:
            case_id = f"{case_id}-{pdf_id[:4]}"
            meta["case_id"] = case_id

        c.execute("""
            INSERT OR REPLACE INTO caoiri_reports
            (case_id, state_file_num, registration, source_url, archive_url, archive_ts,
             pdf_path, event_date, aircraft, operator, location, narrative_text,
             lang, report_type, occurrence_type, status, skip_reason, discovered_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            case_id, meta["state_file_num"], meta["registration"],
            "https://www.cao.ir/web/accidents/reports",
            wb_url, pdf_id,
            pdf_path,
            meta["event_date"], meta["aircraft"], meta["operator"], meta["location"],
            narrative if not is_protected else None,
            lang, meta["report_type"], meta["occurrence_type"],
            "ok" if not is_protected else "ok_no_text",
            "copy_protected" if is_protected else None,
            now_ms(), now_ms(),
        ))
        c.commit()

        narr_len = len(narrative) if narrative else 0
        above_floor = narr_len >= NARRATIVE_FLOOR
        print(f"  → {case_id} [{lang}] narr={narr_len}ch {'OK' if above_floor else 'SHORT'}", flush=True)
        ok += 1

    print(f"\nDone: {ok} ok, {skip} skip, {err} error (of {total} total)", flush=True)


def cmd_build(args):
    """Build caoiri_accidents from caoiri_reports."""
    c = db_conn()
    rows = c.execute("""
        SELECT * FROM caoiri_reports
        WHERE status IN ('ok', 'ok_no_text')
          AND (narrative_text IS NOT NULL AND length(narrative_text) >= ?)
           OR (status = 'ok_no_text')
        ORDER BY event_date
    """, (NARRATIVE_FLOOR,)).fetchall()

    c.execute("DELETE FROM caoiri_accidents")
    built = 0
    for row in rows:
        if not row["narrative_text"] and row["status"] != "ok_no_text":
            continue
        # Build slug
        reg = (row["registration"] or "unknown").lower().replace("/", "-")
        date = (row["event_date"] or "undated").replace("-", "")[:8]
        site_slug = f"caoiri-{reg}-{date}"

        c.execute("""
            INSERT OR REPLACE INTO caoiri_accidents
            (case_id, event_date, aircraft, registration, operator, location,
             country, narrative_text, probable_cause, source_url, report_type,
             site_slug, lang, built_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row["case_id"], row["event_date"], row["aircraft"],
            row["registration"], row["operator"], row["location"],
            "IR", row["narrative_text"], None,
            row["source_url"], row["report_type"],
            site_slug, row["lang"], now_ms(),
        ))
        built += 1

    c.commit()
    print(f"Built {built} accidents in caoiri_accidents", flush=True)


def cmd_stats(args):
    """Print stats."""
    c = db_conn()
    total = c.execute("SELECT count(*) FROM caoiri_reports").fetchone()[0]
    ok = c.execute("SELECT count(*) FROM caoiri_reports WHERE status='ok'").fetchone()[0]
    ok_no_text = c.execute("SELECT count(*) FROM caoiri_reports WHERE status='ok_no_text'").fetchone()[0]
    skip = c.execute("SELECT count(*) FROM caoiri_reports WHERE status='skip'").fetchone()[0]
    en = c.execute("SELECT count(*) FROM caoiri_reports WHERE lang='en' AND status='ok'").fetchone()[0]
    fa = c.execute("SELECT count(*) FROM caoiri_reports WHERE lang='fa' AND status='ok'").fetchone()[0]
    above_floor = c.execute(
        f"SELECT count(*) FROM caoiri_reports WHERE status='ok' AND length(narrative_text) >= {NARRATIVE_FLOOR}"
    ).fetchone()[0]
    dated = c.execute(
        "SELECT count(*) FROM caoiri_reports WHERE status='ok' AND event_date IS NOT NULL"
    ).fetchone()[0]
    built = c.execute("SELECT count(*) FROM caoiri_accidents").fetchone()[0]

    print(f"caoiri reports:")
    print(f"  total processed : {total}")
    print(f"  ok (with text)  : {ok}")
    print(f"  ok (no text)    : {ok_no_text}")
    print(f"  skipped         : {skip}")
    print(f"  EN narratives   : {en}")
    print(f"  FA narratives   : {fa}")
    print(f"  above floor     : {above_floor}")
    print(f"  dated           : {dated}")
    print(f"  built accidents : {built}")

    # Sample
    samples = c.execute("""
        SELECT case_id, registration, event_date, lang, length(narrative_text) as nlen
        FROM caoiri_reports WHERE status='ok' ORDER BY event_date LIMIT 10
    """).fetchall()
    print("\nSample rows:")
    for s in samples:
        print(f"  {s['case_id']:35s} {s['registration'] or '?':12s} {s['event_date'] or '?':12s} [{s['lang']}] {s['nlen'] or 0}ch")


COMMANDS = {
    "fetch": cmd_fetch,
    "build": cmd_build,
    "stats": cmd_stats,
}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd not in COMMANDS:
        print(f"Usage: {sys.argv[0]} [fetch|build|stats]")
        sys.exit(1)
    COMMANDS[cmd](sys.argv[2:])
