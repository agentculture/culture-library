/* Shape chart geometry and axis labels (plan task t17): pure functions, no DOM, no imports (the
 * tests run this module under Node); evals-shape.js draws what these return.
 *
 * The design canvas "Radar" board fixes the frame: one axis per benchmark result, axis 0
 * pointing up and the rest clockwise, 0 at the centre and 1 at the edge (higher is better).
 * The full chart is centred at (240, 240) with radius 170 in viewBox "-70 20 620 420"; a small
 * multiple at (110, 110) with radius 80 in viewBox "20 20 180 175". An axis with no score
 * (null) is a gap: the outline breaks there and its wedge stays empty, so it never reads as 0.
 */
export const FULL = { cx: 240, cy: 240, r: 170, viewBox: '-70 20 620 420' };
export const MINI = { cx: 110, cy: 110, r: 80, viewBox: '20 20 180 175' };

/* What each radar axis of GET /api/eval/radar is called, and which suite metric (from GET
 * /api/eval/suites) defines it. ``inverse``: the axis is one minus that metric. Axes beyond these
 * come from the suite registry and are defined by the metric of the same name. */
const AXES = {
  found_at_1: { label: 'Found at 1', short: 'F1', metric: 'found_at_1' },
  found_at_3: { label: 'Found at 3', short: 'F3', metric: 'found_at_3' },
  found_at_5: { label: 'Found at 5', short: 'F5', metric: 'found_at_5' },
  mrr: { label: 'MRR', short: 'MRR', metric: 'mrr' },
  judge_precision: { label: 'Judge precision', short: 'JP', metric: 'context_precision' },
  judge_recall: { label: 'Judge recall', short: 'JR', metric: 'context_recall' },
  no_errors: { label: 'No errors', short: 'OK', metric: 'error_rate', inverse: true },
  latency_in_budget: { label: 'Latency in budget', short: 'LB', metric: 'within_budget' },
  no_duplicates: { label: 'No duplicates', short: 'ND', metric: 'duplicate_rate', inverse: true },
  fresh: { label: 'Fresh', short: 'FR', metric: 'fresh_share' },
};
// Used only when the suite catalog has no definition for the axis' metric.
const FALLBACK = {
  found_at_1: 'Share of questions with a reference passage at rank 1.',
  found_at_3: 'Share of questions with a reference passage in the top 3.',
  found_at_5: 'Share of questions with a reference passage in the top 5.',
  mrr: 'Mean reciprocal rank of the first reference passage.',
  judge_precision: 'Judged context precision: are the useful passages ranked first? No score on judge-free runs.',
  judge_recall: 'Judged context recall: do the passages cover the reference answer? No score on judge-free runs.',
  no_errors: 'One minus the share of questions whose retrieval failed.',
  latency_in_budget: 'Share of questions answered within the latency budget.',
  no_duplicates: 'One minus the share of duplicate passages in the results.',
  fresh: "Share of questions whose reference chunks still exist in the item's current index.",
};

const words = key => String(key).split('_').filter(Boolean);
const human = key => { const s = words(key).join(' '); return s.charAt(0).toUpperCase() + s.slice(1); };

/**
 * The axes to draw, in the given order (the radar reply's order): [{key, label, short, tip, metric,
 * inverse}]; ``metric`` is the suite metric behind the axis (the axis is 1 - metric when inverse).
 * ``metrics`` maps a suite metric name to its definition (GET /api/eval/suites).
 */
export function axisList(keys, metrics, depth = null) {
  const def = name => (metrics && Object.prototype.hasOwnProperty.call(metrics, name) ? metrics[name] : null);
  return (keys || []).map(key => {
    const a = AXES[key];
    if (!a) {
      return { key, label: human(key), short: words(key).map(w => w[0].toUpperCase()).join('').slice(0, 3), tip: def(key) || human(key), metric: key, inverse: false };
    }
    const d = def(a.metric);
    let tip = FALLBACK[key];
    if (d && a.inverse) tip = `One minus ${a.metric}. ${a.metric}: ${d}`;
    else if (d) tip = a.metric === key ? d : `${a.metric}: ${d}`;
    const info = DEPTH_METRICS[key] ? depthInfo(DEPTH_METRICS[key], depth) : null;
    return { key, label: a.label, short: a.short, tip, metric: a.metric, inverse: Boolean(a.inverse), info };
  });
}

// Axes measured over a rank depth (deviation d2): they carry an (i) note naming it (d4).
const DEPTH_METRICS = { mrr: 'MRR', ndcg: 'nDCG' };

/**
 * The (i) note for a depth metric: which top it was measured over (``MRR@5``, ``MRR@10``).
 * ``depth`` is a number, 'mixed' (runs with different k) or null (no recorded k).
 */
export function depthInfo(name, depth) {
  if (typeof depth === 'number') {
    const why = depth < 10
      ? ` These runs retrieved ${depth} passages per question (k ${depth}), so ranks below ${depth} were never seen; a k 10 run reports ${name}@10.`
      : '';
    return `${name}@${depth}: measured over the top ${depth} ranks.${why}`;
  }
  if (depth === 'mixed') {
    return `${name} depth differs between these runs: each is measured over the top min(10, k) ranks it retrieved (${name}@5 for k 5, ${name}@10 for k 10 or more).`;
  }
  return `${name} is measured over the top min(10, k) ranks a run retrieved (${name}@5 for k 5, ${name}@10 for k 10 or more).`;
}

const r1 = v => Math.round(v * 10) / 10;
const clamp = v => Math.max(0, Math.min(1, v));
const angle = (i, n) => (2 * Math.PI * i) / n;

/** Axis ``i`` of ``n`` at value ``v`` (clamped to 0..1) in frame ``g``: [x, y], to 0.1. */
export function polar(i, n, v, g) {
  const a = angle(i, n), d = clamp(v) * g.r;
  return [r1(g.cx + d * Math.sin(a)), r1(g.cy - d * Math.cos(a))];
}
const pt = p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`;

/** The SVG points of the ring at value ``v`` on every axis (grid rings, the perfect outline). */
export const ring = (n, v, g) => Array.from({ length: n }, (_, i) => pt(polar(i, n, v, g))).join(' ');

/**
 * The shape for ``values`` (one per axis, null for an unscored axis) in frame ``g``:
 *   - vertices: [{i, x, y, v}] for each scored axis;
 *   - gaps: the unscored axis indexes;
 *   - lines: SVG points of the outline. With no gap it is one closed line; otherwise one open
 *     line per run of two or more neighbouring scored axes (a lone axis is only a vertex);
 *   - fills: SVG points to fill. With no gap, the shape itself; otherwise each run as a fan
 *     from the centre, so the wedges beside a gap stay empty.
 */
export function outline(values, g) {
  const n = values.length;
  const has = i => values[i] !== null && values[i] !== undefined && !Number.isNaN(values[i]);
  const vertices = [], gaps = [];
  for (let i = 0; i < n; i++) {
    if (has(i)) { const [x, y] = polar(i, n, values[i], g); vertices.push({ i, x, y, v: values[i] }); } else gaps.push(i);
  }
  const at = i => pt(polar(i, n, values[i], g));
  if (!vertices.length) return { fills: [], lines: [], vertices, gaps };
  if (!gaps.length) {
    const all = vertices.map(v => at(v.i));
    return { fills: [all.join(' ')], lines: [[...all, all[0]].join(' ')], vertices, gaps };
  }
  // Walk once round the chart, starting just after the first gap, collecting scored runs.
  const runs = [];
  let cur = [];
  for (let s = 1; s <= n; s++) {
    const i = (gaps[0] + s) % n;
    if (has(i)) cur.push(i);
    else { if (cur.length) runs.push(cur); cur = []; }
  }
  if (cur.length) runs.push(cur);
  const long = runs.filter(r => r.length >= 2);
  const centre = pt([g.cx, g.cy]);
  return {
    fills: long.map(r => [centre, ...r.map(at)].join(' ')),
    lines: long.map(r => r.map(at).join(' ')),
    vertices,
    gaps,
  };
}

/** Where axis ``i``'s label goes, outside the edge: {x, y, anchor}. */
export function labelAt(i, n, g) {
  const a = angle(i, n), s = Math.sin(a), c = Math.cos(a), k = g.r / 170;
  const d = g.r + 20 * k;
  return {
    x: r1(g.cx + d * s),
    y: r1(g.cy - d * c + (6 - 10 * c) * k),
    anchor: Math.abs(s) < 0.05 ? 'middle' : s > 0 ? 'start' : 'end',
  };
}

/**
 * Where a grid ring's value label (0.5, 1.0) goes: on the ring's edge between the last axis and
 * axis 0, so it never sits on axis 0, where a data vertex often is. {x, y, anchor}; ``y`` is the
 * text baseline, a little below the edge so the label is centred on it.
 */
export function ringLabelAt(v, n, g) {
  const a = -Math.PI / Math.max(n, 3), d = clamp(v) * g.r * Math.cos(Math.PI / Math.max(n, 3));
  const k = g.r / 170;
  return { x: r1(g.cx + d * Math.sin(a)), y: r1(g.cy - d * Math.cos(a) + 4.4 * k), anchor: 'middle' };
}

/** A score to two decimals, or "not scored". */
export const f2 = v => (v === null || v === undefined || Number.isNaN(v) ? 'not scored' : v.toFixed(2));
/** Values under 0.70 are marked in the numbers table (canvas "Radar"). */
export const isLow = v => typeof v === 'number' && v < 0.7;
