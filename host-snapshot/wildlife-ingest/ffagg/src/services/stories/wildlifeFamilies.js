'use strict';
// Single source of truth for the wildlife "animal family" grouping, shared by
// the offline aggregate (server/scripts/wildlife-aggregate.js builds its SQL
// CASE from buildFamilyCase()) and the story query registry (storyQueries.js
// resolves labels by slug via familyLabel()). Before this module the two
// sides carried the same display strings independently under a "keep in sync"
// comment; a rename in one place silently broke the other's metrics into
// em-dashes. Now a rename happens here or nowhere.
//
// ORDER MATTERS: rules are evaluated top-down and the first matching
// SPECIES_ID prefix wins, so 2-char prefixes (1G deer, 1C bats, NE gulls)
// must precede their 1-char parents (1 mammals, N shorebirds).
const FAMILIES = [
  { slug: 'deer', label: 'Deer', prefixes: ['1G'] },
  { slug: 'bats', label: 'Bats', prefixes: ['1C'] },
  { slug: 'other-mammals', label: 'Other mammals (coyotes, rabbits)', prefixes: ['1'] },
  { slug: 'waterfowl', label: 'Waterfowl (geese, ducks)', prefixes: ['J'] },
  { slug: 'raptors', label: 'Raptors (hawks, falcons, vultures)', prefixes: ['K'] },
  { slug: 'owls', label: 'Owls', prefixes: ['R'] },
  { slug: 'gulls', label: 'Gulls & terns', prefixes: ['NE'] },
  { slug: 'shorebirds', label: 'Shorebirds (plovers, killdeer)', prefixes: ['N'] },
  { slug: 'doves', label: 'Pigeons & doves', prefixes: ['O'] },
  { slug: 'songbirds', label: 'Perching songbirds', prefixes: ['Y', 'Z'] },
  { slug: 'herons', label: 'Herons & egrets', prefixes: ['I'] },
];
const OTHER_LABEL = 'Other';

const _bySlug = new Map(FAMILIES.map((f) => [f.slug, f.label]));

// Label for a family slug. Throws on a typo instead of returning undefined —
// an unknown slug in a metric definition is a programming error that must
// fail tests, not degrade to an em-dash at runtime.
function familyLabel(slug) {
  const label = _bySlug.get(slug);
  if (!label) throw new Error(`[wildlifeFamilies] unknown family slug "${slug}"`);
  return label;
}

// SQL CASE expression mapping a SPECIES_ID column to the family label —
// consumed by wildlife-aggregate.js when folding the raw FAA dump. Labels are
// embedded as single-quoted SQL literals; none contain quotes (asserted), so
// no escaping is needed.
function buildFamilyCase(col = 'SPECIES_ID') {
  const whens = FAMILIES.map(({ label, prefixes }) => {
    if (label.includes("'")) throw new Error(`[wildlifeFamilies] label with quote: ${label}`);
    const len = prefixes[0].length;
    if (!prefixes.every((p) => p.length === len)) throw new Error(`[wildlifeFamilies] mixed prefix lengths for ${label}`);
    const expr = `substr(${col},1,${len})`;
    const cond = prefixes.length === 1
      ? `${expr}='${prefixes[0]}'`
      : `${expr} IN (${prefixes.map((p) => `'${p}'`).join(',')})`;
    return `    WHEN ${cond} THEN '${label}'`;
  });
  return `CASE\n${whens.join('\n')}\n    ELSE '${OTHER_LABEL}' END`;
}

module.exports = { FAMILIES, OTHER_LABEL, familyLabel, buildFamilyCase };
