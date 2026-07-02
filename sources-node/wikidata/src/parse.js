'use strict';

function extractQId(uri) {
  if (!uri || typeof uri !== 'string') return null;
  const m = uri.match(/Q\d+$/);
  return m ? m[0] : null;
}

function val(binding, key) {
  if (!binding[key]) return null;
  const v = binding[key].value;
  return (v && v.trim()) ? v : null;
}

function parseFactors(raw) {
  if (!raw) return [];
  const seen = new Set();
  const out  = [];
  for (const piece of raw.split(';;')) {
    const t = piece.trim();
    if (!t) continue;
    const k = t.toLowerCase();
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(t);
  }
  return out;
}

function parseWikidataResponse(json) {
  const bindings = json?.results?.bindings || [];
  const out = [];
  for (const b of bindings) {
    const q_id = extractQId(b.event?.value);
    if (!q_id) continue;
    // ?causeLabel is now a GROUP_CONCAT aggregate (mirrors ?factorsLabels)
    // so multi-valued P1196 causes no longer inflate the SPARQL GROUP BY
    // row count. Split it the same way factors are split; the first (only,
    // in the common case) cause remains the scalar `probable_cause` used by
    // compose.js, while `causes` exposes the full deduped list.
    const causes = parseFactors(val(b, 'causeLabel'));
    out.push({
      q_id,
      label:           val(b, 'eventLabel'),
      narrative_text:  val(b, 'description'),
      probable_cause:  causes[0] || null,
      causes,
      date:            val(b, 'date'),
      factors:         parseFactors(val(b, 'factorsLabels')),
    });
  }
  return out;
}

module.exports = { extractQId, parseFactors, parseWikidataResponse };
