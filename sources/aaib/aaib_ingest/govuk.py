# aaib_ingest/govuk.py
SEARCH_URL = "https://www.gov.uk/api/search.json"
CONTENT_URL = "https://www.gov.uk/api/content"


def slug_from_link(link):
    return (link or "").rstrip("/").rsplit("/", 1)[-1]


def iter_search(client, *, page_size=100):
    start = 0
    while True:
        resp = client.get(SEARCH_URL, params={
            "filter_format": "aaib_report",
            "order": "-public_timestamp",
            "count": page_size,
            "start": start,
            "fields": "link,title,public_timestamp",
        })
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break
        for r in results:
            yield r
        start += page_size
        if start >= data.get("total", 0):
            break


def get_content(client, slug):
    resp = client.get(f"{CONTENT_URL}/aaib-reports/{slug}")
    resp.raise_for_status()
    return resp.json()


def pick_main_pdf(attachments):
    for a in attachments or []:
        if a.get("content_type") == "application/pdf" and "glossary" not in (a.get("title", "").lower()):
            return a
    return None


def download(client, url, dest):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        f.write(resp.content)
