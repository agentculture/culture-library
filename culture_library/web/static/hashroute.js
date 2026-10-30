/* Hash routes: which view a location hash opens. Pure, no DOM, no imports (the tests run it
 * under Node).
 *
 * The site's routes are #home, #state, an item key (item-N or doc-N) and the Evals pages:
 * #evals (the overview), #evals/radar and #evals/compare. The Evals routes are matched before
 * any item key, by their own "evals" path segment, so they can never collide with an item key,
 * and an unknown #evals/... page opens the Evals overview: the Evals tab never falls through
 * to home. Anything after "?" is the page's own parameters (later views read them).
 *
 * Radar parameters (#evals/radar?...):
 *   runs=IDS   the run (one id) or group of runs (comma-separated ids) whose shape to draw; the
 *              mean over its documents, one document per run. Missing: the radar picks the
 *              book sets' latest complete runs with the search setting most of them ran.
 *   vs=IDS     optional; the run or group to lay under it (dashed). Missing: the latest earlier
 *              comparable run of every set, when there is one. vs=none: lay nothing under.
 * IDS are positive integers joined by ","; anything else in the list is dropped, and a repeated
 * id counts once. Example: #evals/radar?runs=11,18,20,21,22&vs=none.
 *
 * Compare parameters (#evals/compare?...):
 *   before=IDS  the run or group of runs on the before side (dashed, under);
 *   after=IDS   the run or group on the after side (solid, over). Groups are paired by set id
 *               (GET /api/eval/compare). Example: #evals/compare?before=19&after=11, or
 *               #evals/compare?before=1,2,3&after=4,5,6.
 *   With after but no before, the page compares after with the latest earlier comparable run
 *   of each set; with neither (or before alone), it opens on its default pair (evals-model.js
 *   defaultCompare: the book sets' rerank-off runs against their rerank-on runs).
 */
export const EVAL_PAGES = ['radar', 'compare'];

/**
 * The view for ``hash`` ("#..." or without the "#"): {view: 'state'} | {view: 'evals', page,
 * params} | {view: 'read', key} | {view: 'home'}. ``isItemKey(key)`` says whether a loaded
 * item or repo document has that key.
 */
export function parseHash(hash, isItemKey) {
  const raw = String(hash || '').replace(/^#/, '');
  const q = raw.indexOf('?');
  const path = q < 0 ? raw : raw.slice(0, q);
  const params = q < 0 ? '' : raw.slice(q + 1);
  if (path === 'evals' || path.startsWith('evals/')) {
    const page = path.slice('evals/'.length);
    return { view: 'evals', page: EVAL_PAGES.includes(page) ? page : 'overview', params };
  }
  if (path === 'state') return { view: 'state' };
  if (raw && isItemKey(raw)) return { view: 'read', key: raw };
  return { view: 'home' };
}

/** The canonical hash of an Evals page ('overview', 'radar' or 'compare'). */
export const evalsHash = page => (page && page !== 'overview' ? `#evals/${page}` : '#evals');

const ids = text => [...new Set(String(text || '').split(',').map(x => x.trim())
  .filter(x => /^[0-9]+$/.test(x)).map(Number).filter(n => n > 0))];

/** The radar's parameters from a params string: {runs: [ids], vs: [ids] | 'none' | null}. */
export function radarParams(params) {
  const q = new URLSearchParams(String(params || ''));
  const vsRaw = q.get('vs');
  const vs = vsRaw === 'none' ? 'none' : ids(vsRaw);
  return { runs: ids(q.get('runs')), vs: Array.isArray(vs) && !vs.length ? null : vs };
}

/** The hash of a radar view: radarHash({runs: [11, 18], vs: [19]}) is "#evals/radar?runs=11,18&vs=19". */
export function radarHash({ runs, vs } = {}) {
  const parts = [];
  if (runs && runs.length) parts.push(`runs=${runs.join(',')}`);
  if (vs === 'none') parts.push('vs=none');
  else if (vs && vs.length) parts.push(`vs=${vs.join(',')}`);
  return evalsHash('radar') + (parts.length ? '?' + parts.join('&') : '');
}

/** The compare page's parameters from a params string: {before: [ids], after: [ids]}. */
export function compareParams(params) {
  const q = new URLSearchParams(String(params || ''));
  return { before: ids(q.get('before')), after: ids(q.get('after')) };
}

/** The hash of a compare view: compareHash({before: [19], after: [11]}) is "#evals/compare?before=19&after=11". */
export function compareHash({ before, after } = {}) {
  const parts = [];
  if (before && before.length) parts.push(`before=${before.join(',')}`);
  if (after && after.length) parts.push(`after=${after.join(',')}`);
  return evalsHash('compare') + (parts.length ? '?' + parts.join('&') : '');
}
