'use strict';
// Single source of truth for the general-aviation cut definitions used by the
// bird-strike stories. The offline aggregate (server/scripts/wildlife-aggregate.js)
// builds its SQL from the *Case() helpers here and the story query registry
// gates against the bucket names here, so a boundary can move in one place only.
// Mirrors wildlifeFamilies.js, which exists because two hand-kept copies of the
// same grouping drifted apart and quietly broke a story's metrics.

// Height above ground, in feet. 'ground' is a literal zero (strike on the roll),
// which is a different situation from "low" and must not be folded into 1-500.
const HEIGHT_BUCKETS = [
  { bucket: 'ground', max: 0 },
  { bucket: '1-500', max: 500 },
  { bucket: '501-1500', max: 1500 },
  { bucket: '1501-3000', max: 3000 },
  { bucket: '3000+', max: Infinity },
];
// Indicated airspeed, knots. Boundaries chosen to separate typical trainer
// approach speeds (<80), cruise for light singles (80-129), high-performance
// singles and light twins (130-159), and turbine GA (160+).
const SPEED_BUCKETS = [
  { bucket: '<80', max: 79 },
  { bucket: '80-99', max: 99 },
  { bucket: '100-129', max: 129 },
  { bucket: '130-159', max: 159 },
  { bucket: '160+', max: Infinity },
];
const UNKNOWN = 'unknown';

function _bucket(defs, v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return UNKNOWN;
  const n = Number(v);
  for (const d of defs) if (n <= d.max) return d.bucket;
  return defs[defs.length - 1].bucket;
}
const heightBucket = (ft) => _bucket(HEIGHT_BUCKETS, ft);
const speedBucket = (kt) => _bucket(SPEED_BUCKETS, kt);

function _buildCase(defs, col) {
  const whens = defs
    .filter((d) => d.max !== Infinity)
    .map((d) => `    WHEN ${col} <= ${d.max} THEN '${d.bucket}'`);
  const last = defs[defs.length - 1].bucket;
  return `CASE\n    WHEN ${col} IS NULL THEN '${UNKNOWN}'\n${whens.join('\n')}\n    ELSE '${last}' END`;
}
const buildHeightCase = (col = 'HEIGHT') => _buildCase(HEIGHT_BUCKETS, col);
const buildSpeedCase = (col = 'SPEED') => _buildCase(SPEED_BUCKETS, col);

// Per-part strike/damage flag columns in the FAA schema. A single strike can
// set several of these, so part counts DO NOT sum to the strike total and must
// never be rendered as a composition — they are a table of
// "struck / damaged / damage-given-struck".
const PART_COLUMNS = [
  { bucket: 'Windshield', strCol: 'STR_WINDSHLD', damCol: 'DAM_WINDSHLD' },
  { bucket: 'Nose', strCol: 'STR_NOSE', damCol: 'DAM_NOSE' },
  { bucket: 'Radome', strCol: 'STR_RAD', damCol: 'DAM_RAD' },
  { bucket: 'Engine 1', strCol: 'STR_ENG1', damCol: 'DAM_ENG1' },
  { bucket: 'Engine 2', strCol: 'STR_ENG2', damCol: 'DAM_ENG2' },
  { bucket: 'Propeller', strCol: 'STR_PROP', damCol: 'DAM_PROP' },
  { bucket: 'Wing or rotor', strCol: 'STR_WING_ROT', damCol: 'DAM_WING_ROT' },
  { bucket: 'Fuselage', strCol: 'STR_FUSE', damCol: 'DAM_FUSE' },
  { bucket: 'Landing gear', strCol: 'STR_LG', damCol: 'DAM_LG' },
  { bucket: 'Tail', strCol: 'STR_TAIL', damCol: 'DAM_TAIL' },
  { bucket: 'Lights', strCol: 'STR_LGHTS', damCol: 'DAM_LGHTS' },
];

const MONTHS = ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12'];
const MONTH_LABELS = {
  '01': 'January', '02': 'February', '03': 'March', '04': 'April',
  '05': 'May', '06': 'June', '07': 'July', '08': 'August',
  '09': 'September', '10': 'October', '11': 'November', '12': 'December',
};

const DIMENSIONS = [
  { dimension: 'month', buckets: MONTHS },
  { dimension: 'height', buckets: [...HEIGHT_BUCKETS.map((b) => b.bucket), UNKNOWN] },
  { dimension: 'speed', buckets: [...SPEED_BUCKETS.map((b) => b.bucket), UNKNOWN] },
  { dimension: 'time_of_day', buckets: ['Dawn', 'Day', 'Dusk', 'Night', UNKNOWN] },
  { dimension: 'phase', buckets: [] }, // free-form: whatever PHASE_OF_FLIGHT holds
  { dimension: 'part_struck', buckets: PART_COLUMNS.map((p) => p.bucket) },
];

module.exports = {
  UNKNOWN, HEIGHT_BUCKETS, SPEED_BUCKETS, PART_COLUMNS, DIMENSIONS, MONTHS, MONTH_LABELS,
  heightBucket, speedBucket, buildHeightCase, buildSpeedCase,
};
