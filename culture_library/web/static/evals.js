/* Evals tab (plan task t16; design canvas "Evals overview", desktop and phone boards).
 *
 * Pages: the overview (#evals), the radar (#evals/radar, evals-radar.js, plan task t17) and
 * the compare page (#evals/compare, evals-compare.js, plan task t18). Run ids in the runs table and the rerank
 * dumbbell rows link to the radar (hashroute.js radarHash). The overview reads only the eval API:
 *   - GET /api/eval/runs and GET /api/eval/sets: every run and set this key can see;
 *   - GET /api/eval/radar?runs=...: per-run axes (found_at_5, judge_precision, judge_recall);
 *   - GET /api/eval/weakest?runs=...: per-item and per-source rows, weakest first;
 *   - GET /api/eval/suites: the suite catalog (metric definitions become hover tips).
 * Item titles and kinds come from the item list app.js already loaded (GET /api/library).
 * Which runs stand for a set is evals-model.js latestComplete(): the latest complete run per
 * set and rerank setting.
 *
 * Charts follow the Site state pattern (state.js): data-tip hover tips, a Table toggle and a
 * Source line. Content text (titles, URLs, run reasons) reaches the page as text nodes via h(),
 * with dir="auto"; no markup is parsed here except by app.js icon(), on this module's constant
 * SVG strings. Positions are set with logical properties (inset-inline-start, inline-size).
 */
import { $, SVG, api, byKey, chip, fill, fmtN, h, icon, kindKey } from './app.js';
import * as compare from './evals-compare.js';
import * as M from './evals-model.js';
import * as radar from './evals-radar.js';
import { radarHash } from './hashroute.js';

const PAGES = ['overview', 'radar', 'compare'];
const RUNS_SHOWN = 10;
const RUNNING = '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--muted)" stroke-width="1.8" stroke-dasharray="3 2.4"/></svg>';
const SUITE_TITLES = {
  id_retrieval: 'ID retrieval', judged: 'Judged retrieval', result_quality: 'Result quality',
  speed: 'Speed and errors', freshness: 'Freshness',
};
const ID_PRECISION = 'id_based_context_precision';

const f2 = v => (v === null || v === undefined || Number.isNaN(v) ? '–' : v.toFixed(2));
const score = v => (v === null || v === undefined ? 'no score' : v.toFixed(2));
const plural = (n, one, many) => `${fmtN(n)} ${n === 1 ? one : many}`;
const human = name => { const s = String(name).replaceAll('_', ' '); return s.charAt(0).toUpperCase() + s.slice(1); };
// Metric names a plain "snake case to words" turns into the wrong word (acronyms, counts).
const METRIC_LABELS = {
  mrr: 'MRR', ndcg: 'nDCG', precision_at_k: 'Precision at k',
  latency_p50_ms: 'Latency p50 (ms)', latency_p95_ms: 'Latency p95 (ms)',
  fresh_count: 'Fresh', stale_count: 'Stale', fresh_unknown_count: 'Unknown',
  fresh_na_count: 'Not applicable',
};
const metricLabel = name => METRIC_LABELS[name] || human(name);
const utc = iso => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'UTC' });
};
/** An item's title and API kind from the loaded library list (null when this key cannot see it). */
function info(itemId) {
  const e = byKey('item-' + itemId);
  return e ? { title: e.title, kind: e.apiKind } : null;
}
const kindChip = apiKind => chip(kindKey(apiKind || 'document'));

/* ---------- pages ---------- */
let shownPage = null;
export function show(page, params = '') {
  for (const p of PAGES) $('evals-' + p).hidden = p !== page;
  // A link from the overview's runs table opens the radar at its top; a picker change on the
  // radar itself keeps the scroll position.
  if (shownPage && shownPage !== page) window.scrollTo({ top: 0 });
  shownPage = page;
  if (page === 'overview') void draw();
  if (page === 'radar') void radar.draw(params);
  if (page === 'compare') void compare.draw(params);
}

/* ---------- data ---------- */
const ui = { suites: 'all', runs: 'all', allRuns: false };
let last = null;

/** Per-run axes and question counts from GET /api/eval/radar, 200 runs per request. */
async function shapes(ids) {
  const axes = new Map(), questions = new Map();
  const parts = [];
  for (let n = 0; n < ids.length; n += 200) parts.push(ids.slice(n, n + 200));
  const replies = await Promise.all(parts.map(p => api(`/api/eval/radar?runs=${p.join(',')}`).catch(() => null)));
  for (const r of replies) {
    for (const d of r?.documents || []) { axes.set(d.run_id, d.axes || {}); questions.set(d.run_id, d.questions); }
  }
  return { axes, questions };
}

let drawing = 0;
export async function draw() {
  const my = ++drawing;
  let suites, sets, runs;
  try {
    [suites, sets, runs] = await Promise.all([api('/api/eval/suites'), api('/api/eval/sets'), api('/api/eval/runs')]);
  } catch (e) {
    if (my !== drawing) return;
    $('ev-lede').textContent = `The library did not answer (${e.detail || e.message}). Reload the page to try again.`;
    for (const id of ['ev-tiles', 'ev-rerank', 'ev-weak', 'ev-suites', 'ev-runs']) fill($(id));
    return;
  }
  const all = runs.runs || [];
  const { axes, questions } = await shapes(all.map(r => r.id));
  const picked = M.latestComplete(all);
  const rows = M.rerankRows(sets.sets || [], picked, axes, questions, info);
  // Weakest: the latest complete rerank-on run per set; rerank-off runs only when no set has one.
  const on = [...picked.values()].map(p => p.on).filter(Boolean);
  const weakRuns = (on.length ? on : [...picked.values()].map(p => p.off).filter(Boolean)).slice(0, 200);
  const weak = weakRuns.length
    ? await api(`/api/eval/weakest?runs=${weakRuns.map(r => r.id).join(',')}&limit=${Math.max(10, weakRuns.length)}`).catch(() => null)
    : null;
  if (my !== drawing) return;
  last = { suites: suites.suites || [], sets: sets.sets || [], runs: all, axes, questions, picked, rows, weak, weakOn: on.length > 0 };
  $('ev-lede').textContent = "Does search hand an agent the passage its question needs? Each number is a scored run against question sets made from the library's own books and newspapers.";
  fill($('ev-tiles'), tiles(last));
  fill($('ev-rerank'), rerankCard(last));
  fill($('ev-weak'), weakCard(last));
  drawSuites();
  drawRuns();
}

/* ---------- headline tiles ---------- */
function tile(label, value, extra, sub, tip) {
  return h('div', { class: 'ev-tile', 'data-tip': tip },
    h('div', { class: 'l' }, label), h('div', { class: 'v' }, value), extra, h('div', { class: 'd' }, sub));
}
function kindTile(rows, kind, word) {
  const t = M.headline(rows, kind);
  const sub = t.sets
    ? [h('b', {}, f2(t.off)), ` without rerank, ${plural(t.sets, 'set', 'sets')}, ${plural(t.questions, 'question', 'questions')}`]
    : `No ${word} set has a complete run yet`;
  const tip = `Share of questions whose reference passage is in the top 5 results (ID-based, no judge). Mean over ${word} sets of each set's latest complete rerank-on run (${t.onSets} of ${t.sets}); the smaller number is the same with rerank off (${t.offSets} of ${t.sets}).`;
  return tile([kindChip(kind), 'Found in top 5'], f2(t.on), null, sub, tip);
}
function pill(svg, text) { return h('span', { class: 'ev-pill' }, icon(svg), text); }
function tiles(d) {
  const j = M.judgeTile(d.picked, d.axes), c = M.runCounts(d.runs);
  const judge = j.runs
    ? ['context precision, judge ', h('b', {}, j.judges.join(', ') || 'unnamed'), `, ${plural(j.runs, 'run', 'runs')}`]
    : 'No judged rerank-on run yet';
  return [
    kindTile(d.rows, 'book', 'book'),
    kindTile(d.rows, 'newspaper', 'newspaper'),
    tile('Judge: useful passages ranked first', f2(j.value), null, judge,
      `RAGAS context precision from the judge model: are the useful passages ranked first? Mean over the ${plural(j.runs, 'latest judged rerank-on run', 'latest judged rerank-on runs')}.`),
    tile('Runs, all counted', fmtN(c.total),
      h('div', { class: 'ev-counts' }, pill(SVG.good, `${fmtN(c.complete)} complete`), pill(SVG.warning, `${fmtN(c.partial)} partial`),
        pill(SVG.critical, `${fmtN(c.failed)} failed`), c.running ? pill(RUNNING, `${fmtN(c.running)} running`) : null),
      `${plural(c.errors, 'retrieval error', 'retrieval errors')} in ${plural(c.questions, 'question', 'questions')}`,
      'Every run is counted. A partial run keeps the questions it scored and never stands for its set.'),
  ];
}

/* ---------- shared card parts ---------- */
function seg(id, label) {
  return h('div', { class: 'ev-seg', role: 'group', 'aria-label': label },
    h('button', { type: 'button', 'data-evtog': id, 'data-mode': 'chart', 'aria-pressed': 'true' }, 'Chart'),
    h('button', { type: 'button', 'data-evtog': id, 'data-mode': 'table', 'aria-pressed': 'false' }, 'Table'));
}
function head(id, title, sub, right) {
  return h('div', { class: 'ev-head' }, h('div', {}, h('h2', { id }, title), sub ? h('p', {}, sub) : null), right);
}
const src = text => h('div', { class: 'ev-src' }, text);
function table(cols, rows) {
  return h('table', { class: 'data' }, h('tr', {}, cols.map(c => h('th', {}, c))),
    rows.map(r => h('tr', {}, r.map((v, i) => h('td', i === 0 ? { dir: 'auto' } : {}, v)))));
}

/* ---------- rerank dumbbell ---------- */
/** The radar for a dumbbell row: rerank on laid over rerank off, or the one side there is. */
const dbLink = r => (r.on && r.off ? radarHash({ runs: [r.on.run], vs: [r.off.run] }) : radarHash({ runs: [(r.on || r.off).run] }));
const has = v => v !== null && v !== undefined;
function dbTip(r, off, on) {
  const found = v => `${fmtN(r.questions - M.missed(r.questions, v))} of ${fmtN(r.questions)}`;
  const runs = [r.off ? `run ${r.off.run} (off)` : 'no rerank-off run', r.on ? `run ${r.on.run} (on)` : 'no rerank-on run'].join(' vs ');
  let tip = `Set ${r.set_id}, ${runs}`;
  if (has(off) && has(on)) tip += `: ${found(off)} found without rerank, ${found(on)} with`;
  else if (has(on) || has(off)) tip += `: ${found(has(on) ? on : off)} found`;
  return tip;
}
/** The dumbbell's marks and its value text for one row. */
function dbMarks(off, on, pos) {
  const marks = [0, 1, 2, 3].map(i => h('i', { class: 'g', style: { 'inset-inline-start': `${i * 100 / 3}%` } }));
  if (has(off) && has(on) && off === on) {
    marks.push(h('i', { class: 'd both', style: { 'inset-inline-start': `${pos(on)}%` } }));
    return { marks, val: `${f2(on)}, same` };
  }
  if (has(off) && has(on)) {
    const a = Math.min(pos(off), pos(on)), b = Math.max(pos(off), pos(on));
    marks.push(h('i', { class: 'ln', style: { 'inset-inline-start': `${a}%`, 'inline-size': `${b - a}%` } }));
  }
  if (has(off)) marks.push(h('i', { class: 'd off', style: { 'inset-inline-start': `${pos(off)}%` } }));
  if (has(on)) marks.push(h('i', { class: 'd on', style: { 'inset-inline-start': `${pos(on)}%` } }));
  let val;
  if (has(off) && has(on)) val = [`${f2(off)} → `, h('b', {}, f2(on))];
  else val = has(on) ? [h('b', {}, f2(on)), ', rerank on only'] : `${f2(off)}, rerank off only`;
  return { marks, val };
}
function dbRow(r, pos) {
  const off = r.off?.v, on = r.on?.v;
  const tip = dbTip(r, off, on);
  const { marks, val } = dbMarks(off, on, pos);
  return h('div', { class: 'ev-dbrow', 'data-tip': tip },
    h('div', { class: 'who' }, h('a', { class: 'ev-go', href: dbLink(r) }, h('b', { class: 't', dir: 'auto' }, r.title)), h('small', {}, `Set ${r.set_id}, ${plural(r.questions, 'question', 'questions')}`)),
    h('div', { class: 'ev-track' }, marks),
    h('div', { class: 'val' }, val));
}
const gainText = gain => (gain === null ? '–' : (gain >= 0 ? '+' : '') + gain.toFixed(2));
function rerankCard(d) {
  const rows = d.rows;
  const used = [...d.picked.values()].flatMap(p => [p.off, p.on]);
  const label = M.searchLabel(used);
  const labelText = label ? ` ${human(label)}.` : '';
  const top = head('ev-rerank-h', 'What the reranker adds, per set',
    `Share of questions found in the top 5, without and with rerank.${labelText}`, rows.length ? seg('rerank', 'View') : null);
  if (!rows.length) return [top, h('p', { class: 'empty' }, 'No set has a complete run yet. Score one with culture-library eval run.')];
  const [lo] = M.domain(rows.flatMap(r => [r.off?.v, r.on?.v]));
  const pos = v => (v - lo) / (1 - lo) * 100;
  const ticks = [0, 1, 2, 3].map(i => lo + (1 - lo) * i / 3);
  const smallest = Math.min(...rows.map(r => r.questions).filter(n => n > 0));
  const smallNote = Number.isFinite(smallest) && smallest < 10
    ? ` The smallest set has ${plural(smallest, 'question', 'questions')}, so one question moves its score by ${f2(1 / smallest)}.`
    : '';
  return [
    top,
    h('div', { class: 'ev-viz' },
      h('div', { class: 'ev-legend' }, h('span', {}, h('i', { class: 'ev-dot off' }), 'Rerank off'), h('span', {}, h('i', { class: 'ev-dot on' }), 'Rerank on')),
      h('div', { class: 'ev-db' },
        h('div', { class: 'ev-dbrow ev-axis', 'aria-hidden': 'true' }, h('div', { class: 'who' }),
          h('div', { class: 'ev-scale' }, ticks.map((t, i) => h('span', { style: { 'inset-inline-start': `${i * 100 / 3}%` } }, t.toFixed(2)))), h('div', { class: 'val' })),
        rows.map(r => dbRow(r, pos)))),
    h('div', { class: 'ev-tbl', hidden: true }, table(['Set', 'Questions', 'Rerank off', 'Rerank on', 'Change', 'Runs (off, on)'],
      rows.map(r => [`${r.title} (set ${r.set_id})`, fmtN(r.questions), f2(r.off?.v), f2(r.on?.v),
        gainText(r.gain), `${r.off ? r.off.run : '–'}, ${r.on ? r.on.run : '–'}`]))),
    src(`Source: GET /api/eval/runs, the latest complete run per set and rerank setting, scored by GET /api/eval/radar (found at 5). Sorted by gain.${smallNote}`),
  ];
}

/* ---------- weakest sources ---------- */
function rankList(rows, nameOf, cls) {
  return h('ol', { class: 'ev-rank' }, rows.map((r, n) => {
    const miss = M.missed(r.questions, r.axes.found_at_5), share = r.axes.found_at_5 || 0;
    return h('li', { 'data-tip': `${nameOf(r)}: ${fmtN(r.questions - miss)} of ${fmtN(r.questions)} found in the top 5 (run ${r.run_id}, set ${r.set_id})` },
      h('span', { class: 'n' }, String(n + 1)), h('b', { class: cls, dir: 'auto' }, nameOf(r)),
      h('span', { class: 'miss' }, fmtN(miss), h('small', {}, `of ${fmtN(r.questions)} missed`)),
      h('span', { class: 'ev-bar', role: 'img', 'aria-label': `${Math.round(share * 100)}% found` }, h('span', { style: { 'inline-size': `${share * 100}%` } })));
  }));
}
function weakCard(d) {
  const which = d.weakOn ? 'rerank-on' : 'rerank-off';
  const top = head('ev-weak-h', 'Weakest sources', `Most questions missed in the top 5, in the latest complete ${which} run per set.`, d.weak ? seg('weak', 'View') : null);
  if (!d.weak) return [top, h('p', { class: 'empty' }, 'No complete run to rank yet.')];
  const scored = r => has(r.axes?.found_at_5);
  const items = d.weak.items.filter(scored), urls = d.weak.sources.filter(scored);
  const weakItems = items.filter(r => M.missed(r.questions, r.axes.found_at_5) > 0).slice(0, 5);
  const weakUrls = urls.filter(r => M.missed(r.questions, r.axes.found_at_5) > 0).slice(0, 3);
  const clean = items.length - items.filter(r => M.missed(r.questions, r.axes.found_at_5) > 0).length;
  const cleanText = clean ? `${plural(clean, 'more set misses', 'more sets miss')} nothing. ` : '';
  const itemName = r => info(r.item_id)?.title || `Item ${r.item_id}`;
  const tbl = [...weakItems.map(r => [itemName(r), r]), ...weakUrls.map(r => [r.url, r])];
  return [
    top,
    h('div', { class: 'ev-viz' },
      weakItems.length ? rankList(weakItems, itemName, 't') : h('p', { class: 'empty' }, 'Every set finds every question in the top 5.'),
      weakUrls.length ? [h('h3', { class: 'ev-h3' }, 'Newspaper sources'), rankList(weakUrls, r => r.url, 'u')] : null),
    h('div', { class: 'ev-tbl', hidden: true }, table(['Source', 'Missed', 'Questions', 'Found at 5', 'Run'],
      tbl.map(([name, r]) => [name, fmtN(M.missed(r.questions, r.axes.found_at_5)), fmtN(r.questions), f2(r.axes.found_at_5), String(r.run_id)]))),
    src(`${cleanText}Bars show the share found. Source: GET /api/eval/weakest.`),
  ];
}

/* ---------- suites ---------- */
function filterSeg(attr, label, active, options) {
  return h('div', { class: 'ev-seg', role: 'group', 'aria-label': label },
    options.map(([key, text]) => h('button', { type: 'button', [attr]: key, 'aria-pressed': String(active === key) }, text)));
}
function suiteCard(s, runsWith) {
  const live = runsWith > 0;
  return h('article', { class: live ? 'ev-suite live' : 'ev-suite' },
    h('div', { class: 'top' }, h('h3', {}, SUITE_TITLES[s.name] || human(s.name)),
      h('span', { class: live ? 'ev-st live' : 'ev-st' }, icon(live ? SVG.good : RUNNING), live ? `In ${plural(runsWith, 'run', 'runs')}` : 'Not run yet')),
    h('p', {}, `${s.name}, version ${s.version}. ${s.needs_judge ? 'Needs a judge model.' : 'No judge.'}`),
    h('div', { class: 'ev-metrics' }, (s.metrics || []).map(m => {
      const [lo, hi] = m.range || [];
      const range = `Range ${lo ?? '?'} to ${hi ?? 'no upper bound'}, ${m.direction === 'lower' ? 'lower' : 'higher'} is better.`;
      return h('span', { class: 'ev-metric', tabindex: '0', 'data-tip': `${m.name}: ${m.definition} ${range}` }, metricLabel(m.name));
    })));
}
function suitesShown(all, scored, used) {
  if (ui.suites === 'scored') return scored;
  return ui.suites === 'unrun' ? all.filter(s => !used.get(s.name)) : all;
}
function drawSuites() {
  if (!last) return;
  const used = new Map();
  for (const r of last.runs) for (const s of r.suites || []) used.set(s.name, (used.get(s.name) || 0) + 1);
  const all = last.suites, scored = all.filter(s => used.get(s.name));
  const shown = suitesShown(all, scored, used);
  fill($('ev-suites'),
    head('ev-suites-h', 'Suites', 'Each suite is a plugin. A run records every suite it scored, so runs stay comparable as suites are added. Hover a metric for its definition.',
      filterSeg('data-evsuites', 'Filter suites', ui.suites, [['all', `All ${all.length}`], ['scored', `Scored ${scored.length}`], ['unrun', `Not run yet ${all.length - scored.length}`]])),
    shown.length ? h('div', { class: 'ev-suites' }, shown.map(s => suiteCard(s, used.get(s.name) || 0))) : h('p', { class: 'empty' }, 'No suite in this group.'),
    src('Source: GET /api/eval/suites; run counts from each run\'s recorded suites (GET /api/eval/runs).'));
}

/* ---------- runs ---------- */
// A function, not a constant: app.js (which owns SVG) imports this module, so SVG is not
// initialised yet while this module is evaluated.
const STATUS_LABELS = { complete: 'Complete', partial: 'Partial', failed: 'Failed', running: 'Running' };
const statusIcon = s => ({ complete: SVG.good, partial: SVG.warning, failed: SVG.critical })[s] || RUNNING;
function statusWhy(c) {
  if (c.llm_metrics_failed) return `Judge scoring failed: ${c.llm_metrics_failed}`;
  return c.error ? `Retrieval error: ${c.error}` : null;
}
function status(r) {
  const svg = statusIcon(r.status), label = STATUS_LABELS[r.status] || String(r.status);
  const c = r.config || {};
  const why = statusWhy(c);
  return h('span', { class: `ev-st ${r.status}`, 'data-tip': why, dir: why ? 'auto' : null }, icon(svg), label);
}
function runRow(r) {
  const set = last.sets.find(s => s.id === r.set_id) || {};
  const it = info(set.item_id) || { title: set.item_id ? `Item ${set.item_id}` : `Set ${r.set_id}`, kind: null };
  const ax = last.axes.get(r.id) || {};
  const agg = r.status === 'complete' ? r.aggregates : r.partial_aggregates;
  const idp = agg && typeof agg[ID_PRECISION] === 'number' ? agg[ID_PRECISION] : null;
  const td = (label, v, cls) => h('td', { class: cls, 'data-label': label }, v);
  return h('tr', {},
    td('Run', h('a', { class: 'ev-go', href: radarHash({ runs: [r.id] }), 'aria-label': `Run ${r.id} on the radar` }, h('b', {}, String(r.id)))),
    td('Source', [kindChip(it.kind), ' ', h('span', { class: 't', dir: 'auto' }, it.title)], 'src'),
    td('Search', M.runSearch(r), 'cfg'),
    td('Status', status(r)),
    td('Found in top 5', score(ax.found_at_5), 'n'),
    td('ID precision', score(idp), 'n'),
    td('Judge precision', score(ax.judge_precision), 'n'),
    td('Judge recall', score(ax.judge_recall), 'n'),
    td('Recorded', utc(r.created_at)));
}
const noRunsText = () => (last.runs.length ? 'No run with this status.' : 'No eval run yet. Score a set with culture-library eval run.');
const moreLabel = n => (ui.allRuns ? 'Show the newest only' : `Show all ${fmtN(n)} runs`);
function drawRuns() {
  if (!last) return;
  const c = M.runCounts(last.runs);
  const all = M.newestFirst(last.runs);
  const list = ui.runs === 'all' ? all : all.filter(r => r.status === ui.runs);
  const shown = ui.allRuns ? list : list.slice(0, RUNS_SHOWN);
  const opts = [['all', `All ${c.total}`], ...['complete', 'partial', 'failed', 'running'].filter(s => c[s]).map(s => [s, `${STATUS_LABELS[s]} ${c[s]}`])];
  const cols = ['Run', 'Source', 'Search', 'Status', 'Found in top 5', 'ID precision', 'Judge precision', 'Judge recall', 'Recorded'];
  fill($('ev-runs'),
    head('ev-runs-h', 'Latest runs', 'Times in UTC. ID precision at k tops out at 1/k when a question has one reference passage.',
      filterSeg('data-evruns', 'Filter runs', ui.runs, opts)),
    list.length
      ? h('div', { class: 'ev-tablewrap' }, h('table', { class: 'ev-table' },
        h('thead', {}, h('tr', {}, cols.map((t, i) => h('th', { class: i >= 4 && i <= 7 ? 'n' : null }, t)))),
        h('tbody', {}, shown.map(runRow))))
      : h('p', { class: 'empty' }, noRunsText()),
    h('div', { class: 'ev-more' }, h('span', {}, `Showing ${fmtN(shown.length)} of ${plural(list.length, 'run', 'runs')}, newest first. Source: GET /api/eval/runs and GET /api/eval/radar.`),
      list.length > RUNS_SHOWN ? h('button', { type: 'button', class: 'btn', 'data-evmore': '' }, moreLabel(list.length)) : null));
}

/* ---------- events ---------- */
export function init() {
  radar.init();
  compare.init();
  $('view-evals').addEventListener('click', e => {
    const t = e.target;
    const tog = t.closest('[data-evtog]');
    if (tog) {
      const card = tog.closest('.ev-card'), table = tog.dataset.mode === 'table';
      card.querySelector('.ev-viz').hidden = table; card.querySelector('.ev-tbl').hidden = !table;
      card.querySelectorAll('[data-evtog]').forEach(b => b.setAttribute('aria-pressed', String(b === tog)));
      return;
    }
    const su = t.closest('[data-evsuites]'); if (su) { ui.suites = su.dataset.evsuites; drawSuites(); return; }
    const ru = t.closest('[data-evruns]'); if (ru) { ui.runs = ru.dataset.evruns; ui.allRuns = false; drawRuns(); return; }
    if (t.closest('[data-evmore]')) { ui.allRuns = !ui.allRuns; drawRuns(); }
  });
}
