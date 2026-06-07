# jst_ingest/jst.py
"""
JST Argentina (Junta de Seguridad en el Transporte) aviation investigation
parser — intranet.jst.gob.ar events API + so.jst.gob.ar PDF manifest.

The event listing is a paginated JSON API:
    GET /expedientes/w/busqueda-modos/?modo=2&pagina=N
⚠️ modo=2 is AVIATION (numeric).  20 events/page, ~120 pages, 2,390 events.
Plain httpx works (browser UA only; no Cloudflare).  Response shape:
    {"expedientes": [...], "cantidad": 2390, "paginas": {"max": 120, ...}}
Paginate until an empty/short page (or pagina > paginas.max).

Each event carries:
    id (internal int — UNRELATED to expediente), fecha (date), hora (time),
    estado (En Curso/Finalizada), nro_expediente ("41546464/26" — '/YY' suffix),
    lugar (location), reseña (narrative paragraph — key carries the ñ accent),
    vehiculos[] (marca=manufacturer, modelo, matricula=registration LV-/CC-,
    fase=phase, operacion, danios, suceso, categoria, victimas_fatales).

The FINAL reports live in a separate manifest:
    GET https://so.jst.gob.ar/static/informes/Index.json
a dict keyed by the 8-digit ZERO-PADDED expediente core → [{tipo, path}, ...].
PDF URL = https://so.jst.gob.ar/static/informes/{path}.

⚠️ JOIN TRAP: the manifest key is the 8-digit zero-padded expediente *core*;
the API's nro_expediente carries a '/YY' suffix and the API 'id' is unrelated.
Join on the zero-padded core (case_id).

Document preference (FINAL first): ISO > IB > INC > IPROV > IP.
"""
import re

EVENTS_BASE = "https://intranet.jst.gob.ar/expedientes/w/busqueda-modos/"
MANIFEST_URL = "https://so.jst.gob.ar/static/informes/Index.json"
PDF_BASE = "https://so.jst.gob.ar/static/informes"
MODO_AVIATION = 2
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "es,en;q=0.9",
}

# FINAL report first, then descending richness.
DOC_PREFERENCE = ["ISO", "IB", "INC", "IPROV", "IP"]
_PREF_RANK = {tipo: i for i, tipo in enumerate(DOC_PREFERENCE)}


def case_id_from_expediente(nro_expediente):
    """
    '41546464/26' → '41546464' (8-digit zero-padded core).
    Strips the '/YY' suffix, keeps digits only, left-pads to 8.
    """
    if not nro_expediente:
        return None
    core = str(nro_expediente).split("/", 1)[0]
    digits = re.sub(r"\D", "", core)
    if not digits:
        return None
    return digits.zfill(8)


def pick_doc(docs):
    """
    Choose the preferred manifest entry from a list of {tipo, path}.
    Preference ISO > IB > INC > IPROV > IP; unknown tipos rank last.
    Returns (path, tipo) or (None, None) when docs is empty.
    """
    if not docs:
        return None, None
    best = min(
        docs,
        key=lambda d: _PREF_RANK.get((d.get("tipo") or "").upper(), len(DOC_PREFERENCE)),
    )
    return best.get("path"), (best.get("tipo") or None)


def pdf_url(path):
    if not path:
        return None
    return f"{PDF_BASE}/{path.lstrip('/')}"


def parse_event(event):
    """
    Map a raw event dict → flat metadata.  registration/aircraft/operator-ish
    fields come from vehiculos[0]; fatalities summed across vehiculos.
    Returns dict with keys:
        case_id, nro_expediente, date, location, summary, status,
        aircraft, registration, operator, occurrence_type, fatalities.
    """
    nro = event.get("nro_expediente")
    case_id = case_id_from_expediente(nro)
    vehiculos = event.get("vehiculos") or []
    v0 = vehiculos[0] if vehiculos else {}

    marca = (v0.get("marca") or "").strip()
    modelo = (v0.get("modelo") or "").strip()
    aircraft = " ".join(p for p in (marca, modelo) if p) or None

    fatalities = 0
    have_fatal = False
    for v in vehiculos:
        f = v.get("victimas_fatales")
        if isinstance(f, int):
            fatalities += f
            have_fatal = True
    if not have_fatal:
        fatalities = None

    # 'reseña' carries the ñ; tolerate the de-accented spelling too.
    summary = event.get("reseña")
    if summary is None:
        summary = event.get("resena")

    return {
        "case_id": case_id,
        "nro_expediente": nro,
        "date": (event.get("fecha") or "")[:10] or None,
        "location": (event.get("lugar") or "").strip() or None,
        "summary": (summary or "").strip() or None,
        "status": event.get("estado") or None,
        "aircraft": aircraft,
        "registration": (v0.get("matricula") or "").strip() or None,
        "operator": (v0.get("operacion") or "").strip() or None,
        "occurrence_type": (v0.get("suceso") or "").strip() or None,
        "fatalities": fatalities,
    }


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def events_url(pagina):
    return f"{EVENTS_BASE}?modo={MODO_AVIATION}&pagina={pagina}"


def fetch_events_page(client, pagina):
    """Return the list of raw event dicts for one page."""
    resp = client.get(events_url(pagina))
    resp.raise_for_status()
    data = resp.json()
    return data.get("expedientes") or []


def fetch_manifest(client):
    """Return the Index.json dict {case_id8: [{tipo, path}, ...]}."""
    resp = client.get(MANIFEST_URL)
    resp.raise_for_status()
    return resp.json()


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
