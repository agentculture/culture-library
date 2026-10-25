/**
 * To-do checklists (#11): culture-rules writes one Markdown checklist per recording. The
 * reader shows `- [ ]` / `- [x]` lines as read-only checkboxes, `#` lines as headings and
 * any other line as a note. `<!-- ... -->` comments (culture-rules' todo ids) are hidden.
 * Pure: no DOM, no imports, so it runs under Node in the tests.
 */
// Prefixes only (no trailing `.*`), matched against a trimmed line: linear, no backtracking.
const CHECK = /^[-*+] \[([ xX])\](?: |$)/;
const HEADING = /^#{1,6} /;

export function parseChecklist(text) {
  const out = [];
  const visible = String(text).replace(/<!--[\s\S]*?-->/g, '');
  for (const raw of visible.split('\n')) {
    const line = raw.trim().replaceAll('\t', ' ');
    if (!line) continue;
    const check = CHECK.exec(line);
    const heading = check ? null : HEADING.exec(line);
    if (check) out.push({ type: 'check', done: check[1] !== ' ', text: line.slice(check[0].length).trim() });
    else if (heading) out.push({ type: 'heading', text: line.slice(heading[0].length).trim() });
    else out.push({ type: 'note', text: line });
  }
  return out;
}
