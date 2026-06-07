'use strict';

const REST_SUMMARY = 'https://en.wikipedia.org/api/rest_v1/page/summary/';
const ACTION_API   = 'https://en.wikipedia.org/w/api.php';

const UA = 'wikidata-ingest/1.0 (+https://github.com/disclaimer8/aviation-safety-scrapers)';

async function fetchSummary(title) {
  if (!title) return null;
  const url = REST_SUMMARY + encodeURIComponent(title.replace(/ /g, '_'));
  const res = await fetch(url, { headers: { 'User-Agent': UA, Accept: 'application/json' } });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Wikipedia summary ${res.status} for ${title}`);
  const json = await res.json();
  return (json && typeof json.extract === 'string' && json.extract.trim()) ? json.extract.trim() : null;
}

async function fetchFullExtract(title) {
  if (!title) return null;
  const params = new URLSearchParams({
    action:       'query',
    prop:         'extracts',
    explaintext:  '1',
    redirects:    '1',
    format:       'json',
    titles:       title,
    origin:       '*',
  });
  const res = await fetch(`${ACTION_API}?${params}`, { headers: { 'User-Agent': UA, Accept: 'application/json' } });
  if (!res.ok) throw new Error(`Wikipedia action API ${res.status} for ${title}`);
  const json = await res.json();
  const pages = json?.query?.pages || {};
  const first = Object.values(pages)[0];
  if (!first || first.missing !== undefined) return null;
  const ext = (first.extract || '').trim();
  return ext || null;
}

async function fetchArticleText(title) {
  const summary = await fetchSummary(title).catch(() => null);
  if (summary && summary.length >= 300) return summary;

  const full = await fetchFullExtract(title).catch(() => null);
  if (full) {
    if (full.length <= 4000) return full;
    return full.slice(0, 4000).replace(/\s+\S*$/, '') + '…';
  }
  return summary || null;
}

module.exports = { fetchSummary, fetchFullExtract, fetchArticleText };
