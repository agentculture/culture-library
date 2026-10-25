/* Reader: the list pane (rows grouped by day, kind filters, search snippets) and the
 * per-kind readers (prototype "pane list" and "reader").
 *
 * Item text comes from GET /api/library/{id}/text (extracted plain text; a to-do checklist
 * reads its Markdown from /file and is parsed by checklist.js) and is inserted
 * as text nodes only, except an EPUB section's http(s) source links, which links.js builds
 * as <a> elements with DOM properties. Specs (Markdown) and devague frames (JSON) are parsed here into the
 * prototype's layouts, again as text nodes. Other devague JSON (plans) uses the server's
 * fragment from GET /api/repodocs/{id}/rendered, inserted as HTML because
 * culture_library.render.devague.render escapes every input string.
 */
import { $, KINDS, S, api, apiText, chip, fill, fmtSize, h, kc, repoShort } from './app.js';
import { parseChecklist } from './checklist.js';
import { linkedNodes } from './links.js';
import * as search from './search.js';
import * as send from './send.js';

const dayLabel = d => new Date(d + 'T12:00:00').toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });
const dayMonth = d => new Date(d + 'T12:00:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'long' });
const fullDay = d => new Date(d + 'T12:00:00').toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }).replace(',', '');
/** The publication title without a trailing ISO date ("Daily newspaper 2026-10-23" -> "Daily newspaper"). */
const bareTitle = t => stripTrailingDate(String(t)).trim() || String(t);
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const DATE_SEP = /[\s,\-–—:]/;
/** ``s`` without a trailing "(YYYY-MM-DD)" or "YYYY-MM-DD" and the separators before it: what
 * s.replace(/[\s,\-–—:]*\(?\d{4}-\d{2}-\d{2}\)?\s*$/, '') gives, as a linear scan. */
function stripTrailingDate(s) {
  let end = s.trimEnd().length;
  if (s[end - 1] === ')') end--;
  if (end < 10 || !ISO_DATE.test(s.slice(end - 10, end))) return s;
  end -= 10;
  if (s[end - 1] === '(') end--;
  while (end > 0 && DATE_SEP.test(s[end - 1])) end--;
  return s.slice(0, end);
}
let token = 0;

/* ---------- highlighting (text nodes + <mark>) ---------- */
function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`); }
/** ``text`` as nodes, with the current query's terms wrapped in <mark>. */
export function hl(text) {
  const s = String(text ?? '');
  const terms = search.terms();
  if (!terms.length || !s) return [s];
  const re = new RegExp(terms.map(escRe).join('|'), 'gi');
  const out = [];
  let last = 0;
  for (const m of s.matchAll(re)) {
    if (!m[0]) continue;
    out.push(s.slice(last, m.index), h('mark', {}, m[0]));
    last = m.index + m[0].length;
  }
  out.push(s.slice(last));
  return out;
}

/* ---------- pane list ---------- */
export function drawRows() {
  fill($('pane-filters'), filterButtons());
  const q = S.query.trim(), list = search.visibleList();
  if (!list.length) {
    fill($('rows'), h('p', { class: 'empty' }, S.loaded
      ? `Nothing matches “${q}”. Search covers every item's title and full text, and repo documents by title. Try another word.`
      : 'Loading the library…'));
    return;
  }
  const rows = [];
  let last = '';
  for (const i of list) {
    const d = i.at.slice(0, 10);
    if (d !== last) { rows.push(h('div', { class: 'day' }, dayLabel(d))); last = d; }
    const sn = q ? search.snippetFor(i.key) : null;
    rows.push(h('button', { class: 'row', dir: 'auto', 'data-open': i.key, 'aria-current': String(S.current === i.key) },
      h('i', { class: 'sw', style: { background: kc(i.kind) } }),
      h('span', { class: 't', dir: 'auto' }, i.title),
      h('span', { class: 's' }, i.src === 'item' ? fmtSize(i.size) : (i.format || '').toUpperCase()),
      sn ? h('span', { class: 'snip', dir: 'auto' }, sn) : null));
  }
  fill($('rows'), rows);
}
function filterButtons() {
  const kinds = ['all', ...Object.keys(KINDS).filter(k => S.entries.some(i => i.kind === k))];
  return kinds.map(k => {
    const n = k === 'all' ? S.entries.length : S.entries.filter(i => i.kind === k).length;
    return h('button', { 'data-pfilter': k, 'aria-pressed': String(S.paneFilter === k) }, `${k === 'all' ? 'All' : KINDS[k].plural} ${n}`);
  });
}

/* ---------- shared pieces ---------- */
const note = text => h('p', { class: 'note', dir: 'auto' }, text);
const loading = () => note('Loading…');
function failed(e) {
  if (e.status === 404) return note('This is no longer in the library, or it is private to someone else.');
  return note(`The library could not load this (${e.detail || e.message}). Try again in a moment.`);
}
function head(i, extra) {
  return h('div', { class: 'reader-head' }, chip(i.kind),
    i.live ? null : h('span', { class: 'chip soon' }, 'Arriving'),
    i.vis === 'private' ? h('span', { class: 'chip soon' }, 'Only me') : null,
    extra);
}
function paragraphs(text) {
  return String(text).split(/\n\s*\n/).map(p => p.trim()).filter(Boolean).map(p => h('p', { class: 'para', dir: 'auto' }, hl(p)));
}

/** A to-do checklist: read-only boxes; culture-rules is its only writer (#11). */
function checklist(text) {
  const out = [];
  let list = null;
  for (const row of parseChecklist(text)) {
    if (row.type !== 'check') {
      list = null;
      out.push(row.type === 'heading'
        ? h('h3', { class: 'sh', dir: 'auto' }, hl(row.text))
        : h('p', { class: 'para', dir: 'auto' }, hl(row.text)));
      continue;
    }
    if (!list) { list = h('ul', { class: 'todo' }); out.push(list); }
    list.append(h('li', { class: row.done ? 'done' : null, dir: 'auto' },
      h('input', { type: 'checkbox', disabled: true, checked: row.done, 'aria-label': row.done ? 'Done' : 'Not done' }),
      h('span', {}, hl(row.text))));
  }
  return out;
}

/* ---------- library items ---------- */
/** One EPUB section: its heading, then its paragraphs (a list for newspapers). */
function sectionNodes(i, s) {
  const out = [];
  if (s.heading) out.push(h('h3', { class: 'sh', dir: 'auto' }, hl(s.heading)));
  // Source links (#18): s.links[n] lists paragraph n's http(s) links, built by links.js.
  const tag = i.kind === 'newspaper' ? 'li' : 'p';
  const paras = s.paragraphs.map((p, n) => h(tag, { class: 'para', dir: 'auto' }, linkedNodes(p, s.links?.[n], hl)));
  out.push(tag === 'li' ? h('ul', {}, paras) : paras);
  return out;
}
function itemBody(i, t) {
  const kids = [];
  if (t.sections?.length) {
    for (const s of t.sections) kids.push(...sectionNodes(i, s));
  } else if (t.text && i.format === 'pdf') {
    kids.push(paragraphs(t.text));
  } else if (t.text) {
    kids.push(h('pre', { class: 'text', dir: 'auto' }, hl(t.text)));
  } else {
    const why = { pdftotext_missing: 'The server has no PDF text extractor installed (pdftotext).', pdftotext_failed: 'The PDF text could not be extracted.' }[t.reason];
    kids.push(note(why || 'This file has no text to show.'));
  }
  kids.push(note(`Item ${i.id}, sha256 ${String(i.sha).slice(0, 12)}…`));
  return kids;
}
function renderItem(i, art, my) {
  const meta = [h('span', {}, fullDay(i.at.slice(0, 10))), h('span', {}, `${(i.format || '').toUpperCase()}, ${fmtSize(i.size)}`)];
  if (i.author) meta.push(h('span', { dir: 'auto' }, `from ${i.author}`));
  const prose = h('div', { class: 'prose', dir: 'auto' }, loading());
  fill(art, head(i, meta), h('h2', { class: 'title', dir: 'auto' }, bareTitle(i.title)), prose);
  // A to-do checklist needs its Markdown markers, which /text strips: read the file itself.
  // It is still untrusted text, so parseChecklist output only ever becomes text nodes.
  const body = i.apiKind === 'todo' && i.format === 'md'
    ? apiText(`/api/library/${i.id}/file`).then(md => [...checklist(md), note(`Item ${i.id}, sha256 ${String(i.sha).slice(0, 12)}…`)])
    : api(`/api/library/${i.id}/text`).then(t => itemBody(i, t));
  body.then(kids => { if (my === token) fill(prose, kids); }, e => { if (my === token) fill(prose, failed(e)); });
}

/* ---------- repo documents ---------- */
const unescMd = s => s.replace(/\\([\\`*_{}[\]()#+\-.!>|])/g, '$1');
/** Parse an exported devague spec (Markdown) into title, lede and sections of items. */
export function parseSpec(md) {
  const st = { out: { title: null, lede: [], sections: [] }, sec: null, item: null };
  for (const raw of md.split('\n')) specLine(st, raw.trimEnd());
  return st.out;
}
/** One (right-trimmed) line of a spec: the title, a section heading, the lede, or section body. */
function specLine(st, line) {
  const { out } = st;
  const title = out.title ? null : /^# (.+)/.exec(line);
  if (title) { out.title = unescMd(title[1].trim()); return; }
  const heading = /^## (.+)/.exec(line);
  if (heading) {
    st.sec = { name: unescMd(heading[1].trim()), items: [], paras: [] };
    out.sections.push(st.sec);
    st.item = null;
    return;
  }
  if (!st.sec) {
    const quote = /^>\s?(.*)/.exec(line);
    if (quote) out.lede.push(unescMd(quote[1]));
    return;
  }
  if (line.trim()) sectionLine(st, line);
}
/** A non-blank line inside a section: a list item, a sub-item, a continuation or a paragraph. */
function sectionLine(st, line) {
  const li = /^[-*] (.*)/.exec(line);
  if (li) {
    st.item = { text: unescMd(li[1]), subs: [] };
    st.sec.items.push(st.item);
    return;
  }
  const subItem = st.item ? /^\s+[-*] (.*)/.exec(line) : null;
  if (subItem) { st.item.subs.push(specSub(unescMd(subItem[1]))); return; }
  if (st.item && /^\s/.test(line)) { st.item.text += ' ' + unescMd(line.trim()); return; }
  st.sec.paras.push(unescMd(line.trim()));
}
/** "Key: text" sub-items carry a key (a word, then words and spaces, up to the first colon). */
function specSub(t) {
  const k = /^\w[\w ]*:/.exec(t);
  if (!k) return { key: '', text: t };
  return { key: k[0].slice(0, -1).toLowerCase(), text: t.slice(k[0].length).trimStart() };
}
const isSpec = p => p.lede.length > 0 || p.sections.some(s => /^requirements$/i.test(s.name));
function sub(s) {
  return h('div', { class: 'q', dir: 'auto' }, s.key ? [h('b', {}, s.key.charAt(0).toUpperCase() + s.key.slice(1) + ':'), ' '] : null, hl(s.text));
}
function specBody(p, versions) {
  const kids = [];
  if (p.lede.length) kids.push(h('p', { class: 'lede', dir: 'auto' }, hl(p.lede.join(' ').trim())));
  const ba = p.sections.find(s => /^before\b/i.test(s.name));
  if (ba) {
    const pick = w => (ba.items.find(x => x.text.toLowerCase().startsWith(w.toLowerCase() + ':')) || { text: '' }).text.replace(/^\w+:\s*/, '');
    kids.push(h('div', { class: 'ba' },
      h('div', { dir: 'auto' }, h('b', {}, 'Before'), hl(pick('Before'))),
      h('div', { dir: 'auto' }, h('b', {}, 'After'), hl(pick('After')))));
  }
  const reqs = p.sections.find(s => /^requirements$/i.test(s.name));
  if (reqs) {
    kids.push(h('h3', { class: 'sh', dir: 'auto' }, `Requirements, ${reqs.items.length}`));
    for (const r of reqs.items) {
      kids.push(h('div', { class: 'req', dir: 'auto' }, hl(r.text),
        r.subs.map(s => s.key === 'honesty'
          ? h('div', { class: 'h', dir: 'auto' }, h('b', {}, 'Honesty condition.'), ' ', hl(s.text))
          : sub(s))));
    }
  }
  for (const s of p.sections) {
    if (s === ba || s === reqs || (!s.items.length && !s.paras.length)) continue;
    kids.push(h('h3', { class: 'sh', dir: 'auto' }, s.name), s.paras.map(t => h('p', { class: 'para', dir: 'auto' }, hl(t))));
    if (s.items.length) kids.push(h('ul', {}, s.items.map(x => h('li', { class: 'para', dir: 'auto' }, hl(x.text), x.subs.map(sub)))));
  }
  kids.push(versionNote(versions));
  return kids;
}
function versionNote(versions) {
  const older = Math.max(0, (versions || []).length - 1);
  const are = older === 1 ? 'version is' : 'versions are';
  return note('Repo documents are versioned by commit. When the document is amended, this page shows the new version and keeps the old one in history.' +
    (older ? ` ${older} older ${are} in history now.` : ''));
}
const CLAIM_GROUPS = { announcement: 'Announcement', decision: 'Decisions', requirement: 'Requirements', boundary: 'Boundaries', non_goal: 'Non-goals', assumption: 'Assumptions', audience: 'Audience', before_state: 'Before', after_state: 'After', why_it_matters: 'Why it matters', success_signal: 'Success signals' };
const arr = v => Array.isArray(v) ? v.filter(x => x && typeof x === 'object') : [];
function frameBody(f) {
  const c = arr(f.claims), ok = c.filter(x => x.status === 'confirmed').length, pr = c.length - ok;
  const qs = c.flatMap(x => arr(x.hard_questions)), qres = qs.filter(q => q.resolved || q.resolution).length;
  const scope = arr(f.scope_entries), vague = arr(f.open_vagueness).filter(v => !v.resolved);
  const kinds = [...Object.keys(CLAIM_GROUPS), ...new Set(c.map(x => x.kind).filter(k => !(k in CLAIM_GROUPS)))];
  const groups = kinds.filter(k => c.some(x => x.kind === k)).map(k => [
    h('h3', { class: 'sh' }, CLAIM_GROUPS[k] || String(k).replaceAll('_', ' ').replace(/^./, s => s.toUpperCase())),
    c.filter(x => x.kind === k).map(x => h('div', { class: 'claim' },
      h('span', { class: 'id' }, x.id),
      h('div', {},
        h('span', { class: x.status === 'confirmed' ? 'pill ok' : 'pill prop' }, x.status === 'confirmed' ? '✓ Confirmed' : '○ Proposed'),
        h('div', { class: 'claim-text', dir: 'auto' }, hl(x.text)),
        arr(x.hard_questions).map(q => h('div', { class: 'q', dir: 'auto' }, h('b', {}, q.id), ' ', hl(q.text),
          q.resolution ? [h('br'), h('b', {}, 'Decided:'), ' ', hl(q.resolution)] : null))))),
  ]);
  return [
    h('div', { class: 'progress', role: 'img', 'aria-label': `${ok} confirmed, ${pr} proposed` },
      h('span', { style: { flex: String(ok), background: 'var(--accent)' } }), h('span', { style: { flex: String(pr), background: 'var(--warning)' } })),
    h('div', { class: 'frame-sum' }, `${ok} of ${c.length} claims confirmed, ${pr} waiting for you. ${qres} of ${qs.length} questions decided. ${scope.length} surfaces explored.`),
    groups,
    h('details', {}, h('summary', {}, `Scope exploration, ${scope.length} surfaces`),
      scope.map(s => h('div', { class: 'scope', dir: 'auto' }, h('b', { dir: 'auto' }, s.surface), hl(s.finding)))),
    h('details', {}, h('summary', {}, `Open unknowns, ${vague.length}`), vague.map(v => h('div', { class: 'scope', dir: 'auto' }, hl(v.text)))),
    note(`Rendered from the frame's JSON (schema version ${f.schema_version ?? 'unknown'}). The raw file stays downloadable beside this page.`),
  ];
}
function renderDoc(i, art, my) {
  const where = h('span', {}, `${i.path.split('/').slice(0, -1).join('/') || '/'}, ${dayMonth(i.at.slice(0, 10))}`);
  const title = h('h2', { class: 'title', dir: 'auto' }, i.heading || i.title);
  const prose = h('div', { class: 'prose', dir: 'auto' }, loading());
  fill(art, head(i, [h('span', {}, repoShort(i.repo)), where]), title, prose);
  const done = kids => { if (my === token) fill(prose, kids); };
  const fail = e => done(failed(e));
  const setTitle = t => { if (t && my === token) { title.textContent = t; i.heading = t; } };
  const setWhere = status => { if (status && my === token) where.textContent = `${i.path.split('/').slice(0, -1).join('/')}, ${status}`; };
  docBody(i, setTitle, setWhere).then(done).catch(fail);
}
/** A devague frame (claims, no tasks, schema 6 or older) is drawn here from its JSON. */
const isFrame = data => data && typeof data === 'object' && Array.isArray(data.claims) && !Array.isArray(data.tasks) && (data.schema_version ?? 0) <= 6;
/** A JSON repo doc's parsed content, or null (not JSON, or it does not parse). */
async function docJson(i) {
  if (!/\.json$/i.test(i.path)) return null;
  const t = await apiText(`/api/repodocs/${i.id}/content`);
  try {
    return JSON.parse(t);
  } catch (e) {
    return null; // not valid JSON: fall back to the server's rendering below
  }
}
/** The reader body for a repo doc: a frame from its JSON, else the server's rendering. */
async function docBody(i, setTitle, setWhere) {
  const data = await docJson(i);
  if (isFrame(data)) {
    setTitle(data.title || data.slug);
    setWhere(data.status);
    return frameBody(data);
  }
  const [r, meta] = await Promise.all([api(`/api/repodocs/${i.id}/rendered`), api(`/api/repodocs/${i.id}`)]);
  setTitle(r.title);
  if (r.format === 'html') {
    const box = h('div', { class: 'fragment' });
    const fragment = r.html; // escaped by culture_library.render.devague.render
    box.innerHTML = fragment;
    const inner = box.querySelector('header h2'); // the reader's own h2 shows the title
    if (inner) inner.remove();
    return [box, versionNote(meta.versions)];
  }
  const p = parseSpec(r.text);
  if (/\.md$/i.test(i.path) && isSpec(p)) return specBody(p, meta.versions);
  return [h('pre', { class: 'text', dir: 'auto' }, hl(r.text)), versionNote(meta.versions)];
}

/* ---------- entry points ---------- */
export function render(i) {
  const art = $('reader');
  const my = ++token;
  if (i.src === 'doc') renderDoc(i, art, my); else renderItem(i, art, my);
  send.paintSendRow(i.key);
}
/** A repo doc's real title arrived in the background: update the open reader's heading. */
export function refreshHeading(i) {
  if (i?.src !== 'doc' || !i.heading) return;
  const t = $('reader').querySelector('h2.title');
  if (t) t.textContent = i.heading;
}
