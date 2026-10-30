/* Evals overview numbers: pure functions over the eval API's JSON. No DOM, no imports (the
 * tests run this module under Node); evals.js draws what these return.
 *
 * Inputs are the shapes of GET /api/eval/runs (runs), GET /api/eval/sets (sets), and the
 * per-run axes of GET /api/eval/radar?runs=... (``axes``: run id -> {found_at_5, judge_precision,
 * ...} and ``questions``: run id -> question count). ``info(itemId)`` gives an item's
 * {title, kind} (kind is the API kind, e.g. 'book' or 'newspaper').
 */

const rerankOf = run => Boolean(run && run.config && run.config.rerank);

/**
 * The runs the overview compares: for every set and each rerank setting (off, on), the latest
 * complete run.
 *
 * Rule: only runs whose status is "complete" count (a partial run never stands for a set, and
 * a running or failed one has no full score); "latest" is the highest run id, because ids are
 * assigned in insertion order and created_at has one-second resolution. The rerank setting is
 * the run's own recorded config.rerank (missing means off). Search mode and k are not part of
 * the key: the overview says which settings the picked runs used (searchLabel).
 *
 * Returns Map set id -> {off: run | null, on: run | null}.
 */
export function latestComplete(runs) {
  const out = new Map();
  for (const r of runs || []) {
    if (r.status !== 'complete') continue;
    const slot = out.get(r.set_id) || { off: null, on: null };
    const side = rerankOf(r) ? 'on' : 'off';
    if (!slot[side] || r.id > slot[side].id) slot[side] = r;
    out.set(r.set_id, slot);
  }
  return out;
}

/** "hybrid, k 5" for runs that share one search setting, else "mixed search settings". */
export function searchLabel(runs) {
  const labels = new Set(runs.filter(Boolean).map(r => `${r.config?.mode || 'mode ?'}, k ${r.config?.k ?? '?'}`));
  if (!labels.size) return '';
  return labels.size === 1 ? [...labels][0] : 'mixed search settings';
}

/** A run's search setting for the runs table: "hybrid, k 5, rerank on". */
export function runSearch(run) {
  const c = run.config || {};
  return `${c.mode || 'mode ?'}, k ${c.k ?? '?'}, rerank ${rerankOf(run) ? 'on' : 'off'}`;
}

const mean = vals => {
  const v = vals.filter(x => x !== null && x !== undefined && !Number.isNaN(x));
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
};
const axis = (axes, run, name) => {
  if (!run) return null;
  const a = axes.get(run.id);
  return a && a[name] !== undefined ? a[name] : null;
};

/**
 * One dumbbell row per set that has a latest complete run on either side: found in the top 5
 * without rerank (off) and with it (on). Sorted by gain (on minus off), largest first; sets
 * with only one side come last. Set order breaks ties.
 */
export function rerankRows(sets, picked, axes, questions, info) {
  const rows = [];
  for (const s of sets || []) {
    const p = picked.get(s.id);
    if (!p) continue;
    const it = info(s.item_id) || {};
    const off = axis(axes, p.off, 'found_at_5'), on = axis(axes, p.on, 'found_at_5');
    if (off === null && on === null) continue;
    const ref = p.on || p.off;
    rows.push({
      set_id: s.id, item_id: s.item_id, title: it.title || `Item ${s.item_id}`, kind: it.kind || 'document',
      questions: questions.get(ref.id) ?? ref.total ?? 0,
      off: p.off ? { run: p.off.id, v: off } : null,
      on: p.on ? { run: p.on.id, v: on } : null,
      gain: off !== null && on !== null ? on - off : null,
    });
  }
  return rows.sort((a, b) => (a.gain === null) - (b.gain === null) || (b.gain ?? 0) - (a.gain ?? 0) || a.set_id - b.set_id);
}

/** The dumbbell axis: [lo, 1] with lo a multiple of 0.2 at or below every value (0.8 at most). */
export function domain(values) {
  const v = values.filter(x => x !== null && x !== undefined);
  if (!v.length) return [0, 1];
  const lo = Math.floor((Math.min(...v) + 1e-9) * 5) / 5;
  return [Math.max(0, Math.min(0.8, lo)), 1];
}

/** Headline numbers for one kind: mean found-in-top-5 with and without rerank over its sets. */
export function headline(rows, kind) {
  const mine = rows.filter(r => r.kind === kind);
  return {
    sets: mine.length,
    questions: mine.reduce((a, r) => a + (r.questions || 0), 0),
    on: mean(mine.map(r => r.on && r.on.v)),
    off: mean(mine.map(r => r.off && r.off.v)),
    onSets: mine.filter(r => r.on && r.on.v !== null).length,
    offSets: mine.filter(r => r.off && r.off.v !== null).length,
  };
}

/** Judge precision over the picked rerank-on runs that a judge scored. */
export function judgeTile(picked, axes) {
  const runs = [...picked.values()].map(p => p.on).filter(r => r && axis(axes, r, 'judge_precision') !== null);
  const judges = [...new Set(runs.map(r => r.config?.judge_model).filter(Boolean))];
  return { value: mean(runs.map(r => axis(axes, r, 'judge_precision'))), runs: runs.length, judges };
}

/**
 * Every run counted by status, with retrieval errors: a run's errors are its error_rate times
 * its question total (rounded), summed over runs that report an error rate.
 */
export function runCounts(runs) {
  const c = { total: 0, complete: 0, partial: 0, failed: 0, running: 0, errors: 0, questions: 0 };
  for (const r of runs || []) {
    c.total += 1;
    if (c[r.status] !== undefined) c[r.status] += 1;
    c.questions += r.total || 0;
    if (typeof r.error_rate === 'number') c.errors += Math.round(r.error_rate * (r.total || 0));
  }
  return c;
}

/** Questions missed in the top 5 out of ``questions``, from a found-at-5 share. */
export const missed = (questions, found5) => (found5 === null || found5 === undefined ? null : questions - Math.round(found5 * questions));

/** Newest first (highest id first). */
export const newestFirst = runs => [...(runs || [])].sort((a, b) => b.id - a.id);

/* ---------- radar (plan task t17) ---------- */

/** A run's search setting as a key: "hybrid|5|on" (mode, k, rerank). */
export const configKey = run => {
  const c = (run && run.config) || {};
  return `${c.mode || '?'}|${c.k ?? '?'}|${rerankOf(run) ? 'on' : 'off'}`;
};
/** "hybrid, k 5, rerank on" for a configKey. */
export const configText = key => {
  const [mode, k, rr] = String(key).split('|');
  return `${mode === '?' ? 'mode ?' : mode}, k ${k}, rerank ${rr}`;
};

/** "set 4", "sets 1–3, 5": set ids, ranges of neighbours joined. */
export function setsText(ids) {
  const v = [...new Set(ids)].sort((a, b) => a - b);
  const parts = [];
  for (let i = 0; i < v.length; i++) {
    let j = i;
    while (j + 1 < v.length && v[j + 1] === v[j] + 1) j++;
    parts.push(j > i + 1 ? `${v[i]}–${v[j]}` : j === i + 1 ? `${v[i]}, ${v[j]}` : `${v[i]}`);
    i = j;
  }
  return `${v.length === 1 ? 'set' : 'sets'} ${parts.join(', ')}`;
}

const KIND_GROUPS = [['book', 'books'], ['newspaper', 'newspapers']];
const completeSets = runs => new Set((runs || []).filter(r => r.status === 'complete').map(r => r.set_id));

/**
 * The radar's document groups, for sets with at least one complete run: all books, all
 * newspapers, then other kinds, then everything (when more than one group). Each is
 * {id, name, label, sets}; ``id`` is the item kind, or "all"; ``label`` is ``name`` with its sets.
 */
export function setGroups(sets, runs, info) {
  const live = completeSets(runs);
  const byKind = new Map();
  for (const s of sets || []) {
    if (!live.has(s.id)) continue;
    const kind = (info(s.item_id) || {}).kind || 'document';
    if (!byKind.has(kind)) byKind.set(kind, []);
    byKind.get(kind).push(s.id);
  }
  const named = new Map(KIND_GROUPS);
  const kinds = [...KIND_GROUPS.map(k => k[0]).filter(k => byKind.has(k)), ...[...byKind.keys()].filter(k => !named.has(k)).sort()];
  const out = kinds.map(k => {
    const ids = byKind.get(k).sort((a, b) => a - b);
    const name = `All ${named.get(k) || `${k} items`}`;
    return { id: k, name, label: `${name}, ${setsText(ids)}`, sets: ids };
  });
  if (out.length > 1) {
    const all = out.flatMap(g => g.sets).sort((a, b) => a - b);
    out.push({ id: 'all', name: 'Everything', label: `Everything, ${setsText(all)}`, sets: all });
  }
  return out;
}

/** The search settings complete runs of ``setIds`` used: [{key, sets}], most sets first, then rerank on. */
export function configsFor(runs, setIds) {
  const want = new Set(setIds), per = new Map();
  for (const r of runs || []) {
    if (r.status !== 'complete' || !want.has(r.set_id)) continue;
    const k = configKey(r);
    if (!per.has(k)) per.set(k, new Set());
    per.get(k).add(r.set_id);
  }
  return [...per].map(([key, s]) => ({ key, sets: s.size }))
    .sort((a, b) => b.sets - a.sets || b.key.endsWith('|on') - a.key.endsWith('|on') || a.key.localeCompare(b.key));
}

/** For each set, the latest complete run with search setting ``key``: {runs, missing: [set ids]}. */
export function pickRuns(runs, setIds, key) {
  const out = [], missing = [];
  for (const sid of setIds) {
    let best = null;
    for (const r of runs || []) {
      if (r.set_id === sid && r.status === 'complete' && configKey(r) === key && (!best || r.id > best.id)) best = r;
    }
    if (best) out.push(best); else missing.push(sid);
  }
  return { runs: out, missing };
}

const suiteNames = run => [...new Set((run.suites || []).map(s => s.name))].sort().join(',');
const judgeOf = run => (run.config && run.config.judge_model) || null;

/**
 * The earlier run to lay under each of ``picked``: on the same set, complete, older (lower id),
 * with the same search setting, the same suites and the same judge (what GET /api/eval/compare
 * calls comparable, plus the same setting so the overlay shows change over time). The latest
 * such run per set. {runs, missing: [set ids with none]}; the radar lays nothing under a group
 * unless ``missing`` is empty.
 */
export function earlierRuns(runs, picked) {
  const out = [], missing = [];
  for (const p of picked) {
    let best = null;
    for (const r of runs || []) {
      if (r.set_id !== p.set_id || r.status !== 'complete' || r.id >= p.id) continue;
      if (configKey(r) !== configKey(p) || suiteNames(r) !== suiteNames(p) || judgeOf(r) !== judgeOf(p)) continue;
      if (!best || r.id > best.id) best = r;
    }
    if (best) out.push(best); else missing.push(p.set_id);
  }
  return { runs: out, missing };
}

/**
 * The pickers' state for runs named in a link: {scope: 'one' | 'set', docs, key}. ``docs`` is
 * "set:N" for one run, a group id when the runs cover exactly that group's sets, else "custom";
 * ``key`` is their shared configKey, else "mixed".
 */
export function selectionOf(picked, groups) {
  const keys = new Set(picked.map(configKey));
  const key = keys.size === 1 ? [...keys][0] : 'mixed';
  if (picked.length === 1) return { scope: 'one', docs: `set:${picked[0].set_id}`, key };
  const ids = [...new Set(picked.map(r => r.set_id))].sort((a, b) => a - b).join(',');
  const g = (groups || []).find(x => x.sets.join(',') === ids);
  return { scope: 'set', docs: g ? g.id : 'custom', key };
}

/** What #evals/radar shows without parameters: the first group (books first) and the setting most of its sets ran. */
export function defaultView(runs, groups) {
  const g = (groups || [])[0];
  if (!g) return null;
  const c = configsFor(runs, g.sets)[0];
  return c ? { docs: g.id, sets: g.sets, key: c.key } : null;
}

/* ---------- compare (plan task t18) ---------- */

const SUITE_TITLES = {
  id_retrieval: 'ID retrieval', judged: 'judged retrieval', result_quality: 'result quality',
  speed: 'speed and errors', freshness: 'freshness',
};
/** A suite's name for a sentence: "ID retrieval", "judged retrieval", else its words. */
export const suiteTitle = name => SUITE_TITLES[name] || String(name).replaceAll('_', ' ');
/** "a", "a and b", "a, b and c". */
const andList = xs => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`);
const cap = s => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const sameSets = (a, b) => {
  const x = [...new Set(a.map(r => r.set_id))].sort((p, q) => p - q).join(',');
  return x === [...new Set(b.map(r => r.set_id))].sort((p, q) => p - q).join(',');
};
const runsName = runs => (runs.length === 1 ? `run ${runs[0].id}` : `runs ${runs.map(r => r.id).join(', ')}`);

/**
 * What differs between the two sides' search settings, and the page's headline question.
 * {question, settings, diffs: [{what, then}]}. One setting differing: "Did turning rerank on
 * help?"; none: "What changed since run 3?"; a side with mixed settings, or different sets:
 * "How do these runs compare?".
 */
export function compareQuestion(before, after) {
  const kb = new Set(before.map(configKey)), ka = new Set(after.map(configKey));
  if (kb.size !== 1 || ka.size !== 1) {
    return { question: 'How do these runs compare?', settings: 'Mixed search settings on one side', diffs: [] };
  }
  const [mb, kkb, rb] = [...kb][0].split('|'), [ma, kka, ra] = [...ka][0].split('|');
  const diffs = [];
  if (mb !== ma) diffs.push({ what: `switching from ${mb} to ${ma} search`, then: `${mb}, then ${ma} search` });
  if (kkb !== kka) diffs.push({ what: `changing k from ${kkb} to ${kka}`, then: `k ${kkb}, then ${kka}` });
  if (rb !== ra) diffs.push({ what: `turning rerank ${ra}`, then: `rerank ${rb}, then ${ra}` });
  const settings = !diffs.length
    ? `No setting differs: ${configText([...ka][0])}`
    : diffs.length === 1 ? `Only one setting differs: ${diffs[0].then}` : `${diffs.length} settings differ: ${diffs.map(d => d.then).join('; ')}`;
  let question;
  if (!sameSets(before, after)) question = 'How do these runs compare?';
  else if (!diffs.length) question = `What changed since ${runsName(before)}?`;
  else question = `Did ${andList(diffs.map(d => d.what))} help?`;
  return { question, settings, diffs };
}

const errorsOf = runs => (runs.some(r => typeof r.error_rate === 'number')
  ? runs.reduce((t, r) => t + Math.round((r.error_rate || 0) * (r.total || 0)), 0) : null);

/**
 * The comparability checks, in the order the page shows them: the reasons GET /api/eval/compare
 * gives for refusing (state "fail"), then what holds on both sides (state "ok": same sets and
 * versions, same suites and judge, every run complete), then the API's warnings and runs left
 * out ("warn"). A check that does not hold is left out of the "ok" rows: the API's reasons and
 * warnings already name it. Rows are {state, text, detail}.
 */
export function checkRows(before, after, checks, leftOut) {
  const c = checks || { reasons: [], warnings: [] };
  const rows = (c.reasons || []).map(t => ({ state: 'fail', text: cap(t), detail: null }));
  const all = [...before, ...after];
  const many = new Set(all.map(r => r.set_id)).size > 1;
  if (all.length && sameSets(before, after)) {
    const ver = new Set(all.map(r => r.set_version));
    const known = ver.size === 1 && [...ver][0] !== undefined && [...ver][0] !== null;
    const ids = [...new Set(before.map(r => r.set_id))];
    const qs = before.reduce((t, r) => t + (r.total || 0), 0);
    rows.push({
      state: 'ok',
      text: `Same question ${many ? 'sets' : 'set'}${known ? (many ? ' and versions' : ' and version') : ''}`,
      detail: `${cap(setsText(ids))}${known && !many ? `, version ${[...ver][0]}` : ''}, ${qs} questions`,
    });
  }
  const suites = new Set(all.map(suiteNames)), judges = new Set(all.map(judgeOf));
  if (all.length && suites.size === 1 && judges.size === 1) {
    const names = [...suites][0].split(',').filter(Boolean).map(suiteTitle);
    const judge = [...judges][0];
    rows.push({ state: 'ok', text: 'Same suites and judge', detail: `${cap(names.length > 2 ? names.join(', ') : andList(names)) || 'No suites'}; ${judge ? `judge ${judge}` : 'no judge'}` });
  }
  if (all.length && all.every(r => r.status === 'complete')) {
    const errs = errorsOf(all);
    rows.push({
      state: 'ok',
      text: `${all.length === 2 ? 'Both' : 'All'} complete${errs === null ? '' : `, ${errs} ${errs === 1 ? 'error' : 'errors'}`}`,
      detail: compareQuestion(before, after).settings,
    });
  }
  for (const t of c.warnings || []) rows.push({ state: 'warn', text: cap(t), detail: null });
  for (const side of ['before', 'after']) {
    const n = (leftOut && leftOut[side]) || 0;
    if (n) rows.push({ state: 'warn', text: `${n} ${n === 1 ? 'run' : 'runs'} left out on the ${side} side`, detail: `This key cannot see ${n === 1 ? 'its item' : 'their items'}.` });
  }
  return rows;
}

const EPS = 0.005;
/** The axes the change helped, hurt and left alone (within 0.005), and those not scored on both sides. */
export function tally(keys, deltas) {
  const out = { better: [], worse: [], same: [], unscored: [] };
  for (const k of keys) {
    const d = deltas ? deltas[k] : null;
    if (typeof d !== 'number' || Number.isNaN(d)) out.unscored.push(k);
    else if (d > EPS) out.better.push(k);
    else if (d < -EPS) out.worse.push(k);
    else out.same.push(k);
  }
  return out;
}

/** ``keys`` sorted by change, largest gain first; unscored axes last, in their own order. */
export function byChange(keys, deltas) {
  const d = k => (deltas && typeof deltas[k] === 'number' ? deltas[k] : null);
  return keys.map((k, i) => [k, i]).sort((a, b) => {
    const x = d(a[0]), y = d(b[0]);
    if (x === null || y === null) return (x === null) - (y === null) || a[1] - b[1];
    return y - x || a[1] - b[1];
  }).map(p => p[0]);
}

const ID_AXES = ['found_at_1', 'found_at_3', 'found_at_5', 'mrr'];
const num = v => (typeof v === 'number' && !Number.isNaN(v) ? v : null);

/**
 * Changed questions (GET /api/eval/compare, worst first) split for the page: ``worse`` holds
 * every question that got worse on at least one ID result (found at 1, 3, 5, MRR), even if it
 * gained on another; ``better`` the rest. The API's order is kept in each.
 */
export function questionGroups(changed) {
  const worse = [], better = [];
  for (const q of changed || []) {
    const down = ID_AXES.some(a => num(q.before && q.before[a]) !== null && num(q.after && q.after[a]) !== null && q.after[a] < q.before[a]);
    (down ? worse : better).push(q);
  }
  return { worse, better };
}

/** Questions whose reference passage reached the top 5 only after (gained) or only before (lost). */
export function foundShift(changed) {
  let gained = 0, lost = 0;
  for (const q of changed || []) {
    const b = num(q.before && q.before.found_at_5), a = num(q.after && q.after.found_at_5);
    if (b === null || a === null) continue;
    if (b < 0.5 && a >= 0.5) gained += 1;
    if (b >= 0.5 && a < 0.5) lost += 1;
  }
  return { gained, lost };
}

/** The rank of the first reference passage from a question's reciprocal rank (null: not found). */
export const rankOf = mrr => (num(mrr) !== null && mrr > 0 ? Math.round(1 / mrr) : null);

/**
 * The before / after pickers: each document group (setGroups) with each search setting its
 * sets ran (the latest complete run per set, pickRuns), then every complete run, newest first,
 * named by its set's item (``sets`` from GET /api/eval/sets, ``info`` as for setGroups).
 * [{value, label, ids, group}]; ``value`` is "g:<group id>:<configKey>" or "r:<run id>".
 */
export function compareOptions(runs, groups, info, sets) {
  const out = [];
  for (const g of groups || []) {
    for (const c of configsFor(runs, g.sets)) {
      const ids = pickRuns(runs, g.sets, c.key).runs.map(r => r.id);
      const part = c.sets < g.sets.length ? ` (${c.sets} of ${g.sets.length} sets)` : '';
      out.push({ value: `g:${g.id}:${c.key}`, label: `${g.name}, ${configText(c.key)}${part}`, ids, group: 'Sets of documents' });
    }
  }
  for (const r of newestFirst((runs || []).filter(x => x.status === 'complete'))) {
    const s = (sets || []).find(x => x.id === r.set_id);
    const title = s ? (info(s.item_id) || {}).title : null;
    out.push({ value: `r:${r.id}`, label: `Run ${r.id}, ${title || `set ${r.set_id}`}, ${configText(configKey(r))}`, ids: [r.id], group: 'One run' });
  }
  return out;
}

/** The option whose runs are exactly ``ids`` (any order), or null. */
export function optionFor(ids, options) {
  const want = [...ids].sort((a, b) => a - b).join(',');
  const o = (options || []).find(x => [...x.ids].sort((a, b) => a - b).join(',') === want);
  return o ? o.value : null;
}

/**
 * What #evals/compare shows without parameters: "Did turning rerank on help?" for the first
 * document group (books first) that has one. For each set of the group, the latest rerank-on
 * run that has a comparable rerank-off run (complete, same mode and k, same suites and judge),
 * paired with the latest such rerank-off run; the mode and k most sets can pair on wins.
 * Without such a pair anywhere: the newest complete run against its earlier comparable run
 * (earlierRuns). {before, after, why: 'rerank' | 'earlier', group} or null.
 */
export function defaultCompare(runs, groups) {
  const done = (runs || []).filter(r => r.status === 'complete');
  for (const g of groups || []) {
    const bases = [...new Set(configsFor(done, g.sets).map(c => c.key.split('|').slice(0, 2).join('|')))];
    let best = null;
    for (const base of bases) {
      const pairs = [];
      for (const sid of g.sets) {
        const mine = done.filter(r => r.set_id === sid && configKey(r).startsWith(base + '|')).sort((a, b) => b.id - a.id);
        const rerankOn = mine.filter(r => configKey(r).endsWith('|on')), offs = mine.filter(r => configKey(r).endsWith('|off'));
        for (const on of rerankOn) {
          const off = offs.find(r => suiteNames(r) === suiteNames(on) && judgeOf(r) === judgeOf(on));
          if (off) { pairs.push([off, on]); break; }
        }
      }
      if (pairs.length && (!best || pairs.length > best.length)) best = pairs;
    }
    if (best) return { before: best.map(p => p[0].id), after: best.map(p => p[1].id), why: 'rerank', group: g.id };
  }
  for (const r of newestFirst(done)) {
    const e = earlierRuns(done, [r]);
    if (!e.missing.length) return { before: [e.runs[0].id], after: [r.id], why: 'earlier', group: null };
  }
  return null;
}
