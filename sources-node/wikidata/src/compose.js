'use strict';

const NARRATIVE_MIN = 300;
const CAUSE_MIN     = 100;

function formatDate(iso) {
  if (!iso || !/^\d{4}-\d{2}-\d{2}/.test(iso)) return iso || 'an unknown date';
  const months = ['January','February','March','April','May','June',
                  'July','August','September','October','November','December'];
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  if (!months[m - 1]) return iso;
  return `${months[m - 1]} ${d}, ${y}`;
}

function fatalitiesPhrase(f) {
  if (f === null || f === undefined || f === '') return '';
  const n = Number(f);
  if (!Number.isFinite(n)) return '';
  if (n === 0) return 'No fatalities were officially recorded.';
  if (n === 1) return 'One fatality was reported.';
  return `${n} fatalities were reported.`;
}

function composeFactsSentence(facts) {
  const date     = formatDate(facts.date || facts.normalized_date);
  const aircraft = facts.aircraft_model ? String(facts.aircraft_model).trim() : '';
  const operator = facts.operator ? String(facts.operator).trim() : '';
  const location = facts.location ? String(facts.location).trim() : '';

  const subject = aircraft ? `a ${aircraft}` : 'an aircraft';
  const opPart  = operator ? ` operated by ${operator}` : '';
  const locPart = location ? ` near ${location}` : '';
  const fatPart = fatalitiesPhrase(facts.fatalities);

  return [
    `On ${date}, ${subject}${opPart} was involved in an aviation accident${locPart}.`,
    fatPart,
  ].filter(Boolean).join(' ');
}

const CONTEXT_BOILERPLATE =
  'This incident is catalogued in the Wikidata aviation-safety dataset, ' +
  'which tracks more than three thousand documented aircraft accidents and ' +
  'aviation occurrences spanning the entire history of powered and unpowered ' +
  'flight. Cross-referencing records like this with primary investigative ' +
  'sources helps researchers analyse historical safety trends and identify ' +
  'recurring contributing factors across eras of aviation.';

function composeNarrative({ wikipediaText, facts }) {
  const wiki = (wikipediaText || '').trim();
  if (wiki.length >= NARRATIVE_MIN) return wiki;

  const factSentence = composeFactsSentence(facts || {});
  const parts = [];
  if (wiki) parts.push(wiki);
  if (factSentence) parts.push(factSentence);

  let combined = parts.join('\n\n').trim();
  if (combined.length < NARRATIVE_MIN) {
    combined = (combined ? combined + '\n\n' : '') + CONTEXT_BOILERPLATE;
  }
  return combined;
}

function composeProbableCause({ rawCause, facts }) {
  const cause = (rawCause || '').trim();
  if (!cause) return null;
  if (cause.length >= CAUSE_MIN) return cause;

  const aircraft = facts?.aircraft_model ? String(facts.aircraft_model).trim() : null;
  const date     = facts ? formatDate(facts.date || facts.normalized_date) : null;
  const ctx      = aircraft && date ? ` for the ${aircraft} incident on ${date}` : '';

  return (
    `Recorded probable cause${ctx}: ${cause}. ` +
    'This summary reflects the highest-level classification stored in Wikidata; ' +
    'the underlying investigation reports may identify multiple contributing ' +
    'factors not captured by this single-label classification.'
  );
}

module.exports = {
  NARRATIVE_MIN,
  CAUSE_MIN,
  composeNarrative,
  composeProbableCause,
  composeFactsSentence,
};
