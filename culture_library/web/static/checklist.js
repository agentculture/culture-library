/**
 * To-do checklists (#11): culture-rules writes one Markdown checklist per recording. The
 * reader shows `- [ ]` / `- [x]` lines as read-only checkboxes, `#` lines as headings and
 * any other line as a note. `<!-- ... -->` comments (culture-rules' todo ids) are hidden.
 * Pure: no DOM, no imports, so it runs under Node in the tests.
 */
const CHECK = /^\s*[-*+]\s+\[([ xX])\]\s+(.*)$/;
const HEADING = /^\s*#{1,6}\s+(.*)$/;

export function parseChecklist(text) {
  const out = [];
  const visible = String(text).replace(/<!--[\s\S]*?-->/g, '');
  for (const raw of visible.split('\n')) {
    const line = raw.trimEnd();
    if (!line.trim()) continue;
    const check = CHECK.exec(line);
    const heading = check ? null : HEADING.exec(line);
    if (check) out.push({ type: 'check', done: check[1] !== ' ', text: check[2].trim() });
    else if (heading) out.push({ type: 'heading', text: heading[1].trim() });
    else out.push({ type: 'note', text: line.trim() });
  }
  return out;
}
