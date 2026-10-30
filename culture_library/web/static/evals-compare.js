/* Evals compare page, #evals/compare (plan task t18; design canvas "Compare").
 *
 * Two runs, or two groups of runs paired by set, side by side: the headline question names
 * what differs between the sides ("Did turning rerank on help?"), then the comparability checks
 * (before any change is shown), the headline change in found-in-top-5 with a tally of the
 * results that got better, worse or stayed the same, the shape of the change (the after shape,
 * solid, over the before shape, dashed; evals-shape.js) beside its results sorted by change,
 * the change per metric, and the questions that changed, worse first. When GET
 * /api/eval/compare says the runs cannot be compared, the page shows its reasons and no change.
 *
 * Route parameters (hashroute.js compareParams): before=IDS&after=IDS. With after alone, the
 * before side is the latest earlier comparable run of each set (evals-model.js earlierRuns);
 * with neither, evals-model.js defaultCompare picks: the book sets' latest rerank-off runs
 * against their rerank-on runs, else the newest run against its earlier comparable run.
 *
 * Data, all read scope:
 *   - GET /api/eval/runs, /api/eval/sets: the pickers, names and settings;
 *   - GET /api/eval/suites: metric definitions (hover tips) and which suite owns each metric;
 *   - GET /api/eval/compare?before=&after=: checks, both shapes, deltas, changed questions.
 * Item titles come from the item list app.js loaded. Question text and reasons are content:
 * text nodes with dir="auto", never markup. Never read an app.js import at module top level:
 * app.js imports evals.js, which imports this module.
 */
import { $, SVG, api, byKey, fill, fmtN, h, icon } from './app.js';
import * as G from './evals-geom.js';
import * as M from './evals-model.js';
import { compareHash, compareParams } from './hashroute.js';
import { shapeChart, shapeKey, shapeList, shapeTable, shapeToggle } from './evals-shape.js';

const UP = '<svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const DOWN = '<svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14m-6-6 6 6 6-6" fill="none" stroke="var(--ev-warn)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const SAME = '<svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" fill="none" stroke="var(--muted)" stroke-width="2.5" stroke-linecap="round"/></svg>';
const ARROW = '<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const QUESTIONS_SHOWN = 8;
const BAR_SCALE = 0.25; // a change of 0.25 fills half the bar

const plural = (n, one, many) => `${fmtN(n)} ${n === 1 ? one : many}`;
const sign = d => (d > 0.005 ? '+' : d < -0.005 ? '−' : '') + Math.abs(d).toFixed(2);
const cls = d => (d === null ? '' : d > 0.005 ? 'p' : d < -0.005 ? 'm' : '');
const ids = runs => runs.map(r => r.id).join(', ');
function info(itemId) {
  const e = byKey('item-' + itemId);
  return e ? { title: e.title, kind: e.apiKind } : null;
}

let cur = null;
let drawing = 0;

/** Draw the compare page for the route's params string. */
export async function draw(params) {
  const my = ++drawing;
  const body = $('ev-compare-body');
  const p = compareParams(params);
  let suites, sets, runs;
  try {
    [suites, sets, runs] = await Promise.all([api('/api/eval/suites'), api('/api/eval/sets'), api('/api/eval/runs')]);
  } catch (e) {
    if (my === drawing) fill(body, h('p', { class: 'empty' }, `The library did not answer (${e.detail || e.message}). Reload the page to try again.`));
    return;
  }
  const all = runs.runs || [], allSets = sets.sets || [];
  const groups = M.setGroups(allSets, all, info);
  const find = list => list.map(id => all.find(r => r.id === id)).filter(Boolean);
  let before = [], after = [], note = null;
  if (p.after.length && p.before.length) {
    before = find(p.before); after = find(p.after);
  } else if (p.after.length) {
    after = find(p.after);
    const e = M.earlierRuns(all, after);
    before = e.runs;
    if (e.missing.length) note = `No earlier comparable run for ${M.setsText(e.missing)}: pick the before side above.`;
  } else {
    const d = M.defaultCompare(all, groups);
    if (d) {
      before = find(d.before); after = find(d.after);
      const g = groups.find(x => x.id === d.group);
      note = d.why === 'rerank'
        ? `No runs named in the link, so this is the default: the latest rerank-off run of each ${g ? g.name.replace(/^All /, '').replace(/s$/, '') : 'document'} set against its latest rerank-on run with the same suites and judge. Pick other runs above.`
        : `No runs named in the link, so this is the default: the newest run against its earlier comparable run. Pick other runs above.`;
      if (p.before.length) note = `The link names only the before side. ${note}`;
    }
  }
  cur = { all, allSets, groups, before, after };
  const metrics = {}, suiteOf = {};
  for (const su of suites.suites || []) for (const m of su.metrics || []) { metrics[m.name] = m.definition; suiteOf[m.name] = su.name; }
  if (!before.length || !after.length) {
    if (my !== drawing) return;
    intro(null);
    const missing = [...p.before, ...p.after].filter(id => !all.some(r => r.id === id));
    pickers();
    fill(body, h('p', { class: 'empty' }, missing.length
      ? `This key cannot see run ${missing.join(', ')}, or it does not exist. Pick runs above.`
      : all.some(r => r.status === 'complete') ? 'Pick a before and an after run above.' : 'No set has a complete run yet. Score one with culture-library eval run.'));
    return;
  }
  let cmp;
  try {
    cmp = await api(`/api/eval/compare?before=${before.map(r => r.id).join(',')}&after=${after.map(r => r.id).join(',')}`);
  } catch (e) {
    if (my !== drawing) return;
    intro(null);
    pickers();
    fill(body, h('p', { class: 'empty' }, `The comparison did not answer (${e.detail || e.message}).`));
    return;
  }
  if (my !== drawing) return;
  const q = M.compareQuestion(before, after);
  intro(q);
  const rows = M.checkRows(before, after, cmp.checks, cmp.left_out);
  const notes = note ? h('p', { class: 'ev-note', dir: 'auto' }, note) : null;
  if (!cmp.checks || !cmp.checks.comparable) {
    pickers();
    fill(body, notes, checksCard(rows, false),
      h('p', { class: 'ev-foot' }, `Source: GET /api/eval/compare for ${sideName(before)} and ${sideName(after)}. Runs scored with different suites or judges measure different things, so no change is shown.`));
    return;
  }
  const keys = [...Object.keys(cmp.after || {}), ...Object.keys(cmp.before || {}).filter(k => !(k in (cmp.after || {})))];
  const view = { axes: G.axisList(keys, metrics), suiteOf, cmp, before, after, q };
  pickers();
  fill(body, notes,
    h('div', { class: 'ev-cmprow' }, checksCard(rows, true), verdict(view)),
    shapeCard(view), metricCard(view), questionsCard(view),
    h('p', { class: 'ev-foot' }, `Source: GET /api/eval/compare for ${sideName(before)} and ${sideName(after)}. Each result keeps the question text and set version it scored, so editing a question later never rewrites this page.`));
}

/* ---------- names ---------- */
function setOf(setId) { return cur.allSets.find(s => s.id === setId) || {}; }
function docTitle(setId) {
  const s = setOf(setId);
  return (info(s.item_id) || {}).title || (s.item_id ? `Item ${s.item_id}` : `Set ${setId}`);
}
/** A side's search setting for the key: just "rerank off" when rerank is all that differs. */
function setting(runs, q) {
  if (new Set(runs.map(M.configKey)).size > 1) return 'mixed search settings';
  const text = M.configText(M.configKey(runs[0]));
  return q.diffs.length === 1 && q.diffs[0].what.startsWith('turning rerank') ? text.replace(/^.*, (rerank \w+)$/, '$1') : text;
}
const sideName = runs => (runs.length === 1 ? `run ${runs[0].id}` : `runs ${ids(runs)}`);
/** "pg31547 images 3" for one set, "all books" when the sets are a whole group, else "sets 1–3". */
function docsName(runs) {
  const sids = [...new Set(runs.map(r => r.set_id))].sort((a, b) => a - b);
  if (sids.length === 1) return docTitle(sids[0]);
  const g = cur.groups.find(x => x.sets.join(',') === sids.join(','));
  return g ? g.name.toLowerCase() : M.setsText(sids);
}
function how(q, before, after) {
  if (q.diffs.length === 1 && q.diffs[0].what.startsWith('turning rerank')) {
    return q.diffs[0].what.endsWith('on') ? 'searched without and with the reranker' : 'searched with and without the reranker';
  }
  if (!q.diffs.length && q.settings.startsWith('No setting')) return `searched the same way (${M.configText(M.configKey(after[0]))})`;
  if (q.diffs.length) return `searched with ${M.configText(M.configKey(before[0]))}, then ${M.configText(M.configKey(after[0]))}`;
  return 'with mixed search settings';
}
function intro(q) {
  const { before, after } = cur;
  $('ev-cmp-title').textContent = q ? q.question : 'Compare runs';
  const lede = $('ev-cmp-lede');
  if (!q) { lede.textContent = 'Lay one run, or a set of runs, over another and see what changed: checks first, then the shape of the change and the questions that moved.'; return; }
  const n = before.reduce((t, r) => t + (r.total || 0), 0);
  const same = [...new Set(before.map(r => r.set_id))].sort().join() === [...new Set(after.map(r => r.set_id))].sort().join();
  const who = `${sideName(before).replace(/^r/, 'R')} against ${sideName(after)}`;
  lede.textContent = same
    ? `${who}: the same ${plural(n, 'question', 'questions')} from ${docsName(before)}, ${how(q, before, after)}.`
    : `${who}: ${docsName(before)} against ${docsName(after)}, ${how(q, before, after)}.`;
}

/* ---------- pickers ---------- */
function picker(side, label, runs, options) {
  const want = runs.map(r => r.id);
  let value = M.optionFor(want, options);
  const opts = [...options];
  if (!value && want.length) {
    value = 'cur';
    opts.unshift({ value, label: `${want.length === 1 ? 'Run' : 'Runs'} ${want.join(', ')}`, group: null });
  }
  if (!value) { value = ''; opts.unshift({ value, label: 'Pick runs', group: null }); }
  const byGroup = new Map();
  for (const o of opts) { if (!byGroup.has(o.group)) byGroup.set(o.group, []); byGroup.get(o.group).push(o); }
  const opt = o => h('option', { value: o.value, selected: o.value === value }, o.label);
  return h('label', { class: 'ev-sel' }, label,
    h('select', { 'data-evcmp': side, 'aria-label': `${label} run` },
      [...byGroup].map(([g, list]) => (g ? h('optgroup', { label: g }, list.map(opt)) : list.map(opt)))));
}
/** Fill the intro's before / after pickers (they live in the page intro, not the body). */
function pickers() {
  const options = M.compareOptions(cur.all, cur.groups, info, cur.allSets);
  cur.options = options;
  fill($('ev-cmp-pick'), picker('before', 'Before', cur.before, options), h('span', { class: 'ev-arrow' }, icon(ARROW)), picker('after', 'After', cur.after, options));
}

/* ---------- checks and the verdict ---------- */
function checksCard(rows, ok) {
  const mark = { ok: SVG.good, warn: SVG.warning, fail: SVG.critical };
  const word = { ok: 'Holds', warn: 'Warning', fail: 'Fails' };
  return h('section', { class: ok ? 'ev-card ev-checkcard' : 'ev-card ev-checkcard bad', 'aria-labelledby': 'ev-checks-h' },
    h('div', { class: 'ev-head' }, h('div', {},
      h('h2', { id: 'ev-checks-h' }, ok ? 'Can these runs be compared?' : 'These runs cannot be compared'),
      h('p', {}, ok ? 'Checked before any change is shown.' : 'GET /api/eval/compare refuses to compare them, so no change is shown. Pick runs scored with the same suites and judge.'))),
    h('ul', { class: 'ev-checks' }, rows.map(r => h('li', { class: r.state },
      h('span', { class: 'ic', role: 'img', 'aria-label': word[r.state] }, icon(mark[r.state])),
      h('span', { class: 'tx', dir: 'auto' }, r.text, r.detail ? h('small', { dir: 'auto' }, r.detail) : null)))));
}
function pill(svg, text, tip) { return h('span', { class: 'ev-pill', 'data-tip': tip || null }, icon(svg), text); }
function verdict(v) {
  const { cmp, axes } = v;
  const main = axes.find(a => a.key === 'found_at_5' && typeof cmp.after[a.key] === 'number') || axes.find(a => typeof cmp.after[a.key] === 'number' && typeof cmp.before[a.key] === 'number');
  const t = M.tally(axes.map(a => a.key), cmp.deltas);
  const name = k => axes.find(a => a.key === k).label.toLowerCase();
  const shift = M.foundShift(cmp.changed_questions);
  const found = shift.gained || shift.lost
    ? `${shift.gained ? `${plural(shift.gained, 'more question', 'more questions')} found` : 'No more questions found'}, ${shift.lost ? `${fmtN(shift.lost)} lost` : 'none lost'}.`
    : 'The same questions found in the top 5.';
  const label = main ? (main.key === 'found_at_5' ? 'found in top 5' : main.label.toLowerCase()) : '';
  const list = ks => (ks.length && ks.length <= 3 ? ` (${ks.map(name).join(', ')})` : '');
  return h('section', { class: 'ev-card ev-verdict', 'aria-label': 'The change' },
    main
      ? h('div', { class: 'big' }, `${G.f2(cmp.before[main.key])} → ${G.f2(cmp.after[main.key])} `, h('small', {}, label))
      : h('div', { class: 'big' }, 'No shared score'),
    h('p', {}, found),
    h('div', { class: 'ev-tally' },
      pill(UP, `Better on ${plural(t.better.length, 'result', 'results')}${list(t.better)}`, t.better.map(name).join(', ') || null),
      pill(DOWN, `Worse on ${fmtN(t.worse.length)}${list(t.worse)}`, t.worse.map(name).join(', ') || null),
      pill(SAME, `Same on ${fmtN(t.same.length)}`, t.same.length ? `Within 0.005: ${t.same.map(name).join(', ')}` : null),
      t.unscored.length ? h('span', { class: 'ev-pill ns', 'data-tip': t.unscored.map(name).join(', ') }, `Not scored on both: ${fmtN(t.unscored.length)}`) : null));
}

/* ---------- the shape of the change ---------- */
function shapeCard(v) {
  const { cmp, axes, before, after } = v;
  const bName = `${sideName(before)}, ${setting(before, v.q)}`, aName = `${sideName(after)}, ${setting(after, v.q)}`;
  const under = { name: bName.replace(/^r/, 'R'), values: cmp.before };
  const over = { name: aName.replace(/^r/, 'R'), values: cmp.after };
  const sorted = M.byChange(axes.map(a => a.key), cmp.deltas).map(k => axes.find(a => a.key === k));
  const t = M.tally(axes.map(a => a.key), cmp.deltas);
  const top = t.better.length ? sorted[0] : null;
  const low = t.worse.length ? sorted.filter(a => typeof cmp.deltas[a.key] === 'number').slice(-1)[0] : null;
  const summary = ['Sorted by change.'];
  if (top) summary.push(`Biggest gain: ${top.label.toLowerCase()} (${sign(cmp.deltas[top.key])}).`);
  if (low) summary.push(`Biggest drop: ${low.label.toLowerCase()} (${sign(cmp.deltas[low.key])}).`);
  const gaps = axes.filter(a => typeof cmp.after[a.key] !== 'number').map(a => a.label.toLowerCase());
  return h('section', { class: 'ev-card ev-cmp', 'aria-labelledby': 'ev-shape-h' },
    h('div', { class: 'ev-head' },
      h('div', {}, h('h2', { id: 'ev-shape-h' }, 'Shape of the change'),
        h('p', {}, 'Each result is one axis, 0 at the centre and 1 at the edge. The after shape is laid over the before shape: where it reaches further out, the change helped.')),
      shapeToggle('cmp-shape')),
    h('div', { class: 'ev-viz' },
      h('div', { class: 'ev-starrow' },
        shapeChart({ axes, over, under, was: 'before', label: `Shape of the change, ${sideName(before)} dashed and ${sideName(after)} solid` }),
        shapeList(sorted, over, under, { first: 'Metric', pair: 'Before, after' })),
      shapeKey([{ mark: 'under', text: `Before: ${bName}` }, { mark: 'over', text: `After: ${aName}` }, { mark: 'perfect', text: 'Perfect' }, { text: summary.join(' ') }])),
    h('div', { class: 'ev-tbl', hidden: true }, shapeTable(axes, [{ name: under.name, values: cmp.before, cls: 'under' }, { name: over.name, values: cmp.after, cls: 'mean' }])),
    h('div', { class: 'ev-src' }, `${gaps.length ? `Not scored, drawn as gaps: ${gaps.join(', ')}. ` : ''}${before.length > 1 ? 'Each side is the mean over its documents, each document weighing the same. ' : ''}Latency budget ${cmp.budget_s ?? 1} s. Hover a point or a result for its definition. Source: GET /api/eval/compare.`));
}

/* ---------- change per metric ---------- */
function bar(d) {
  const w = d === null ? 0 : Math.min(50, (Math.abs(d) / BAR_SCALE) * 50);
  return h('div', { class: 'ev-dbar', 'aria-hidden': 'true' },
    w > 0 && cls(d) ? h('span', { class: cls(d), style: { 'inline-size': `${w}%` } }) : null);
}
function metricCard(v) {
  const { cmp, axes, before, after, suiteOf } = v;
  const suite = a => { const s = suiteOf[a.metric]; return s ? M.suiteTitle(s).replace(/^./, c => c.toUpperCase()) : '–'; };
  return h('section', { class: 'ev-card', 'aria-labelledby': 'ev-metric-h' },
    h('div', { class: 'ev-head' }, h('div', {}, h('h2', { id: 'ev-metric-h' }, 'Change per metric'),
      h('p', {}, `${sideName(before).replace(/^r/, 'R')}, then ${sideName(after)}.`))),
    h('div', { class: 'ev-tablewrap' }, h('table', { class: 'ev-ctable' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Metric'), h('th', { class: 'su' }, 'Suite'), h('th', { class: 'n' }, 'Before'), h('th', { class: 'n' }, 'After'), h('th', { class: 'n' }, 'Change'), h('th', { class: 'br' }, h('span', { class: 'sr' }, 'Change bar')))),
      h('tbody', {}, axes.map(a => {
        const d = typeof cmp.deltas[a.key] === 'number' ? cmp.deltas[a.key] : null;
        return h('tr', { 'data-tip': `${a.label}\n${a.tip}` },
          h('td', {}, a.label), h('td', { class: 'su' }, suite(a)),
          h('td', { class: 'n' }, G.f2(cmp.before[a.key])), h('td', { class: 'n' }, G.f2(cmp.after[a.key])),
          h('td', { class: 'n' }, d === null ? '–' : h('span', { class: `ev-delta ${cls(d)}` }, sign(d))),
          h('td', { class: 'br' }, bar(d)));
      })))),
    h('div', { class: 'ev-src' }, `Bars share one scale: half the bar width is a change of ${BAR_SCALE}. Source: GET /api/eval/compare.`));
}

/* ---------- changed questions ---------- */
const foundWord = v => (typeof v !== 'number' ? 'not scored' : v >= 0.5 ? 'found' : 'missed');
const rankWord = mrr => { const r = M.rankOf(mrr); return r === null ? 'none' : String(r); };
function pairText(label, b, a) {
  return b === a ? `${label}: ${b}, ${a}` : [`${label}: ${b} → `, h('b', {}, a)];
}
function qRow(q, n, many) {
  const b = q.before || {}, a = q.after || {};
  const tip = ['found_at_1', 'found_at_3', 'found_at_5', 'mrr'].map(k => `${k.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase())}: ${G.f2(b[k])} → ${G.f2(a[k])}`).join('\n');
  return h('li', { hidden: n >= QUESTIONS_SHOWN || null, 'data-more': n >= QUESTIONS_SHOWN ? '' : null, 'data-tip': tip },
    h('span', { class: 'qt' }, h('b', { class: 't', dir: 'auto' }, q.question || 'Question without stored text'),
      many && q.set_id ? h('small', { dir: 'auto' }, `${docTitle(q.set_id)}, set ${q.set_id}`) : null),
    h('span', { class: 'c' }, pairText('Found', foundWord(b.found_at_5), foundWord(a.found_at_5))),
    h('span', { class: 'c' }, pairText('First at rank', rankWord(b.mrr), rankWord(a.mrr))));
}
function questionsCard(v) {
  const { cmp, before, after } = v;
  const changed = cmp.changed_questions || [];
  const g = M.questionGroups(changed);
  const many = new Set(before.map(r => r.set_id)).size > 1;
  const paired = new Set(after.map(r => r.set_id));
  const total = before.filter(r => paired.has(r.set_id)).reduce((t, r) => t + (r.total || 0), 0);
  const hiddenCount = Math.max(0, g.worse.length - QUESTIONS_SHOWN) + Math.max(0, g.better.length - QUESTIONS_SHOWN);
  const head = h('div', { class: 'ev-head' }, h('div', {}, h('h2', { id: 'ev-q-h' }, 'Questions that changed'),
    h('p', {}, 'Worse first. Found is whether the reference passage reached the top 5; first at rank is where the first reference passage came.')));
  if (!changed.length) return h('section', { class: 'ev-card', 'aria-labelledby': 'ev-q-h' }, head, h('p', { class: 'empty' }, 'No question changed: every question was found at the same rank on both sides.'));
  return h('section', { class: 'ev-card', 'aria-labelledby': 'ev-q-h' }, head,
    g.worse.length ? [h('h3', { class: 'ev-grp' }, icon(DOWN), `Worse on at least one result: ${fmtN(g.worse.length)}`),
      h('ul', { class: 'ev-q' }, g.worse.map((q, n) => qRow(q, n, many)))] : null,
    g.better.length ? [h('h3', { class: 'ev-grp' }, icon(UP), `Better: ${fmtN(g.better.length)}`),
      h('ul', { class: 'ev-q' }, g.better.map((q, n) => qRow(q, n, many)))] : null,
    h('div', { class: 'ev-more' },
      h('span', {}, `${total ? `Unchanged: ${fmtN(Math.max(0, total - changed.length))} of ${plural(total, 'question', 'questions')}. ` : ''}Source: GET /api/eval/compare.`),
      hiddenCount ? h('button', { type: 'button', class: 'btn', 'data-evqmore': '', 'aria-expanded': 'false' }, `Show all ${plural(changed.length, 'changed question', 'changed questions')}`) : null));
}

/* ---------- events ---------- */
export function init() {
  const page = $('evals-compare');
  page.addEventListener('change', e => {
    const t = e.target;
    if (!cur || t.tagName !== 'SELECT' || !t.hasAttribute('data-evcmp')) return;
    const o = (cur.options || []).find(x => x.value === t.value);
    if (!o) return;
    const side = t.dataset.evcmp;
    const before = side === 'before' ? o.ids : cur.before.map(r => r.id);
    const after = side === 'after' ? o.ids : cur.after.map(r => r.id);
    location.hash = compareHash({ before, after });
  });
  page.addEventListener('click', e => {
    const b = e.target.closest('[data-evqmore]');
    if (!b) return;
    const open = b.getAttribute('aria-expanded') !== 'true';
    b.closest('.ev-card').querySelectorAll('[data-more]').forEach(li => { li.hidden = !open; });
    b.setAttribute('aria-expanded', String(open));
    b.textContent = open ? 'Show the first questions only' : `Show all ${plural(b.closest('.ev-card').querySelectorAll('.ev-q li').length, 'changed question', 'changed questions')}`;
  });
}
