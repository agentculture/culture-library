/* Culture Library site: shared state, the API client, views and boot.
 *
 * Ported from docs/design/prototype.html (approved 2026-10-24; contract:
 * docs/design/library-site.md). The prototype's single inline script is split into
 * app.js (this file), home.js, reader.js, search.js, upload.js, send.js and state.js:
 * plain ES modules, no build step, no framework. Data comes from the library API instead
 * of the prototype's embedded DATA block.
 *
 * Content is untrusted (claim c38): every string from an item or a repo document reaches
 * the page through text nodes (h() below). innerHTML is used only for this module's own
 * constant SVG icons and for the server's escaped devague fragment (reader.js).
 * Per-browser state (theme, last read item, pane state, reading position) lives in
 * localStorage behind try/catch, exactly like the prototype; a local-development key
 * lives in sessionStorage only.
 */
import * as home from './home.js';
import * as reader from './reader.js';
import * as search from './search.js';
import * as send from './send.js';
import * as siteState from './state.js';
import * as upload from './upload.js';

export const KINDS = {
  newspaper: { label: 'Newspaper', plural: 'Newspapers' },
  document: { label: 'Document', plural: 'Documents' },
  book: { label: 'Book', plural: 'Books' },
  transcript: { label: 'Transcript', plural: 'Transcripts' },
  repo: { label: 'Spec or plan', plural: 'Specs and plans' },
  diary: { label: 'Diary', plural: 'Diary' },
};
// API kinds (core.models.KINDS) to the six display kinds and their colors.
const KIND_OF = { spec: 'repo', plan: 'repo', 'devague-record': 'repo', article: 'document' };
export const kindKey = k => KIND_OF[k] || (KINDS[k] ? k : 'document');
export const kc = k => `var(--k-${k})`;
export const fmtSize = b => b > 1e6 ? (b / 1e6).toFixed(1) + ' MB' : (b / 1024).toFixed(1) + ' KB';
export const fmtN = n => Number(n || 0).toLocaleString('en-US');
// Browser storage holds per-viewer conveniences only: keys come from a fixed vocabulary and
// values are short strings, so nothing read back can carry markup or grow unbounded.
const KEY_RE = /^[a-z][a-z0-9._-]{0,63}$/;
const VALUE_MAX = 512;
const safeKey = k => (KEY_RE.test(String(k)) ? 'lib.' + k : null);
const safeValue = v => String(v).slice(0, VALUE_MAX);
const wrap = area => ({
  get(k) {
    const key = safeKey(k);
    if (!key) return null;
    try { const v = area().getItem(key); return v === null ? null : safeValue(v); } catch (e) { return null; }
  },
  set(k, v) {
    const key = safeKey(k);
    if (!key) return;
    try { area().setItem(key, safeValue(v)); } catch (e) { /* storage unavailable */ }
  },
  del(k) {
    const key = safeKey(k);
    if (!key) return;
    try { area().removeItem(key); } catch (e) { /* storage unavailable */ }
  },
});
export const store = wrap(() => localStorage);
export const session = wrap(() => sessionStorage);

export const SVG = {
  sun: '<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="3.6" fill="currentColor"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M9 1.5v1.8M9 14.7v1.8M1.5 9h1.8M14.7 9h1.8M3.7 3.7l1.3 1.3M13 13l1.3 1.3M3.7 14.3L5 13M13 5l1.3-1.3"/></g></svg>',
  moon: '<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><path d="M14.5 11.2A6.2 6.2 0 016.8 3.5 6.3 6.3 0 1014.5 11.2z" fill="currentColor"/></svg>',
  dev: '<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><rect x="4" y="1.5" width="10" height="15" rx="2" stroke="currentColor" stroke-width="1.6" fill="none"/><path d="M7 13.5h4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  good: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="7" fill="var(--good)"/><path d="M4 7.2l2 2 4-4.4" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round"/></svg>',
  warning: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M7 1l6.5 12H.5z" fill="var(--warning)"/><path d="M7 5.5v3.2M7 10.6v.2" stroke="#0b0b0b" stroke-width="1.6" stroke-linecap="round"/></svg>',
  critical: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="7" fill="var(--critical)"/><path d="M4.5 4.5l5 5M9.5 4.5l-5 5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>',
};

/* ---------- DOM ---------- */
function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false || k === '') continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
/** Build an element. Strings become text nodes; never markup. */
export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (/^on/i.test(k)) throw new Error('event handler attributes are not allowed');
    if (k === 'class') el.className = v;
    else if (k === 'style') for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  return append(el, kids);
}
/** One of this module's constant SVG strings (never content) as a node. */
export function icon(svgText) {
  const t = document.createElement('template');
  t.innerHTML = svgText;
  return t.content.firstElementChild;
}
// Function declarations are hoisted: the other modules may call these while app.js is
// still being evaluated (they import it circularly).
export function $(id) { return document.getElementById(id); }
export function fill(el, ...kids) { el.replaceChildren(); return append(el, kids); }
export const chip = k => h('span', { class: 'chip' }, h('i', { style: { background: kc(k) } }), KINDS[k].label);

/* ---------- API ---------- */
export class ApiError extends Error {
  constructor(status, code, detail) { super(detail); this.status = status; this.code = code; this.detail = detail; }
}
export async function api(path, opts = {}) {
  const headers = { 'X-Library-Request': '1', Accept: 'application/json' };
  const key = session.get('key');
  if (key) headers.Authorization = 'Bearer ' + key;
  let body = opts.body;
  if (opts.json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(opts.json); }
  let r;
  try {
    r = await fetch(path, { method: opts.method || 'GET', headers, body, credentials: 'same-origin', cache: 'no-store' });
  } catch (e) {
    throw new ApiError(0, 'network', 'could not reach the library');
  }
  if (r.status === 204) return null;
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    const d = data && typeof data.detail === 'object' && data.detail ? data.detail : (data || {});
    if (r.status === 401) askForKey();
    throw new ApiError(r.status, d.code || 'error', d.detail || r.statusText || 'request failed');
  }
  return data;
}
export async function apiText(path) {
  const headers = { 'X-Library-Request': '1' };
  const key = session.get('key');
  if (key) headers.Authorization = 'Bearer ' + key;
  let r;
  try { r = await fetch(path, { headers, credentials: 'same-origin', cache: 'no-store' }); } catch (e) { throw new ApiError(0, 'network', 'could not reach the library'); }
  if (!r.ok) { if (r.status === 401) askForKey(); throw new ApiError(r.status, 'error', r.statusText); }
  return r.text();
}

/* ---------- connect (local development only) ---------- */
let asking = false;
function askForKey() {
  const dlg = $('connect');
  if (asking || dlg.open) return;
  asking = true;
  $('cn-err').hidden = !session.get('key');
  $('cn-err').textContent = 'That key was refused. Check it, or mint a new one with culture-library keys.';
  $('cn-key').value = '';
  dlg.showModal();
}
function initConnect() {
  const dlg = $('connect');
  $('cn-cancel').addEventListener('click', () => dlg.close());
  dlg.addEventListener('close', () => { asking = false; });
  $('cn-form').addEventListener('submit', async e => {
    e.preventDefault();
    const key = $('cn-key').value.trim();
    if (!key) { $('cn-err').textContent = 'Paste a key first, or close this and sign in through Cloudflare Access.'; $('cn-err').hidden = false; return; }
    session.set('key', key);
    dlg.close();
    await load();
  });
}

/* ---------- data ---------- */
export const S = {
  entries: [], current: null, homeFilter: 'all', paneFilter: 'all', query: '', hits: null,
  sends: {}, loaded: false,
};
const pad = n => String(n).padStart(2, '0');
export function localAt(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '1970-01-01 00:00';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export const repoShort = repo => String(repo || '').split('/').slice(-2).join('/');
export const repoName = repo => String(repo || '').split('/').pop();
function docTitle(path) {
  const base = path.split('/').pop().replace(/\.[^.]+$/, '').replace(/^\d{4}-\d{2}-\d{2}-/, '');
  const words = base.replace(/[-_]+/g, ' ').trim() || path;
  if (path.startsWith('.devague/frames/')) return `${words} (devague frame)`;
  if (path.startsWith('.devague/plans/')) return `${words} (devague plan)`;
  return words.charAt(0).toUpperCase() + words.slice(1);
}
export function docSuffix(path) {
  if (path.startsWith('.devague/frames/')) return ' (devague frame)';
  if (path.startsWith('.devague/plans/')) return ' (devague plan)';
  return '';
}
function fromItem(i) {
  return {
    key: 'item-' + i.id, src: 'item', id: i.id, kind: kindKey(i.kind), apiKind: i.kind, title: i.title,
    author: i.author, at: localAt(i.created_at), size: i.size, format: i.format, vis: i.visibility,
    sha: i.sha256, live: true,
  };
}
function fromDoc(d) {
  const ext = (d.path.match(/\.([a-z0-9]+)$/i) || [, ''])[1].toLowerCase();
  return {
    key: 'doc-' + d.id, src: 'doc', id: d.id, kind: 'repo', apiKind: d.kind, title: docTitle(d.path),
    heading: null, at: localAt(d.updated_at), size: 0, format: ext, vis: d.visibility, repo: d.repo,
    path: d.path, live: true,
  };
}
async function loadItems() {
  const out = [];
  for (let offset = 0; ; offset += 1000) {
    const r = await api(`/api/library?limit=1000&offset=${offset}`);
    out.push(...r.items);
    if (r.items.length < 1000) return out;
  }
}
async function loadDocs() {
  try { return (await api('/api/repodocs')).docs; } catch (e) { if (e.status === 401) throw e; return []; }
}
export async function reloadEntries() {
  const [items, docs] = await Promise.all([loadItems(), loadDocs()]);
  const old = new Map(S.entries.map(e => [e.key, e]));
  S.entries = [...items.map(fromItem), ...docs.map(fromDoc)].map(e => {
    const was = old.get(e.key);
    if (was && was.excerpt !== undefined) { e.excerpt = was.excerpt; e.heading = was.heading; if (was.heading) e.title = was.title; }
    return e;
  });
  S.loaded = true;
}
export const sorted = () => [...S.entries].sort((a, b) => b.at.localeCompare(a.at) || b.id - a.id);
export const byKey = k => S.entries.find(i => i.key === k);

/** Fetch card excerpts (and repo-doc titles) for entries that have none; true if any arrived. */
export async function fetchExcerpts(entries) {
  const need = entries.filter(e => e.excerpt === undefined);
  if (!need.length) return false;
  for (let n = 0; n < need.length; n += 200) {
    const part = need.slice(n, n + 200);
    const items = part.filter(e => e.src === 'item').map(e => e.id).join(',');
    const docs = part.filter(e => e.src === 'doc').map(e => e.id).join(',');
    let r;
    try { r = await api(`/api/excerpts?items=${items}&docs=${docs}`); } catch (e) { return false; }
    for (const e of part) {
      e.excerpt = '';
      if (e.src === 'item' && r.items[e.id] !== undefined) e.excerpt = r.items[e.id];
      const d = e.src === 'doc' ? r.docs[e.id] : null;
      if (d) {
        e.excerpt = d.excerpt || '';
        if (d.title) { e.heading = d.title; e.title = d.title + docSuffix(e.path); }
      }
    }
  }
  return true;
}

/* ---------- theme ---------- */
const themeBtn = $('theme');
function effectiveTheme() {
  const t = document.documentElement.dataset.theme;
  if (t === 'dark' || t === 'light') return t;
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
function paintThemeBtn() {
  const dark = effectiveTheme() === 'dark';
  themeBtn.replaceChildren(icon(dark ? SVG.sun : SVG.moon));
  themeBtn.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
}
const savedTheme = store.get('theme');
if (savedTheme === 'dark' || savedTheme === 'light') document.documentElement.dataset.theme = savedTheme;
themeBtn.addEventListener('click', () => {
  const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next; store.set('theme', next); paintThemeBtn();
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', paintThemeBtn);
paintThemeBtn();

/* ---------- pane state ---------- */
const split = $('split');
export const narrow = () => matchMedia('(max-width: 900px)').matches;
export function setPane(open) {
  split.classList.toggle('closed', !open);
  $('pane-lbl').textContent = open ? 'Hide list' : 'Show list';
  $('pane-btn').setAttribute('aria-expanded', String(open));
  if (!narrow()) store.set('pane', open ? 'open' : 'closed');
}

/* ---------- views ---------- */
export function show(view) {
  for (const v of ['home', 'read', 'state']) $('view-' + v).hidden = v !== view;
  document.querySelectorAll('.tabs button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.view === (view === 'read' ? 'home' : view))));
  if (view === 'state') void siteState.draw();
}
export function openItem(key, keepScroll) {
  const i = byKey(key); if (!i) return goHome();
  S.current = key; store.set('last', key);
  reader.render(i);
  send.paintSendBtn(i);
  const wasRead = !$('view-read').hidden;
  show('read'); reader.drawRows();
  if (!wasRead) { if (narrow()) setPane(false); else setPane(store.get('pane') !== 'closed'); }
  else if (narrow()) setPane(false);
  if (location.hash !== '#' + key) history.replaceState(null, '', '#' + key);
  if (!keepScroll) window.scrollTo({ top: 0 });
  const row = document.querySelector('.row[aria-current="true"]'); if (row) row.scrollIntoView({ block: 'nearest' });
}
export function step(d) {
  const list = search.visibleList(), ix = list.findIndex(i => i.key === S.current);
  const nx = list[ix + d]; if (nx) openItem(nx.key);
}
export function goHome() { S.current = null; show('home'); home.drawHome(); history.replaceState(null, '', '#home'); window.scrollTo({ top: 0 }); }
function route() {
  const h = location.hash.slice(1);
  if (h === 'state') return show('state');
  if (h && byKey(h)) return openItem(h);
  S.current = null; show('home'); home.drawHome();
}
let scrollT;
window.addEventListener('scroll', () => {
  if (!S.current) return;
  clearTimeout(scrollT);
  scrollT = setTimeout(() => {
    const max = document.documentElement.scrollHeight - innerHeight;
    if (max > 0) store.set('pos.' + S.current, Math.min(100, Math.round(scrollY / max * 100)));
  }, 200);
}, { passive: true });

/* ---------- events ---------- */
document.addEventListener('click', e => {
  const t = e.target;
  const o = t.closest('[data-open]'); if (o) { openItem(o.dataset.open); return; }
  if (t.closest('[data-home]')) { e.preventDefault(); goHome(); return; }
  if (t.closest('[data-pane-toggle]')) { setPane(split.classList.contains('closed')); return; }
  const hf = t.closest('[data-hfilter]'); if (hf) { S.homeFilter = hf.dataset.hfilter; home.drawHome(); return; }
  const pf = t.closest('[data-pfilter]'); if (pf) { S.paneFilter = pf.dataset.pfilter; reader.drawRows(); return; }
  const v = t.closest('[data-view]'); if (v) { if (v.dataset.view === 'state') { show('state'); history.replaceState(null, '', '#state'); window.scrollTo({ top: 0 }); } else goHome(); }
});
$('prev').addEventListener('click', () => step(-1));
$('next').addEventListener('click', () => step(1));
document.addEventListener('keydown', e => { if (e.key === 'Escape' && narrow() && !split.classList.contains('closed')) setPane(false); });
const tip = $('tip');
document.addEventListener('mousemove', e => {
  const t = e.target.closest('[data-tip]');
  if (!t) { tip.hidden = true; return; }
  tip.textContent = t.dataset.tip; tip.hidden = false;
  tip.style.left = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8) + 'px'; tip.style.top = (e.clientY + 16) + 'px';
});
window.addEventListener('hashchange', route);

/* ---------- boot ---------- */
async function load() {
  try {
    await reloadEntries();
  } catch (e) {
    if (e.status !== 401) home.drawError(e);
    return;
  }
  home.drawShelf();
  route();
  // Repo-doc titles live in their content: fetch them (and card excerpts) in the background.
  if (await fetchExcerpts(S.entries.filter(x => x.src === 'doc'))) {
    home.drawShelf();
    if (S.current) { reader.drawRows(); reader.refreshHeading(byKey(S.current)); } else if (!$('view-home').hidden) home.drawHome();
  }
}
initConnect();
home.init(); search.init(); send.init(); upload.init(); siteState.init();
home.drawShelf(); home.drawHome();
void load();
