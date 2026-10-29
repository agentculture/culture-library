/* Hash routes: which view a location hash opens. Pure, no DOM, no imports (the tests run it
 * under Node).
 *
 * The site's routes are #home, #state, an item key (item-N or doc-N) and the Evals pages:
 * #evals (the overview), #evals/radar and #evals/compare. The Evals routes are matched before
 * any item key, by their own "evals" path segment, so they can never collide with an item key,
 * and an unknown #evals/... page opens the Evals overview: the Evals tab never falls through
 * to home. Anything after "?" is the page's own parameters (later views read them).
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
