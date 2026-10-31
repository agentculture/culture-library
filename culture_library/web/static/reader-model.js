/* Reader decisions as pure functions (no DOM; tests call them under Node): which version of a
 * repo doc to show, how a design is presented, what a spec page says about its PR and code
 * paths, the removed notice, and a safe Markdown block model. Markdown is parsed to data
 * only; reader.js turns it into text nodes, so source markup is never interpreted. */

const short = sha => String(sha || '').slice(0, 7);
const day = iso => String(iso || '').slice(0, 10);

/**
 * Which version the reader opens. ``meta`` is GET /api/repodocs/{id} ({draft, current});
 * ``choice`` is null (default), 'working' or 'current'. ``version`` is the value for the
 * ?version= parameter ('working', a sha, or null for the server default). An ahead draft is
 * shown by default with the badge; ``toggle`` names the one-click switch ('current' or
 * 'working'), or null when there is nothing to switch to.
 */
export function chooseView(meta, choice) {
  const ahead = !!meta?.draft?.ahead;
  const cur = meta?.current?.commit || null;
  const working = ahead && (choice !== 'current' || !cur);
  if (working) return { version: 'working', working: true, toggle: cur ? 'current' : null, badge: true };
  return { version: ahead ? cur : null, working: false, toggle: ahead ? 'working' : null, badge: false };
}

const IMAGE = /\.(png|jpe?g|gif|webp|avif|bmp|ico|svg)$/i;
const HTML = /\.html?$/i;
/** 'image' (an <img>, SVG included), 'html' (a sandboxed iframe) or 'download'. */
export function designMode(path) {
  if (IMAGE.test(path || '')) return 'image';
  if (HTML.test(path || '')) return 'html';
  return 'download';
}
/** The iframe sandbox for an HTML prototype: no scripts, never same-origin. */
export function prototypeSandbox() { return ''; }

/** "computed at <short commit> (<day>)" for a spec_links object that has at_commit. */
function computedAt(sl) {
  const when = sl.at_committed_at ? ` (${day(sl.at_committed_at)})` : '';
  return `computed at ${short(sl.at_commit)}${when}`;
}

/** What a spec page shows from spec_links, or null when it has nothing. */
export function specLinksModel(sl) {
  if (!sl) return null;
  const paths = Array.isArray(sl.code_paths) ? sl.code_paths : [];
  if (!sl.pr && !sl.merge_commit && !paths.length) return null;
  let pr = null;
  if (sl.pr) {
    const href = /^https?:\/\//i.test(sl.pr.url || '') ? sl.pr.url : null;
    const label = sl.pr.number != null ? `#${sl.pr.number}` : (href || '');
    pr = { href, label };
  }
  const computed = sl.at_commit ? computedAt(sl) : null;
  return { pr, merge: sl.merge_commit ? short(sl.merge_commit) : null, paths, computed };
}

/** The notice for a removed doc, or null. */
export function removedNotice(removed) {
  if (!removed?.removed) return null;
  const at = removed.removed_at ? ` (${day(removed.removed_at)})` : '';
  return `Removed at ${short(removed.removed_commit) || 'an unknown commit'}${at}. History stays readable.`;
}

/** Inline tokens: **bold**, `code`, everything else plain text. */
export function inlineTokens(s) {
  const out = [];
  const re = /\*\*([^*]+)\*\*|`([^`]+)`/g;
  let last = 0;
  for (const m of String(s).matchAll(re)) {
    if (m.index > last) out.push({ t: 'text', s: s.slice(last, m.index) });
    out.push(m[1] !== undefined ? { t: 'b', s: m[1] } : { t: 'code', s: m[2] });
    last = m.index + m[0].length;
  }
  if (last < s.length) out.push({ t: 'text', s: s.slice(last) });
  return out;
}

const isFence = (line) => line.startsWith('```');
const BULLET = /^\s*[-*] +(.*)/;
const NUMBERED = /^\s*\d+[.)] +(.*)/;
const QUOTE = /^>\s?(.*)/;
const HEADING = /^(#{1,6}) +([^ ].*)$/;

/** A heading line's {level, text}, or null. Trailing #s are dropped, keeping at least one char. */
function headingOf(line) {
  const m = HEADING.exec(line);
  if (!m) return null;
  let end = m[2].length;
  while (end > 1 && m[2][end - 1] === '#') end--;
  return { level: m[1].length, text: m[2].slice(0, end).trim() };
}

/** Push the pending paragraph and quote as blocks and reset the state. */
function flushMd(st) {
  if (st.para.length) st.blocks.push({ type: 'p', inline: inlineTokens(st.para.join(' ')) });
  if (st.quote) st.blocks.push({ type: 'quote', inline: inlineTokens(st.quote.join(' ')) });
  st.para = []; st.list = null; st.quote = null;
}

/** A fenced code block opening at ``start``: pushes it, returns the index of its closing fence. */
function fencedCode(st, lines, start) {
  flushMd(st);
  const code = [];
  let n = start + 1;
  while (n < lines.length && !isFence(lines[n])) { code.push(lines[n].trimEnd()); n++; }
  st.blocks.push({ type: 'code', text: code.join('\n') });
  return n;
}

function listItem(st, type, text) {
  if (!st.list || st.list.type !== type) { flushMd(st); st.list = { type, items: [] }; st.blocks.push(st.list); }
  st.list.items.push(inlineTokens(text));
}

function quoteLine(st, text) {
  if (st.para.length || st.list) flushMd(st);
  st.quote ??= [];
  st.quote.push(text);
}

/** Any line that is not a fence, heading, list item or quote: blank, list continuation, text. */
function plainLine(st, line) {
  if (!line.trim()) flushMd(st);
  else if (st.list && /^\s+\S/.test(line)) st.list.items.at(-1).push(...inlineTokens(' ' + line.trim()));
  else {
    if (st.list || st.quote) flushMd(st);
    st.para.push(line.trim());
  }
}

function mdLine(st, line) {
  const hd = headingOf(line);
  const li = BULLET.exec(line) || NUMBERED.exec(line);
  const q = QUOTE.exec(line);
  if (hd) {
    flushMd(st);
    st.blocks.push({ type: 'h', level: hd.level, inline: inlineTokens(hd.text) });
  } else if (li) listItem(st, BULLET.test(line) ? 'ul' : 'ol', li[1]);
  else if (q) quoteLine(st, q[1]);
  else plainLine(st, line);
}

/** Markdown to blocks: h, p, ul, ol, quote, code. Plain data; no HTML is ever produced. */
export function markdownBlocks(md) {
  const st = { blocks: [], para: [], list: null, quote: null };
  const lines = String(md).split('\n');
  let n = 0;
  while (n < lines.length) {
    const line = lines[n].trimEnd();
    if (isFence(line)) {
      n = fencedCode(st, lines, n) + 1; // resume after the closing fence
    } else {
      mdLine(st, line);
      n += 1;
    }
  }
  flushMd(st);
  return st.blocks;
}
