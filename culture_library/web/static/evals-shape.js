/* The shape chart (plan task t17; design canvas "Radar", reused by "Compare" for the shape of
 * the change). One axis per benchmark result, 0 at the centre and 1 at the edge; the dotted
 * outline is a perfect run. An "over" shape (solid, filled) is drawn over an optional "under"
 * shape (dashed, the earlier or before run) and an optional "mean" outline (dashed, small
 * multiples). An unscored axis (null) is a labelled gap, never a 0 (evals-geom.js outline()).
 *
 * API (all return DOM nodes; nothing here fetches or reads app state):
 *   shapeChart({axes, over, under?, mean?, mini?, label})  the SVG chart
 *       axes   G.axisList(keys, metrics): [{key, label, short, tip}], drawn in this order
 *       over   {name, values: {axisKey: number | null}}  the solid shape, with vertex tips
 *       under  {name, values}  dashed, hollow vertices with tips (the earlier / before run)
 *       mean   {name, values}  dashed outline only (the mean behind a small multiple)
 *       mini   true for a small multiple: the MINI frame, no axis labels or vertices
 *       label  the chart's accessible name; the values are appended to it
 *   shapeKey(entries)    the legend: [{mark: 'over' | 'under' | 'mean' | 'perfect', text}]
 *   shapeList(axes, over, under?)   per-axis readout beside the chart (value, earlier, change)
 *   shapeTable(axes, rows)          the numbers: rows [{name, num?, values, cls?, dir?}]
 *   shapeToggle(id)                 the Chart / Table segment. evals.js toggles the first
 *       .ev-viz / .ev-tbl inside the enclosing .ev-card, so a card holds the chart in .ev-viz
 *       and the table in .ev-tbl (hidden).
 *
 * Text from the API (titles, names) reaches the page as text nodes; tips are data-tip
 * attributes app.js shows on hover. Never markup.
 */
import { h } from './app.js';
import * as G from './evals-geom.js';

const NS = 'http://www.w3.org/2000/svg';
/** An SVG element; string kids become text nodes. */
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (/^on/i.test(k)) throw new Error('event handler attributes are not allowed');
    el.setAttribute(k, String(v));
  }
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false || k === '') continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

const val = (side, key) => {
  const v = side && side.values ? side.values[key] : null;
  return typeof v === 'number' && !Number.isNaN(v) ? v : null;
};
const sign = d => (d > 0.005 ? '+' : d < -0.005 ? '−' : '') + Math.abs(d).toFixed(2);

/** "found at 1 0.73, ..., judge precision not scored" for an accessible name. */
function spoken(axes, side) {
  return axes.map(a => `${a.label.toLowerCase()} ${G.f2(val(side, a.key))}`).join(', ');
}

function shapeMarks(axes, side, g, cls) {
  const o = G.outline(axes.map(a => val(side, a.key)), g);
  return [
    o.fills.map(p => s('polygon', { class: `${cls} sf`, points: p })),
    o.lines.map(p => s('polyline', { class: `${cls} sl`, points: p })),
  ];
}

export function shapeChart({ axes, over, under, mean, mini, label }) {
  const g = mini ? G.MINI : G.FULL, n = axes.length;
  const parts = [label || 'Shape chart', over ? `${over.name}: ${spoken(axes, over)}` : '',
    under ? `laid over ${under.name}: ${spoken(axes, under)}` : ''].filter(Boolean);
  const kids = [];
  // Grid: rings at 0.25, 0.5, 0.75 (one at 0.5 on a small multiple), the axes, the perfect outline.
  for (const v of mini ? [0.5] : [0.25, 0.5, 0.75]) kids.push(s('polygon', { class: 'grid', points: G.ring(n, v, g) }));
  if (!mini) {
    axes.forEach((a, i) => {
      const [x, y] = G.polar(i, n, 1, g);
      const gap = val(over, a.key) === null && val(under, a.key) === null;
      kids.push(s('line', { class: gap ? 'ax gap' : 'ax', x1: g.cx, y1: g.cy, x2: x, y2: y }));
    });
  }
  kids.push(s('polygon', { class: 'perfect', points: G.ring(n, 1, g) }));
  if (!mini) {
    kids.push(s('text', { class: 'gl', x: g.cx + 6, y: g.cy - g.r / 2 - 2 }, '0.5'), s('text', { class: 'gl', x: g.cx + 6, y: g.cy - g.r - 2 }, '1.0'));
    axes.forEach((a, i) => {
      const p = G.labelAt(i, n, g);
      const gap = val(over, a.key) === null;
      const tip = gap ? `${a.label}: not scored${over ? ` by ${over.name}` : ''}.\n${a.tip}` : `${a.label}\n${a.tip}`;
      const cls = gap ? 'al gap' : 'al';
      kids.push(s('g', { class: 'axl', 'data-tip': tip },
        s('text', { class: `${cls} long`, x: p.x, y: p.y, 'text-anchor': p.anchor }, a.label),
        s('text', { class: `${cls} short`, x: p.x, y: p.y, 'text-anchor': p.anchor }, a.short),
        gap ? s('text', { class: 'gn', x: p.x, y: p.y + (p.y < g.cy - g.r ? -18 : 18), 'text-anchor': p.anchor }, 'not scored') : null));
    });
  }
  if (mean) kids.push(shapeMarks(axes, mean, g, 'mean'));
  if (under) kids.push(shapeMarks(axes, under, g, 'under'));
  if (over) kids.push(shapeMarks(axes, over, g, 'over'));
  if (!mini) {
    const vertex = (side, cls, tipOf) => axes.map((a, i) => {
      const v = val(side, a.key);
      if (v === null) return null;
      const [x, y] = G.polar(i, n, v, g);
      return s('g', { class: 'vx', 'data-tip': tipOf(a, v) },
        s('circle', { class: 'hit', cx: x, cy: y, r: 12 }), s('circle', { class: cls, cx: x, cy: y, r: 5 }));
    });
    if (under) kids.push(vertex(under, 'vb', (a, v) => `${under.name}: ${a.label} ${v.toFixed(2)}\n${a.tip}`));
    if (over) {
      kids.push(vertex(over, 'v', (a, v) => {
        const was = val(under, a.key);
        const tail = under ? (was === null ? ' (earlier: not scored)' : ` (earlier ${was.toFixed(2)}, ${sign(v - was)})`) : '';
        return `${over.name}: ${a.label} ${v.toFixed(2)}${tail}\n${a.tip}`;
      }));
    }
  }
  return s('svg', { class: mini ? 'ev-star mini' : 'ev-star', viewBox: g.viewBox, role: 'img', 'aria-label': parts.join('; ') + '.' }, kids);
}

/** The legend under a chart. */
export function shapeKey(entries, cls) {
  return h('div', { class: cls ? `ev-key ${cls}` : 'ev-key' }, entries.map(e =>
    h('span', {}, e.mark ? h('i', { class: `ev-lnk ${e.mark}`, 'aria-hidden': 'true' }) : null, e.text)));
}

/** Per-axis readout: label, value, and with an earlier shape its value and the change. */
export function shapeList(axes, over, under) {
  const head = h('li', { class: 'hd' }, h('b', {}, 'Result'), h('span', {}, under ? 'Earlier, now' : 'Score'), under ? h('span', {}, 'Change') : null);
  return h('ul', { class: under ? 'ev-axl two' : 'ev-axl' }, head, axes.map(a => {
    const v = val(over, a.key), w = val(under, a.key);
    const d = v !== null && w !== null ? v - w : null;
    return h('li', { 'data-tip': `${a.label}\n${a.tip}` },
      h('span', { class: v === null ? 'k gap' : 'k' }, a.label),
      h('span', { class: G.isLow(v) ? 'n low' : 'n' }, under ? `${G.f2(w)}, ${G.f2(v)}` : G.f2(v)),
      under ? h('span', { class: d === null ? 'd' : d > 0.005 ? 'd p' : d < -0.005 ? 'd m' : 'd' }, d === null ? '–' : sign(d)) : null);
  }));
}

/** The numbers behind the shapes: one row per shape, values under 0.70 marked. */
export function shapeTable(axes, rows) {
  return h('table', { class: 'ev-ntable' },
    h('thead', {}, h('tr', {}, h('th', {}, 'Document'), axes.map(a => h('th', { 'data-tip': a.tip }, a.label)))),
    h('tbody', {}, rows.map(r => h('tr', { class: r.cls || null },
      h('td', {}, r.num ? h('span', { class: 'ev-num' }, String(r.num)) : null, h('span', { class: 't', dir: 'auto' }, r.name)),
      axes.map(a => {
        const v = r.values ? r.values[a.key] : null;
        const n = typeof v === 'number' ? v : null;
        return h('td', {}, G.isLow(n) ? h('span', { class: 'low' }, G.f2(n)) : n === null ? h('span', { class: 'ns' }, 'not scored') : G.f2(n));
      })))));
}

/** The Chart / Table segment for a card (evals.js handles the clicks). */
export function shapeToggle(id) {
  return h('div', { class: 'ev-seg', role: 'group', 'aria-label': 'View' },
    h('button', { type: 'button', 'data-evtog': id, 'data-mode': 'chart', 'aria-pressed': 'true' }, 'Chart'),
    h('button', { type: 'button', 'data-evtog': id, 'data-mode': 'table', 'aria-pressed': 'false' }, 'Table'));
}
