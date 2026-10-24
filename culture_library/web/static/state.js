/* Site state (prototype "state view"): four stat tiles, a status list (icon and label) and
 * an auto-fill chart grid with hover tooltips, a Table toggle and a source line.
 * Numbers come from GET /api/status, GET /api/health and the loaded item and repo-doc lists.
 */
import { $, KINDS, S, SVG, api, fill, fmtN, h, icon, kc, kindKey } from './app.js';

const when = iso => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
};
const plural = (n, one, many) => `${fmtN(n)} ${n === 1 ? one : many}`;

function tiles(st) {
  const it = st.counts.items, rd = st.counts.repodocs, ix = st.index;
  const repos = new Set(S.entries.filter(e => e.src === 'doc').map(e => e.repo)).size;
  const byKind = Object.entries(it.by_kind).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => plural(n, KINDS[kindKey(k)].label.toLowerCase(), KINDS[kindKey(k)].plural.toLowerCase())).join(', ');
  const indexed = ix.done + ix.pending + ix.failed;
  return [
    [fmtN(it.total), 'Items in the library', byKind || 'Nothing uploaded yet'],
    [fmtN(rd.total), 'Repo documents', `${plural(rd.versions, 'version', 'versions')} from ${plural(repos, 'repo', 'repos')}`],
    [indexed ? `${fmtN(ix.done)}/${fmtN(indexed)}` : '0', 'Items indexed for search', indexed ? `${fmtN(ix.pending)} waiting, ${fmtN(ix.failed)} failed` : 'Nothing queued'],
    [fmtN(st.feed.cursor), 'Feed events', st.feed.cursor ? 'Ordered change events at /api/feed' : 'No events yet'],
  ].map(t => h('div', { class: 'tile' }, h('div', { class: 'v' }, t[0]), h('div', { class: 'l' }, t[1]), h('div', { class: 'd' }, t[2])));
}
function newest(byKind, label) {
  const rows = Object.entries(byKind || {}).filter(([, v]) => v).sort((a, b) => String(b[1]).localeCompare(String(a[1])));
  return rows.length ? { at: rows[0][1], text: rows.map(([k, v]) => `${label(k)} ${when(v)}`).join(', ') } : null;
}
function statusRows(st, health) {
  const ix = st.index, rows = [];
  rows.push(['Library API', 'good', 'Running', `Version ${health ? health.version : 'unknown'}. It answered this page just now.`]);
  if (ix.failed) rows.push(['Search index', 'critical', `${fmtN(ix.failed)} failed`, ix.last_error ? `Last error: ${ix.last_error}` : 'Some items could not be indexed.']);
  else if (ix.pending) rows.push(['Search index', 'warning', `${fmtN(ix.pending)} waiting`, `The oldest job has waited ${fmtN(ix.lag_seconds)} seconds. Titles are searchable already.`]);
  else rows.push(['Search index', 'good', 'Up to date', ix.last_success_at ? `Last indexed ${when(ix.last_success_at * 1000)}.` : 'Nothing is waiting to be indexed.']);
  rows.push(st.private_included
    ? ['Private items', 'good', 'Included', 'You can see private items, so every count on this page includes them.']
    : ['Private items', 'warning', 'Hidden', 'This key cannot see private items, so the counts here are public items only.']);
  const items = newest(st.last_push.items.by_kind, k => KINDS[kindKey(k)].label);
  rows.push(items ? ['Newest item', 'good', when(items.at), `Newest per kind: ${items.text}.`] : ['Newest item', 'warning', 'None yet', 'Nothing has been uploaded or pushed.']);
  const docs = newest(st.last_push.repodocs.by_kind, k => k);
  rows.push(docs ? ['Newest repo document', 'good', when(docs.at), `Newest per type: ${docs.text}.`] : ['Newest repo document', 'warning', 'None yet', 'No repo has pushed a spec, plan or devague record.']);
  const feed = newest(st.last_push.feed, k => k.replace(/_/g, ' '));
  rows.push(feed ? ['Feed', 'good', plural(st.feed.cursor, 'event', 'events'), `Newest per type: ${feed.text}.`] : ['Feed', 'warning', 'Empty', 'No change events yet.']);
  return rows.map(r => h('div', { class: 'srow' }, h('span', { class: 'n' }, r[0]), h('span', { class: 'st' }, icon(SVG[r[1]]), r[2]), h('span', { class: 'x' }, r[3])));
}
function count(list, keyOf) {
  const m = new Map();
  for (const x of list) { const k = keyOf(x); m.set(k, (m.get(k) || 0) + 1); }
  return [...m.entries()];
}
function charts(st) {
  const items = S.entries.filter(e => e.src === 'item'), docs = S.entries.filter(e => e.src === 'doc');
  const mix = Object.keys(KINDS).map(k => {
    const n = Object.entries(st.counts.items.by_kind).filter(([kk]) => kindKey(kk) === k).reduce((a, [, v]) => a + v, 0) + (k === 'repo' ? st.counts.repodocs.total : 0);
    return [k, KINDS[k].plural, n];
  }).sort((a, b) => b[2] - a[2]);
  const months = count(items, i => i.at.slice(0, 7)).sort((a, b) => a[0].localeCompare(b[0])).slice(-12);
  const thisMonth = new Date().toISOString().slice(0, 7);
  const repos = count(docs, d => d.repo.split('/').pop()).sort((a, b) => b[1] - a[1]);
  const news = items.filter(i => i.kind === 'newspaper').sort((a, b) => a.at.localeCompare(b.at)).slice(-14);
  const formats = count(items, i => (i.format || '?').toUpperCase()).sort((a, b) => b[1] - a[1]);
  const docKinds = Object.entries(st.counts.repodocs.by_kind).sort((a, b) => b[1] - a[1]);
  return [
    { id: 'mix', title: 'What the library holds', sub: 'Items and repo documents by kind', type: 'h', data: mix, src: 'GET /api/status' },
    { id: 'months', title: 'Items added per month', sub: `${plural(items.length, 'item', 'items')}, dated by upload`, type: 'v',
      data: months.map(([m, n]) => [new Date(m + '-15').toLocaleDateString('en-GB', { month: 'short' }) + (m === thisMonth ? '*' : ''), n]), src: 'GET /api/library. *This month so far' },
    { id: 'repos', title: 'Largest repo records', sub: `Specs, plans and devague files per repo, top 10 of ${fmtN(repos.length)}`, type: 'h',
      data: repos.slice(0, 10).map(([r, n]) => [null, r, n]), src: 'GET /api/repodocs' },
    { id: 'paper', title: 'Newspaper issues', sub: `File size per daily issue, the last ${plural(news.length, 'issue', 'issues')}`, type: 'v',
      data: news.map(i => [i.at.slice(8, 10), +(i.size / 1024).toFixed(1)]), unit: ' KB', src: 'GET /api/library?kind=newspaper' },
    { id: 'formats', title: 'Items by format', sub: 'What was uploaded or pushed, by file format', type: 'h',
      data: formats.map(([f, n]) => [null, f, n]), src: 'GET /api/library' },
    { id: 'kinds', title: 'Repo documents by type', sub: 'Specs, plans and devague records pushed from repos', type: 'h',
      data: docKinds.map(([k, n]) => [null, k.charAt(0).toUpperCase() + k.slice(1), n]), src: 'GET /api/status' },
  ].filter(c => c.data.length && c.data.some(d => (c.type === 'h' ? d[2] : d[1]) > 0));
}
function hbars(c) {
  const max = Math.max(...c.data.map(d => d[2]));
  return h('div', { class: 'hbars' }, c.data.map(d => {
    const w = d[2] ? Math.max(0.6, d[2] / max * 100) : 0, col = d[0] ? kc(d[0]) : 'var(--accent)';
    return h('div', { class: 'hb' }, h('span', { class: 'lab', title: d[1] }, d[1]),
      h('span', { class: 'track', 'data-tip': `${d[1]}: ${fmtN(d[2])}${d[3] ? ', ' + d[3] : ''}` },
        d[2] ? h('span', { class: 'bar', style: { width: `calc(${w}% - 70px)`, background: col } }) : null,
        h('span', { class: 'val' }, fmtN(d[2]), d[3] ? [' ', h('span', { class: 'm' }, d[3])] : null)));
  }));
}
function nice(max) { if (!(max > 0)) return 1; const p = Math.pow(10, Math.floor(Math.log10(max))); const m = max / p; return (m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; }
function vcols(c) {
  const top = nice(Math.max(...c.data.map(d => d[1]))), ticks = [0, top / 2, top];
  return [
    h('div', { class: 'cols' },
      h('div', { class: 'yax' }, ticks.map(t => h('span', { style: { bottom: `${t / top * 100}%` } }, fmtN(t)))),
      h('div', { class: 'plot' }, ticks.slice(1).map(t => h('span', { class: 'grid-l', style: { bottom: `${t / top * 100}%` } })),
        c.data.map(d => h('span', { class: 'col', 'data-tip': `${d[0]}: ${fmtN(d[1])}${c.unit || ''}` }, h('span', { class: 'bar', style: { height: `${d[1] / top * 100}%` } }))))),
    h('div', { class: 'xlab' }, c.data.map(d => h('span', {}, d[0]))),
  ];
}
function table(c) {
  const rows = c.type === 'h' ? c.data.map(d => [d[1], fmtN(d[2]) + (d[3] ? ' (' + d[3] + ')' : '')]) : c.data.map(d => [d[0], fmtN(d[1]) + (c.unit || '')]);
  return h('table', { class: 'data' }, h('tr', {}, h('th', {}, c.type === 'h' ? 'Category' : 'Period'), h('th', {}, 'Value')),
    rows.map(r => h('tr', {}, h('td', {}, r[0]), h('td', {}, r[1]))));
}
let drawing = 0;
export async function draw() {
  const my = ++drawing;
  let st, health = null;
  try {
    [st, health] = await Promise.all([api('/api/status'), api('/api/health').catch(() => null)]);
  } catch (e) {
    if (my !== drawing) return;
    $('state-lede').textContent = `The library did not answer (${e.detail || e.message}). Reload the page to try again.`;
    fill($('tiles')); fill($('status')); fill($('charts'));
    return;
  }
  if (my !== drawing) return;
  const now = new Date().toLocaleString('en-GB', { day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  $('state-lede').textContent = `Measured from this library's API on ${now}. ${st.private_included ? 'Private items are included.' : 'Public items only: this key cannot see private ones.'}`;
  fill($('tiles'), tiles(st));
  fill($('status'), statusRows(st, health));
  fill($('charts'), charts(st).map(c => h('section', { class: 'cc', id: 'c-' + c.id },
    h('h3', {}, c.title), h('div', { class: 'sub' }, c.sub), h('button', { class: 'tog', 'data-tog': c.id }, 'Table'),
    h('div', { class: 'viz' }, c.type === 'h' ? hbars(c) : vcols(c)), h('div', { class: 'tbl', hidden: true }, table(c)),
    h('div', { class: 'src' }, `Source: ${c.src}`))));
}
export function init() {
  $('charts').addEventListener('click', e => {
    const g = e.target.closest('[data-tog]'); if (!g) return;
    const cc = $('c-' + g.dataset.tog), tb = cc.querySelector('.tbl'), vz = cc.querySelector('.viz');
    const on = tb.hidden; tb.hidden = !on; vz.hidden = on; g.textContent = on ? 'Chart' : 'Table';
  });
}
