import sys, time, os
sys.path.insert(0, os.path.expanduser("~/aias-ingest"))
import aias_scraper as A
c=A.conn()
rows=c.execute("SELECT case_id,report_url,event_date FROM aias_reports WHERE status='new' LIMIT 3").fetchall()
br=A.Browser()
try:
    br.goto(A.BASE+"/en/homepage/")
    for row in rows:
        url=row["report_url"]
        br.goto(url); time.sleep(1.5)
        title=br.page.title()
        html=br.page.content()
        import re
        hrefs=re.findall(r'href="([^"]+\.pdf[^"]*)"', html, re.I)
        pdf_url,lang=A.pick_report_pdf(hrefs)
        ev,rtype,acft,reg,loc=A.parse_title(title)
        cid=A.case_id_from_pdf(pdf_url) if pdf_url else "NO_PDF"
        print("TITLE:", title[:80])
        print("  -> case",cid,"| date",ev,"| type",rtype,"| acft",acft,"| reg",reg,"| loc",(loc or "")[:40],"| lang",lang)
        print("  PDF:", pdf_url)
        if pdf_url:
            res=br.fetch(pdf_url)
            if res.get("ok") and res.get("b64"):
                import base64
                dest=os.path.join(A.PDFDIR,"TEST_"+re.sub(r"[^A-Za-z0-9_.-]","_",cid)+".pdf")
                open(dest,"wb").write(base64.b64decode(res["b64"]))
                txt=A.extract_text(dest)
                print("  PDF_OK bytes", os.path.getsize(dest), "| pdftext_len", len(txt), "| head:", re.sub(r"\s+"," ",txt[:150]))
            else:
                print("  PDF_FETCH_FAIL", res.get("status"))
        print()
finally: br.close()
