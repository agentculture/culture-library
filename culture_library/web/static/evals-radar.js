/* Evals radar page, #evals/radar (plan task t17; design canvas "Radar").
 *
 * Shows one run or a set of runs as a shape: the mean over its documents (one document per
 * run), laid over the earlier comparable run(s) when there are any, small multiples per
 * document over the mean, and the numbers table. Route parameters (hashroute.js radarParams):
 * runs=IDS picks the runs, vs=IDS | none what to lay under; without runs the page shows the
 * book sets' latest complete runs with the search setting most of them ran.
 *
 * Data, all read scope:
 *   - GET /api/eval/runs, /api/eval/sets: the pickers and the earlier-run search;
 *   - GET /api/eval/suites: metric definitions, the hover tips of the axes;
 *   - GET /api/eval/compare?before=&after=: checks.comparable for the earlier run(s);
 *   - GET /api/eval/radar?run(s)=...&vs=...: the shapes.
 * Item titles and kinds come from the item list app.js loaded. Never read an app.js import at
 * module top level: app.js imports evals.js, which imports this module.
 */
import { $, api, byKey, chip, fill, fmtN, h, kindKey } from './app.js';
import * as G from './evals-geom.js';
import * as M from './evals-model.js';
import { radarHash, radarParams } from './hashroute.js';
import { shapeChart, shapeKey, shapeList, shapeTable, shapeToggle } from './evals-shape.js';

const plural = (n, one, many) => `${fmtN(n)} ${n === 1 ? one : many}`;
const KIND_WORDS = { book: ['book', 'books'], newspaper: ['issue', 'issues'] };
function info(itemId) {
  const e = byKey('item-' + itemId);
  return e ? { title: e.title, kind: e.apiKind } : null;
}
const idList = runs => runs.map(r => r.id).join(', ');

let cur = null;
let drawing = 0;

/** Draw the radar for the route's params string. */
export async function draw(params) {
  const my = ++drawing;
  const body = $('ev-radar-body');
  const p = radarParams(params);
  let suites, sets, runs;
  try {
    [suites, sets, runs] = await Promise.all([api('/api/eval/suites'), api('/api/eval/sets'), api('/api/eval/runs')]);
  } catch (e) {
    if (my === drawing) fill(body, h('p', { class: 'empty' }, `The library did not answer (${e.detail || e.message}). Reload the page to try again.`));
    return;
  }
  const all = runs.runs || [], allSets = sets.sets || [];
  const groups = M.setGroups(allSets, all, info);
  let picked, sel, unknown = [];
  if (p.runs.length) {
    picked = p.runs.map(id => all.find(r => r.id === id)).filter(Boolean);
    unknown = p.runs.filter(id => !all.some(r => r.id === id));
    sel = M.selectionOf(picked, groups);
  } else {
    const dv = M.defaultView(all, groups);
    picked = dv ? M.pickRuns(all, dv.sets, dv.key).runs : [];
    sel = { scope: 'set', docs: dv ? dv.docs : 'custom', key: dv ? dv.key : 'mixed' };
  }
  if (!picked.length) {
    if (my !== drawing) return;
    cur = null;
    fill(body, h('p', { class: 'empty' }, p.runs.length
      ? `This key cannot see run ${p.runs.join(', ')}, or it does not exist. Pick documents on the Evals overview.`
      : 'No set has a complete run yet. Score one with culture-library eval run.'));
    return;
  }
  // What to lay under: the link's vs, nothing, or the earlier comparable run of every set.
  let under = null, note = null, checks = null, overlay = 'auto';
  if (p.vs === 'none') overlay = 'none';
  else if (Array.isArray(p.vs)) { overlay = 'link'; under = p.vs; }
  else {
    const e = M.earlierRuns(all, picked);
    if (e.missing.length) {
      const which = picked.length === 1 ? 'This set has' : `${M.setsText(e.missing).replace(/^s/, 'S')} ${e.missing.length === 1 ? 'has' : 'have'}`;
      note = `No earlier comparable run. ${which} no earlier complete run with the same search setting, suites and judge, so there is nothing to lay under ${picked.length === 1 ? 'it' : 'these runs'} yet. The next full run will show here as the shape to beat.`;
    } else under = e.runs.map(r => r.id);
  }
  const after = picked.map(r => r.id);
  if (under) {
    checks = await api(`/api/eval/compare?before=${under.join(',')}&after=${after.join(',')}`).then(r => r.checks).catch(() => null);
    if (overlay === 'auto' && !(checks && checks.comparable)) {
      note = `No earlier comparable run. Runs ${under.join(', ')} are the latest earlier runs of these sets, but GET /api/eval/compare says they cannot be compared${checks && checks.reasons.length ? `: ${checks.reasons.join('; ')}` : ''}.`;
      under = null;
    }
  }
  const q = after.length === 1 ? `run=${after[0]}` : `runs=${after.join(',')}`;
  let shape;
  try {
    shape = await api(`/api/eval/radar?${q}${under ? `&vs=${under.join(',')}` : ''}`);
  } catch (e) {
    if (my === drawing) fill(body, h('p', { class: 'empty' }, `The radar did not answer (${e.detail || e.message}).`));
    return;
  }
  if (my !== drawing) return;
  const a = shape.kind === 'pair' ? shape.a : shape;
  const b = shape.kind === 'pair' ? shape.b : null;
  const keys = [...Object.keys(a.axes || {}), ...Object.keys((b && b.axes) || {}).filter(k => !(k in (a.axes || {})))];
  const metrics = {};
  for (const su of suites.suites || []) for (const m of su.metrics || []) metrics[m.name] = m.definition;
  const underRuns = under ? under.map(id => all.find(r => r.id === id)).filter(Boolean) : [];
  cur = { all, allSets, groups, picked, sel, overlay, vs: p.vs };
  const view = { axes: G.axisList(keys, metrics, M.rankDepth([...(picked || []), ...underRuns])), a, b, picked, underRuns, under, checks, note, unknown, budget: shape.budget_s };
  fill(body, controls(cur), mainCard(view), minis(view), numbers(view));
  const cmp = $('ev-radar-compare');
  if (cmp) cmp.setAttribute('href', under ? `#evals/compare?before=${under.join(',')}&after=${after.join(',')}` : '#evals/compare');
}

/* ---------- helpers over the loaded lists ---------- */
function setOf(setId) { return cur.allSets.find(s => s.id === setId) || {}; }
function docInfo(setId) {
  const s = setOf(setId);
  return info(s.item_id) || { title: s.item_id ? `Item ${s.item_id}` : `Set ${setId}`, kind: null };
}
function kindsOf(runs) { return [...new Set(runs.map(r => docInfo(r.set_id).kind || 'document'))]; }
function groupName(sel) {
  const g = cur.groups.find(x => x.id === sel.docs);
  return g ? g.name : 'Runs from the link';
}

/* ---------- controls ---------- */
function select(attr, label, value, options) {
  return h('label', { class: 'ev-sel' }, label,
    h('select', { [attr]: '', 'aria-label': label },
      options.map(([v, text]) => h('option', { value: v, selected: v === value }, text))));
}
function controls(c) {
  const { sel } = c;
  const setsWithRuns = [...new Set(c.all.filter(r => r.status === 'complete').map(r => r.set_id))].sort((x, y) => x - y);
  const docOpts = sel.scope === 'one'
    ? setsWithRuns.map(id => [`set:${id}`, `${docInfo(id).title}, set ${id}`])
    : [...c.groups.map(g => [g.id, g.label]), ...(sel.docs === 'custom' ? [['custom', `Runs from the link, ${M.setsText(c.picked.map(r => r.set_id))}`]] : [])];
  const setIds = docSets(sel.docs);
  const cfgs = M.configsFor(c.all, setIds);
  const cfgOpts = cfgs.map(x => [x.key, `${M.configText(x.key)}${x.sets < setIds.length ? ` (${x.sets} of ${setIds.length} sets)` : ''}`]);
  if (sel.key === 'mixed') cfgOpts.unshift(['mixed', 'Mixed settings (from the link)']);
  const overOpts = [['auto', 'Earlier run with the same suites'], ['none', 'Nothing']];
  if (c.overlay === 'link') overOpts.unshift(['link', `${c.vs.length === 1 ? 'Run' : 'Runs'} ${c.vs.join(', ')} (from the link)`]);
  return h('div', { class: 'ev-controls' },
    h('div', { class: 'ev-seg', role: 'group', 'aria-label': 'Scope' },
      h('button', { type: 'button', 'data-evscope': 'one', 'aria-pressed': String(sel.scope === 'one') }, 'One document'),
      h('button', { type: 'button', 'data-evscope': 'set', 'aria-pressed': String(sel.scope === 'set') }, 'Set of documents')),
    select('data-evdocs', 'Documents', sel.docs, docOpts),
    select('data-evsearch', 'Search', sel.key, cfgOpts),
    select('data-evover', 'Lay over', c.overlay, overOpts));
}
function docSets(docs) {
  if (docs.startsWith('set:')) return [Number(docs.slice(4))];
  const g = cur.groups.find(x => x.id === docs);
  return g ? g.sets : [...new Set(cur.picked.map(r => r.set_id))];
}
/** Go to the radar for documents ``docs`` with search setting ``key`` (kept when the sets ran it). */
function go(docs, key, overlay) {
  const ids = docSets(docs);
  const cfgs = M.configsFor(cur.all, ids);
  const k = cfgs.some(x => x.key === key) ? key : cfgs.length ? cfgs[0].key : key;
  const runs = M.pickRuns(cur.all, ids, k).runs.map(r => r.id);
  if (!runs.length) return;
  const vs = overlay === 'none' ? 'none' : overlay === 'link' ? cur.vs : null;
  location.hash = radarHash({ runs, vs });
}

/* ---------- the shape card ---------- */
function runsLine(v) {
  const n = v.picked.length;
  const qs = (v.a.documents || []).reduce((t, d) => t + (d.questions || 0), 0);
  const cfg = M.configText(M.configKey(v.picked[0]));
  const mixed = new Set(v.picked.map(M.configKey)).size > 1;
  const who = n === 1 ? `Run ${v.picked[0].id}, set ${v.picked[0].set_id}, ${plural(qs, 'question', 'questions')}.` : `Mean of ${plural(n, 'document', 'documents')}, ${plural(qs, 'question', 'questions')}. Runs ${idList(v.picked)}.`;
  const over = v.under ? ` Laid over run${v.under.length === 1 ? '' : 's'} ${v.under.join(', ')}.` : '';
  return `${who} ${mixed ? 'Mixed search settings' : cfg.charAt(0).toUpperCase() + cfg.slice(1)}.${over}`;
}
function mainCard(v) {
  const n = v.picked.length;
  const kinds = kindsOf(v.picked);
  const rr = new Set(v.picked.map(r => (M.configKey(r).endsWith('|on') ? 'on' : 'off')));
  const rrText = rr.size === 1 ? `rerank ${[...rr][0]}` : 'mixed rerank';
  const title = n === 1 ? docInfo(v.picked[0].set_id).title : `${groupName(cur.sel)}, ${rrText}`;
  const overName = n === 1 ? `Run ${v.picked[0].id}` : `Mean of runs ${idList(v.picked)}`;
  const underName = v.under ? (v.under.length === 1 ? `Run ${v.under[0]}` : `Mean of runs ${v.under.join(', ')}`) : null;
  const over = { name: overName, values: v.a.axes };
  const under = v.b ? { name: underName, values: v.b.axes } : null;
  const chart = shapeChart({ axes: v.axes, over, under, label: `Shape of ${title}` });
  const notes = [];
  if (v.note) notes.push(v.note);
  if (v.under && v.checks && !v.checks.comparable) notes.push(`These runs are laid over each other as the link asks, but GET /api/eval/compare says they cannot be compared: ${v.checks.reasons.join('; ')}.`);
  if (v.under && v.checks && v.checks.warnings && v.checks.warnings.length) notes.push(`Compared with warnings: ${v.checks.warnings.join('; ')}.`);
  if (v.unknown.length) notes.push(`Left out: run ${v.unknown.join(', ')} (absent, or this key cannot see it).`);
  if (v.a.left_out) notes.push(`${plural(v.a.left_out, 'run', 'runs')} left out of the mean: this key cannot see ${v.a.left_out === 1 ? 'its item' : 'their items'}.`);
  const gaps = v.axes.filter(x => v.a.axes[x.key] === null || v.a.axes[x.key] === undefined).map(x => x.label.toLowerCase());
  const key = [];
  if (under) key.push({ mark: 'under', text: `Under: ${underName}` });
  key.push({ mark: 'over', text: under ? `Over: ${overName}` : 'This run' + (n === 1 ? '' : ', mean of its documents') });
  key.push({ mark: 'perfect', text: 'Perfect: 1 on every result' });
  const rows = [{ name: overName, values: v.a.axes, cls: 'mean' }];
  if (under) rows.push({ name: underName, values: v.b.axes, cls: 'under' });
  return h('section', { class: 'ev-card', 'aria-labelledby': 'ev-radar-h' },
    h('div', { class: 'ev-head' },
      h('div', {}, h('h2', { id: 'ev-radar-h', dir: 'auto' }, title), h('p', {}, runsLine(v))),
      h('div', { class: 'ev-headr' }, kinds.length === 1 ? chip(kindKey(kinds[0])) : null, shapeToggle('radar'))),
    h('div', { class: 'ev-viz' },
      h('div', { class: 'ev-starrow' }, chart, shapeList(v.axes, over, under)),
      shapeKey(key)),
    h('div', { class: 'ev-tbl', hidden: true }, shapeTable(v.axes, rows)),
    notes.map(t => h('p', { class: 'ev-note', dir: 'auto' }, t)),
    h('div', { class: 'ev-src' }, `${gaps.length ? `Not scored, drawn as gaps: ${gaps.join(', ')}. ` : ''}Each document weighs the same in the mean. Latency budget ${v.budget ?? 1} s. Hover a point or a result for its definition. Source: GET /api/eval/radar${v.under ? ' and GET /api/eval/compare' : ''}.`));
}

/* ---------- small multiples ---------- */
function docWord(runs, many) {
  const kinds = kindsOf(runs);
  const w = kinds.length === 1 ? KIND_WORDS[kinds[0]] : null;
  return w ? w[many ? 1 : 0] : many ? 'documents' : 'document';
}
function docRows(v) {
  return (v.a.documents || []).map((d, i) => ({ d, n: i + 1, title: docInfo(d.set_id).title }));
}
function minis(v) {
  const docs = docRows(v);
  if (docs.length < 2) return null;
  const meanName = `Mean of all ${docWord(v.picked, true)}`;
  return h('section', { class: 'ev-card', 'aria-labelledby': 'ev-minis-h' },
    h('div', { class: 'ev-head' }, h('div', {},
      h('h2', { id: 'ev-minis-h' }, `Shape of each ${docWord(v.picked, false)}`),
      h('p', {}, `Same axes and scale as above. The dashed outline is the mean of all ${docs.length}.`))),
    h('div', { class: 'ev-minis' }, docs.map(({ d, n, title }) => h('figure', {
      class: 'ev-mini',
      'data-tip': `${title}, run ${d.run_id}, set ${d.set_id}, ${plural(d.questions, 'question', 'questions')}\n${v.axes.map(x => `${x.label} ${G.f2(d.axes[x.key])}`).join('\n')}`,
    },
    h('figcaption', {}, h('b', {}, h('span', { class: 'ev-num' }, String(n)), h('span', { class: 't', dir: 'auto' }, title)),
      h('small', {}, `Run ${d.run_id}, set ${d.set_id}, ${plural(d.questions, 'question', 'questions')}`)),
    shapeChart({ axes: v.axes, over: { name: `Run ${d.run_id}`, values: d.axes }, mean: { name: meanName, values: v.a.axes }, mini: true, label: title })))),
    shapeKey([{ mark: 'over', text: `This ${docWord(v.picked, false)}` }, { mark: 'mean', text: meanName }, { mark: 'perfect', text: 'Perfect' },
      { text: `Axes clockwise from the top: ${v.axes.map(x => x.label.toLowerCase()).join(', ')}` }], 'start'));
}

/* ---------- numbers ---------- */
function numbers(v) {
  const docs = docRows(v);
  const rows = docs.map(({ d, n, title }) => ({ name: `${title} (run ${d.run_id})`, num: docs.length > 1 ? n : null, values: d.axes }));
  if (docs.length > 1) rows.push({ name: `Mean of all ${docWord(v.picked, true)}`, values: v.a.axes, cls: 'mean' });
  if (v.b) {
    rows.push({ name: `Earlier: ${v.under.length === 1 ? `run ${v.under[0]}` : `mean of runs ${v.under.join(', ')}`}`, values: v.b.axes, cls: 'under' });
  }
  return h('section', { class: 'ev-card', 'aria-labelledby': 'ev-nums-h' },
    h('div', { class: 'ev-head' }, h('div', {}, h('h2', { id: 'ev-nums-h' }, 'The numbers behind the shapes'), h('p', {}, 'Every shape on this page as a table.'))),
    h('div', { class: 'ev-tablewrap' }, shapeTable(v.axes, rows)),
    h('div', { class: 'ev-src' }, 'Values under 0.70 are marked. Found at 1 and 3 and MRR come from the ranked results each run already stores, so older runs have them without a re-run. Each new suite adds its own axes. Source: GET /api/eval/radar.'));
}

/* ---------- events ---------- */
export function init() {
  const page = $('evals-radar');
  page.addEventListener('click', e => {
    const b = e.target.closest('[data-evscope]');
    if (!b || !cur || b.getAttribute('aria-pressed') === 'true') return;
    const first = cur.picked[0].set_id;
    if (b.dataset.evscope === 'one') go(`set:${first}`, cur.sel.key, cur.overlay === 'link' ? 'auto' : cur.overlay);
    else {
      const kind = docInfo(first).kind;
      const g = cur.groups.find(x => x.id === kind) || cur.groups[0];
      if (g) go(g.id, cur.sel.key, cur.overlay === 'link' ? 'auto' : cur.overlay);
    }
  });
  page.addEventListener('change', e => {
    const t = e.target;
    if (!cur || t.tagName !== 'SELECT') return;
    if (t.hasAttribute('data-evdocs')) go(t.value, cur.sel.key, cur.overlay === 'link' ? 'auto' : cur.overlay);
    else if (t.hasAttribute('data-evsearch')) go(cur.sel.docs === 'custom' ? 'custom' : cur.sel.docs, t.value, cur.overlay === 'link' ? 'auto' : cur.overlay);
    else if (t.hasAttribute('data-evover')) {
      const runs = cur.picked.map(r => r.id);
      location.hash = radarHash({ runs, vs: t.value === 'none' ? 'none' : t.value === 'link' ? cur.vs : null });
    }
  });
}
