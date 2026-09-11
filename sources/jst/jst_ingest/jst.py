# jst_ingest/jst.py
"""
JST Argentina (Junta de Seguridad en el Transporte) aviation investigation
parser — so.jst.gob.ar PDF manifest + each report's own front matter.

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
import unicodedata

# The country this source covers, as ISO 3166-1 alpha-2. Declared rather
# than inferred: the coverage database and the scraper inventory had drifted
# apart, and only 32 of 90 sources stated their country anywhere a machine
# could read. scripts/check_coverage.py reconciles the two from this.
COUNTRY_ISO2 = "AR"


# There is no intranet endpoint here any more. discover() used to enumerate
# events from intranet.jst.gob.ar, whose robots.txt is a blanket `Disallow: /`
# — a host named intranet telling crawlers to stay out. Everything it supplied
# is on the public host: the manifest below lists every published report, and
# each report prints its own metadata on page one (parse_report_header).
#
# parse_event() survives for anyone replaying an archived response of the old
# payload; nothing in the pipeline calls it, and nothing builds that URL.
MANIFEST_URL = "https://so.jst.gob.ar/static/informes/Index.json"
PDF_BASE = "https://so.jst.gob.ar/static/informes"
MODO_AVIATION = 2
DELAY = 1.5

# Manifest paths are "<MODE>/<YEAR>/<MMDDYY>-<id>/<TIPO>-<id>-<YY>.pdf".
# AE is aeronáutica; the others (AU road, MA maritime, FE rail, MM) are the
# JST's other transport modes and are not ours.
AVIATION_PREFIX = "AE/"
_PATH_DATE = re.compile(r"^AE/(\d{4})/(\d{2})(\d{2})(\d{2})-")

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


# ── the allowed route ────────────────────────────────────────────────────────
#
# Everything below reads the public manifest and the reports' own front matter.
# It replaces the intranet events API, which robots.txt refuses entirely.
#
# What each source of truth gives us:
#   manifest path   case_id, occurrence date, doc tipo, PDF URL, aviation filter
#   report header   aircraft, registration, location, occurrence type, date
#
# The path date is the primary one: 1826 of 1828 aviation documents carry it,
# and it agrees with the header where both exist. The header supplies what the
# path cannot, and is the only source for aircraft and registration now.

_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# Argentine civil marks: LV-ABC, and LV-S123 for experimental/ultralight.
# pdftotext folds "LV-CMB" across a line break into "LVCMB", so the dash is
# optional — seen live in INT-4080004-22.
_REG = re.compile(r"\bLV-?((?:[A-Z]{3})|(?:S\d{2,4}))\b")


def aviation_docs(manifest):
    """Yield (case_id, tipo, path) for the aviation documents in Index.json.

    The manifest is {case_id: [{tipo, path}, ...]} across every transport mode
    the JST covers; only AE/ is ours.
    """
    for case_id, docs in sorted((manifest or {}).items()):
        for doc in docs or []:
            path = (doc or {}).get("path") or ""
            if path.startswith(AVIATION_PREFIX):
                yield case_id, (doc.get("tipo") or "").strip().upper(), path


def date_from_path(path):
    """The MMDDYY in a manifest path, as ISO. None when the path is irregular.

    A handful of old entries use "-nointranet" or a short serial where the
    case id sits, but the date prefix is still there — that is why this reads
    the date and ignores the rest.
    """
    m = _PATH_DATE.match(path or "")
    if not m:
        return None
    year, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    return f"{year:04d}-{mm:02d}-{dd:02d}"


def pick_manifest_doc(docs):
    """The best document for one case, by the ISO > IB > INC > IPROV > IP rule."""
    ranked = sorted(docs, key=lambda d: _PREF_RANK.get(d[0], len(DOC_PREFERENCE)))
    return ranked[0] if ranked else None


def _flat(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _strip_accents(value):
    return "".join(c for c in unicodedata.normalize("NFD", value)
                   if unicodedata.category(c) != "Mn")


def _spanish_date(text):
    """'08 de junio de 2024' or '17/6/2026' → ISO."""
    m = re.search(r"(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚáéíóú]+)\s+de\s+(\d{4})", text or "")
    if m:
        month = _MONTHS.get(_strip_accents(m.group(2)).lower())
        if month:
            return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text or "")
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _label(name, text):
    """Value of a `Label: value` line, up to the next label or blank run."""
    m = re.search(
        r"^[ \t]*%s[ \t]*:[ \t]*(.+?)(?=\n[ \t]*[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ ]{2,34}[ \t]*:|\n[ \t]*\n)"
        % name, text, re.M | re.S | re.I)
    return _flat(m.group(1)) if m else None


def _label_below(name, text, max_len=80):
    """Value on the line AFTER a bare label, for the INFORME BÁSICO form.

    That layout is a table, and pdftotext emits the label and its value as
    consecutive lines with no colon between them. It scrambles wider columns —
    `Marca` ends up six lines from `Piper` — so only the fields that stay
    adjacent are read this way. Aircraft is deliberately not one of them:
    guessing it from a shuffled column would be worse than leaving it None.
    """
    m = re.search(r"^[ \t]*%s[ \t]*$" % name, text, re.M | re.I)
    if not m:
        return None
    for line in text[m.end():].splitlines():
        value = line.strip()
        if not value:
            continue
        if len(value) > max_len or value.endswith(":"):
            return None
        return value
    return None


def parse_report_header(text):
    """Occurrence metadata from a JST report's own front matter.

    Three layouts occur, all seen live:

      labelled   Suceso: / Título: / Fecha y hora del suceso: / Expediente:
                 — IP, IPROV, INT, INC, ISO and the modern forms generally.
      table      a vertical "ID / INFORME BÁSICO / FECHA / HORA UTC" block
                 with the values beneath it, then Lugar / Provincia.
                 — IB and IF.
      free-form  an untitled cover block, aircraft and mark on one line.
                 — older IPROV.

    Missing fields come back as None rather than a guess; the caller keeps the
    manifest's date, which is present for all but two aviation documents.
    """
    head = (text or "")[:6000]
    out = {"aircraft": None, "registration": None, "location": None,
           "occurrence_type": None, "date_of_occurrence": None,
           "nro_expediente": None}

    out["nro_expediente"] = (_label("Expediente", head) or "")[:60] or None
    out["occurrence_type"] = (_label("Suceso", head) or "")[:40] or None

    fecha = _label(r"Fecha y hora del suceso", head)
    if fecha:
        out["date_of_occurrence"] = _spanish_date(fecha)

    titulo = _label(r"T[íi]tulo", head)
    if titulo:
        out.update(_split_titulo(titulo))

    # ── table layout (INFORME BÁSICO) ────────────────────────────────────────
    # pdftotext renders it as the four labels, then the four values, each on
    # its own line. Read the values rather than trying to align columns.
    if not out["date_of_occurrence"] or not out["occurrence_type"]:
        m = re.search(r"INFORME\s+B[ÁA]SICO(.{0,400})", head, re.S | re.I)
        if m:
            block = m.group(1)
            if not out["occurrence_type"]:
                t = re.search(r"^\s*(ACCIDENTE|INCIDENTE(?:\s+GRAVE)?)\s*$",
                              block, re.M | re.I)
                if t:
                    out["occurrence_type"] = _flat(t.group(1)).title()
            if not out["date_of_occurrence"]:
                out["date_of_occurrence"] = _spanish_date(block)

    # ── ADREP layout ─────────────────────────────────────────────────────────
    # The short IP/INT forms are an ADREP table: bare labels with the value on
    # the following line, in English-ish field names.
    if not out["aircraft"]:
        out["aircraft"] = _label_below(r"Fabricante/modelo", head)

    if not out["occurrence_type"]:
        # ADREP prints the class in English under "Datos del Suceso".
        adrep = _label_below(r"Datos del Suceso", head, max_len=30)
        if adrep and re.fullmatch(r"Accident|Incident|Serious incident", adrep, re.I):
            out["occurrence_type"] = adrep.capitalize()

    if not out["location"]:
        lugar = (_label("Lugar(?: del suceso)?", head)
                 or _label_below("Lugar", head)
                 or _label_below(r"Nombre del lugar", head))
        provincia = _label("Provincia", head) or _label_below("Provincia", head)
        parts = [p for p in (lugar, provincia) if p and len(p) < 80]
        if parts:
            out["location"] = ", ".join(dict.fromkeys(parts))[:120]

    if not out["registration"]:
        m = _REG.search(head)
        if m:
            out["registration"] = "LV-" + m.group(1)

    if not out["date_of_occurrence"]:
        out["date_of_occurrence"] = _spanish_date(head)

    return out


def _split_titulo(titulo):
    """`<cause>. <Aircraft>, matrícula <REG>, <place>, provincia de <X>`

    The cause runs to the first full stop; aircraft sits between that and
    "matrícula"; everything after the mark is the place. Older titles omit the
    word "matrícula" and simply list "Piper J-3C, LV-NDQ".
    """
    res = {}
    m = _REG.search(titulo)
    if m:
        res["registration"] = "LV-" + m.group(1)

    # Anchor on the mark and read outwards. The field immediately before it is
    # the aircraft and everything after is the place, whatever separates the
    # cause from the aircraft — a full stop in IP/INC titles ("Operaciones a
    # baja altura. Extra EA300-SC, matrícula LV-IUX, …") but a comma in INT
    # ones ("…de grupo motor, Embraer ERJ-190, matrícula LVCMB, …"), which an
    # expression anchored at the start cannot straddle.
    m = re.search(
        r"([^.,]{2,80}?)\s*,\s*(?:matr[íi]cula\s+)?LV-?(?:[A-Z]{3}|S\d{2,4})\b\s*,?\s*(.*)$",
        titulo, re.I | re.S)
    if m:
        aircraft = _flat(m.group(1))
        place = _flat(m.group(2)).rstrip(". ")
        if aircraft:
            res["aircraft"] = aircraft[:80]
        if place:
            res["location"] = place[:120]
    return res
