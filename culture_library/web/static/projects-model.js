/* Browse by project and by domain (plan task t20): the pure model. No DOM, no imports (the
 * tests run it under Node).
 *
 * A devague slug ties a feature's documents together. The join is on the slug, never the
 * filename or its date:
 *   docs/specs/YYYY-MM-DD-<slug>.md        spec
 *   docs/plans/YYYY-MM-DD-<slug>.md        plan        (-split.md: the split plan)
 *   docs/deliveries/YYYY-MM-DD-<slug>.md   delivery
 *   .devague/frames/<slug>.json            frame
 *   .devague/plans/<slug>.json             plan state
 *   .devague/deliveries/<slug>.json        delivery state
 * Anything else (reviews, questions, designs, other docs) is listed apart as "Other".
 */
export const ROLES = ['spec', 'plan', 'split', 'delivery', 'frame', 'plan-state', 'delivery-state'];
export const ROLE_LABELS = {
  spec: 'Spec', plan: 'Plan', split: 'Split plan', delivery: 'Delivery', frame: 'Frame',
  'plan-state': 'Plan state', 'delivery-state': 'Delivery state',
};
const DATED = '(\\d{4}-\\d{2}-\\d{2})-([^/]+?)';
const DOC_DIRS = [
  ['spec', new RegExp(`^docs/specs/${DATED}\\.md$`)],
  ['plan', new RegExp(`^docs/plans/${DATED}\\.md$`)],
  ['delivery', new RegExp(`^docs/deliveries/${DATED}\\.md$`)],
];
const STATE_DIRS = [
  ['frame', /^\.devague\/frames\/([^/]+)\.json$/],
  ['plan-state', /^\.devague\/plans\/([^/]+)\.json$/],
  ['delivery-state', /^\.devague\/deliveries\/([^/]+)\.json$/],
];

/** {slug, role, date} for a repo path that belongs to a devague slug, else null. */
export function slugOf(path) {
  const p = String(path || '');
  for (const [role, re] of DOC_DIRS) {
    const m = re.exec(p);
    if (!m) continue;
    if (role === 'plan' && m[2].endsWith('-split') && m[2].length > 6) {
      return { slug: m[2].slice(0, -6), role: 'split', date: m[1] };
    }
    return { slug: m[2], role, date: m[1] };
  }
  for (const [role, re] of STATE_DIRS) {
    const m = re.exec(p);
    if (m) return { slug: m[1], role, date: null };
  }
  return null;
}

/** The last path segment of a repo id: "github.com/org/repo" is "repo". */
export const nameOf = repo => String(repo || '').split('/').pop();

const norm = remote => String(remote || '').trim().replace(/^[a-z]+:\/\//i, '').replace(/\.git$/, '').replace(/\/$/, '');

/** Group documents by devague slug: {groups: [{slug, docs: [{role, doc}]}], other: [doc]}.
 * Removed docs are left out; groups sort by slug, docs in a group by role order. */
export function groupBySlug(docs) {
  const bySlug = new Map();
  const other = [];
  for (const doc of docs || []) {
    if (doc.removed_at) continue;
    const s = slugOf(doc.path);
    if (!s) { other.push(doc); continue; }
    if (!bySlug.has(s.slug)) bySlug.set(s.slug, []);
    bySlug.get(s.slug).push({ role: s.role, doc });
  }
  const groups = [...bySlug.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([slug, list]) => ({ slug, docs: list.sort((a, b) => ROLES.indexOf(a.role) - ROLES.indexOf(b.role) || a.doc.id - b.doc.id) }));
  other.sort((a, b) => a.path.localeCompare(b.path));
  return { groups, other };
}

/** Rows of the #projects list from GET /api/projects, by name. */
export function projectRows(res) {
  return (res?.projects || []).map(p => ({
    repo: p.repo, name: nameOf(p.repo), domains: [...(p.domains || [])].sort(),
    connected: (p.connected || []).map(norm), doc_count: p.doc_count || 0, updated_at: p.updated_at || null,
  })).sort((a, b) => a.name.localeCompare(b.name) || a.repo.localeCompare(b.repo));
}

/** Rows of the #domains list from GET /api/domains: {domain, projects: [name], repos: [repo]}. */
export function domainRows(res) {
  return (res?.domains || []).map(d => ({
    domain: d.domain, repos: [...d.projects], projects: d.projects.map(nameOf).sort(),
  })).sort((a, b) => a.domain.localeCompare(b.domain));
}

const byRepo = docs => {
  const m = new Map();
  for (const d of docs || []) {
    if (d.removed_at) continue;
    if (!m.has(d.repo)) m.set(d.repo, []);
    m.get(d.repo).push(d);
  }
  return m;
};

/** A project page. ``docs`` is GET /api/repodocs?project=REPO&connected=1; ``projects`` is
 * GET /api/projects. {own: {groups, other}, connected: [{repo, name, groups, other}]}:
 * the project's own documents, and one entry per connected project (listed in culture.yaml
 * or present in ``docs``) with that project's documents. */
export function projectPage(docs, repo, projects) {
  const grouped = byRepo(docs);
  const row = (projects?.projects || []).find(p => p.repo === repo);
  const repos = new Set((row?.connected || []).map(norm));
  for (const r of grouped.keys()) if (r !== repo) repos.add(r);
  repos.delete(repo);
  return {
    own: groupBySlug(grouped.get(repo)),
    connected: [...repos].sort().map(r => ({ repo: r, name: nameOf(r), ...groupBySlug(grouped.get(r)) })),
  };
}

/** A domain page. ``docs`` is GET /api/repodocs?domain=DOMAIN: [{repo, name, groups, other}],
 * one per project in the domain. */
export function domainPage(docs, domain, projects) {
  const grouped = byRepo(docs);
  const repos = new Set(grouped.keys());
  for (const p of projects?.projects || []) if ((p.domains || []).includes(domain) && grouped.has(p.repo)) repos.add(p.repo);
  return [...repos].sort().map(r => ({ repo: r, name: nameOf(r), ...groupBySlug(grouped.get(r)) }));
}
