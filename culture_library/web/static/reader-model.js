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
  const computed = sl.at_commit ? `computed at ${short(sl.at_commit)}${sl.at_committed_at ? ` (${day(sl.at_committed_at)})` : ''}` : null;
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

/** Markdown to blocks: h, p, ul, ol, quote, code. Plain data; no HTML is ever produced. */
export function markdownBlocks(md) {
  const blocks = [];
  const lines = String(md).split('\n');
  let para = [], list = null, quote = null;
  const flush = () => {
    if (para.length) blocks.push({ type: 'p', inline: inlineTokens(para.join(' ')) });
    if (quote) blocks.push({ type: 'quote', inline: inlineTokens(quote.join(' ')) });
    para = []; list = null; quote = null;
  };
  for (let n = 0; n < lines.length; n++) {
    const line = lines[n].trimEnd();
    if (/^```/.test(line)) {
      flush();
      const code = [];
      for (n++; n < lines.length && !/^```/.test(lines[n]); n++) code.push(lines[n].trimEnd());
      blocks.push({ type: 'code', text: code.join('\n') });
      continue;
    }
    const hd = /^(#{1,6}) +(.+?)#*$/.exec(line);
    const ul = /^\s*[-*] +(.*)/.exec(line), ol = /^\s*\d+[.)] +(.*)/.exec(line);
    const q = /^>\s?(.*)/.exec(line);
    if (hd) { flush(); blocks.push({ type: 'h', level: hd[1].length, inline: inlineTokens(hd[2].trim()) }); }
    else if (ul || ol) {
      const type = ul ? 'ul' : 'ol';
      if (!list || list.type !== type) { flush(); list = { type, items: [] }; blocks.push(list); }
      list.items.push(inlineTokens((ul || ol)[1]));
    } else if (q) {
      if (para.length || list) flush();
      (quote ||= []).push(q[1]);
    } else if (!line.trim()) flush();
    else if (list && /^\s+\S/.test(line)) list.items[list.items.length - 1].push(...inlineTokens(' ' + line.trim()));
    else { if (list || quote) flush(); para.push(line.trim()); }
  }
  flush();
  return blocks;
}
