/* Search: the top bar's full-text box (prototype "q" input handler).
 *
 * Titles (items and repo documents) are matched here; item full text is matched by
 * GET /api/search, whose snippets mark matches with [ ]. The first full-text hit opens,
 * matches are highlighted in the reader, and snippets appear under matching rows.
 */
import { $, S, api, h, openItem, setPane, show, sorted } from './app.js';
import * as reader from './reader.js';

let timer, seq = 0;
const snippets = new Map();

/** The query's words, for highlighting (whole words of 2+ characters, or the raw query). */
export function terms() {
  const q = S.query.trim();
  if (!q) return [];
  const words = q.split(/\s+/).filter(w => w.length > 1);
  return words.length ? words : [q];
}
export function visibleList() {
  const q = S.query.trim().toLowerCase();
  return sorted().filter(i => (S.paneFilter === 'all' || i.kind === S.paneFilter) &&
    (!q || i.title.toLowerCase().includes(q) || snippets.has(i.key)));
}
/** A hit's snippet as nodes: "[" and "]" from the API become <mark>. */
export function snippetFor(key) {
  const s = snippets.get(key);
  if (!s) return null;
  // A linear scan (same pairs as /\[([^\]]*)\]/g): each "[" up to the next "]".
  const out = [];
  let last = 0;
  for (;;) {
    const open = s.indexOf('[', last);
    const close = open < 0 ? -1 : s.indexOf(']', open + 1);
    if (close < 0) break;
    out.push(s.slice(last, open), h('mark', {}, s.slice(open + 1, close)));
    last = close + 1;
  }
  out.push(s.slice(last));
  return out;
}
async function run() {
  const my = ++seq, q = S.query.trim();
  let hits = [];
  try {
    hits = (await api(`/api/search?q=${encodeURIComponent(q)}&limit=100`)).hits;
  } catch (e) {
    hits = []; // titles still match below; full text is unavailable for now
  }
  if (my !== seq || q !== S.query.trim()) return;
  snippets.clear();
  for (const hit of hits) snippets.set('item-' + hit.item_id, hit.snippet || '');
  const list = visibleList();
  const target = list.find(i => i.key === S.current && snippets.has(i.key)) || list.find(i => snippets.has(i.key)) || list[0];
  if (target) openItem(target.key, target.key === S.current); else { show('read'); reader.drawRows(); }
  setPane(true);
}
export function init() {
  $('q').addEventListener('input', e => {
    S.query = e.target.value;
    clearTimeout(timer);
    if (!S.query.trim()) {
      seq++;
      snippets.clear();
      if (S.current) openItem(S.current, true);
      return;
    }
    timer = setTimeout(run, 180);
  });
}
