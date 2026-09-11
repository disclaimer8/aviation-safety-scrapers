# ECCAA ingest — smoke notes

Source: Eastern Caribbean Civil Aviation Authority (6 OECS states).
Host: https://www.eccaa.aero  (Joomla). ENGLISH text-layer PDFs.

⚠️ RESIDENTIAL-VANTAGE-ONLY. The host returns 000 from the Mac and from
datacenter (hetzner/DE). Reachable ONLY from a residential IP (the mini-PC).
ALL live discover/fetch/smoke MUST run on the mini-PC (a1@the ingest host on the LAN).
Local Mac smoke = 000 and is EXPECTED — do not conclude the source is dead.

Listing page (AIG Reports):
  https://www.eccaa.aero/index.php?option=com_content&view=article&id=175&Itemid=90
PDFs under /images/stories/docs/far/ (directory listing is 403; links are in
the article HTML). 9 PDF links: 7 Final Accident Reports + 2 '_'-prefixed
(press release / preliminary) which are skipped.

⚠️ PDF hrefs contain spaces and parentheses; they MUST be percent-encoded
before the GET or curl/httpx returns 000.

case_id is INTRINSIC: registration + ISO event-date (e.g. J8-SXY-2010-08-05).
No encounter-order suffix.

Per-report country derivation (the 6 OECS states share ECCAA):
  V2-=AG  J3-=GD  J6-=LC  J7-=DM  J8-=VC  V4-=KN
  foreign kept: N-=US, G-=GB, VP-M=MS, YV=VE, ... ; fallback default 'AG'.

Run cycle (mini-PC): discover -> fetch -> parse -> build.
Weekly timer: eccaa-cycle.timer @ Sun 18:15 UTC.
