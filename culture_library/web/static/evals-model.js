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
