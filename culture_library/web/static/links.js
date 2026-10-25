/**
 * Source links in EPUB sections (#18). GET /api/library/{id}/text gives each section a
 * `links` list parallel to its plain-text `paragraphs`: `links[i]` is
 * `[{text, href, start}]` for paragraph i, where
 * `paragraphs[i].slice(start, start + text.length) === text`. The server keeps
 * only absolute http(s) hrefs; this module checks again before it builds an <a>, sets
 * `href` as a property and the text with `textContent`, never markup.
 * No imports, and the document is a parameter, so it runs under Node in the tests.
 */
const SAFE_HREF = /^https?:\/\//i;

/** `href` when it is an absolute http(s) URL, else null. */
export function safeHref(href) {
  return typeof href === 'string' && SAFE_HREF.test(href) ? href : null;
}

/** `text` split into `{text}` and `{text, href}` segments. A link is placed at its
 * `start` offset when that is valid (an integer at or after the previous link's end where
 * `text` really sits); otherwise at the first occurrence of its text after the previous
 * link. A link that is unsafe, empty or not found is skipped, so its words stay plain. */
export function linkSegments(text, links) {
  const s = String(text ?? '');
  const out = [];
  let at = 0;
  for (const l of Array.isArray(links) ? links : []) {
    const t = l && typeof l.text === 'string' ? l.text : '';
    const href = safeHref(l?.href);
    if (!t || !href) continue;
    const st = l.start;
    const i = Number.isInteger(st) && st >= at && s.startsWith(t, st) ? st : s.indexOf(t, at);
    if (i < 0) continue;
    if (i > at) out.push({ text: s.slice(at, i) });
    out.push({ text: t, href });
    at = i + t.length;
  }
  if (at < s.length || !out.length) out.push({ text: s.slice(at) });
  return out;
}

/** A paragraph as nodes: plain segments through `hl` (search highlighting), links as
 * <a target="_blank" rel="noopener noreferrer"> built with DOM APIs. Link text is not
 * highlighted. */
export function linkedNodes(text, links, hl, doc = globalThis.document) {
  return linkSegments(text, links).flatMap(seg => {
    if (!seg.href || !SAFE_HREF.test(seg.href)) return hl(seg.text);
    const a = doc.createElement('a');
    a.href = seg.href;
    a.rel = 'noopener noreferrer';
    a.target = '_blank';
    a.textContent = seg.text;
    return [a];
  });
}
