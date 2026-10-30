/* Browse by project and by domain (plan task t20). Routes (hashroute.js): #projects,
 * #projects/<repo>, #domains and #domains/<domain>. The pure model (the slug join) is
 * projects-model.js.
 *
 * Data: GET /api/projects and GET /api/domains for the lists; GET /api/repodocs with the
 * project=, connected=1 and domain= filters for the pages. A document opens in the reader
 * (#doc-N, the repo-doc list app.js already loaded). Names, paths and domains are content:
 * they reach the page as text nodes via h(), with dir="auto". No markup is parsed here.
 */
import { $, api, fill, fmtN, h } from './app.js';
import { domainsHash, projectsHash } from './hashroute.js';
import * as M from './projects-model.js';

const plural = (n, one, many) => `${fmtN(n)} ${n === 1 ? one : many}`;
const text = (tag, cls, s) => h(tag, { class: cls, dir: 'auto' }, s);
let token = 0;

const chips = (names, hashOf) => h('span', { class: 'pj-chips' }, names.map(n => h('a', { class: 'pj-chip', href: hashOf(n), dir: 'auto' }, n)));
const crumb = (...parts) => h('div', { class: 'pj-crumb' }, parts.flatMap((p, i) => (i ? [' / ', p] : [p])));
const intro = (title, lede) => h('section', { class: 'pj-intro' }, h('div', {}, text('h1', 'pj-title', title), h('p', {}, lede)));
const empty = msg => h('p', { class: 'empty' }, msg);

function docLink(role, d) {
  return h('li', { class: 'pj-doc' },
    role ? h('span', { class: 'pj-role' }, M.ROLE_LABELS[role]) : null,
    h('a', { href: `#doc-${d.id}`, dir: 'auto' }, d.path));
}
function slugGroup(g) {
  return h('div', { class: 'pj-group' }, text('h3', 'pj-slug', g.slug), h('ul', { class: 'pj-docs' }, g.docs.map(x => docLink(x.role, x.doc))));
}
/** One project's documents: each slug's group, then the ungrouped ones under "Other". */
function docsBlock({ groups, other }) {
  if (!groups.length && !other.length) return empty('No documents.');
  return h('div', {}, groups.map(slugGroup),
    other.length ? h('div', { class: 'pj-group' }, h('h3', { class: 'pj-slug' }, 'Other'), h('ul', { class: 'pj-docs' }, other.map(d => docLink(null, d)))) : null);
}
const projectCard = (title, repo, body) => h('section', { class: 'pj-card' },
  h('div', { class: 'pj-head' }, h('h2', { dir: 'auto' }, h('a', { href: projectsHash(repo) }, title)), h('span', { class: 'pj-repo', dir: 'auto' }, repo)), body);

/* ---------- pages ---------- */
function projectList(res) {
  const rows = M.projectRows(res);
  return [
    intro('Projects', 'Every repo that has pushed documents, with its domains and how many documents it holds.'),
    h('div', { class: 'pj-grid' }, rows.length ? rows.map(r => h('a', { class: 'pj-tile', href: projectsHash(r.repo) },
      text('span', 'pj-name', r.name),
      h('span', { class: 'pj-repo', dir: 'auto' }, r.repo),
      h('span', { class: 'pj-meta' }, plural(r.doc_count, 'document', 'documents'),
        r.connected.length ? `, ${plural(r.connected.length, 'connected project', 'connected projects')}` : ''),
      h('span', { class: 'pj-chips' }, r.domains.map(d => h('span', { class: 'pj-chip', dir: 'auto' }, d))))) : [empty('No project has documents this key can see.')]),
    h('p', { class: 'pj-foot' }, 'Source: GET /api/projects.'),
  ];
}
function domainList(res) {
  const rows = M.domainRows(res);
  return [
    intro('Domains', 'The areas projects declare in their culture.yaml, with the projects in each.'),
    h('div', { class: 'pj-grid' }, rows.length ? rows.map(r => h('a', { class: 'pj-tile', href: domainsHash(r.domain) },
      text('span', 'pj-name', r.domain),
      h('span', { class: 'pj-meta' }, plural(r.projects.length, 'project', 'projects')),
      h('span', { class: 'pj-chips' }, r.projects.map(p => h('span', { class: 'pj-chip', dir: 'auto' }, p))))) : [empty('No domain has documents this key can see.')]),
    h('p', { class: 'pj-foot' }, 'Source: GET /api/domains.'),
  ];
}
function projectView(repo, docs, projects) {
  const page = M.projectPage(docs.docs, repo, projects);
  const row = M.projectRows(projects).find(r => r.repo === repo);
  return [
    crumb(h('a', { href: '#projects' }, 'Projects'), M.nameOf(repo)),
    intro(M.nameOf(repo), repo),
    row?.domains.length ? h('p', { class: 'pj-domains' }, 'Domains: ', chips(row.domains, domainsHash)) : null,
    h('section', { class: 'pj-card' }, h('h2', {}, 'Documents'), docsBlock(page.own)),
    h('section', { class: 'pj-card' }, h('h2', {}, 'Connected projects'),
      page.connected.length ? page.connected.map(c => projectCard(c.name, c.repo, docsBlock(c))) : empty('No connected projects.')),
    h('p', { class: 'pj-foot' }, 'Source: GET /api/repodocs?project=...&connected=1. Slugs join a spec, plan, delivery and frame; the file name and date do not matter.'),
  ];
}
function domainView(domain, docs, projects) {
  const page = M.domainPage(docs.docs, domain, projects);
  return [
    crumb(h('a', { href: '#domains' }, 'Domains'), domain),
    intro(domain, `${plural(page.length, 'project', 'projects')} in this domain.`),
    page.length ? page.map(p => projectCard(p.name, p.repo, docsBlock(p))) : empty('No documents in this domain.'),
    h('p', { class: 'pj-foot' }, 'Source: GET /api/repodocs?domain=...'),
  ];
}

/** Show a browse page: ``view`` is 'projects' or 'domains', ``name`` a repo id or domain, or null for the list. */
export async function show(view, name) {
  const mine = ++token;
  const body = $('projects-body');
  fill(body, h('p', { class: 'empty' }, 'Loading...'));
  let nodes;
  try {
    if (view === 'projects' && !name) nodes = projectList(await api('/api/projects'));
    else if (view === 'domains' && !name) nodes = domainList(await api('/api/domains'));
    else if (view === 'projects') {
      const [docs, projects] = await Promise.all([
        api(`/api/repodocs?project=${encodeURIComponent(name)}&connected=1`), api('/api/projects')]);
      nodes = projectView(name, docs, projects);
    } else {
      const [docs, projects] = await Promise.all([
        api(`/api/repodocs?domain=${encodeURIComponent(name)}`), api('/api/projects')]);
      nodes = domainView(name, docs, projects);
    }
  } catch (e) {
    if (mine === token) fill(body, h('p', { class: 'empty', role: 'alert' }, `Could not load this page: ${e.detail || e.message}`));
    return;
  }
  if (mine === token) fill(body, nodes);
}
