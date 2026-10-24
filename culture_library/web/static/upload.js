/* Upload dialog (prototype "upload"): drag-and-drop or a file picker, then a multipart
 * POST /api/library with file, title, kind and visibility. The server extracts text;
 * nothing is parsed in the browser. Errors name the problem and how to fix it.
 */
import { $, KINDS, api, fill, fmtSize, h, kindKey, openItem, reloadEntries } from './app.js';
import * as home from './home.js';
import { toast } from './send.js';

const LIMIT = 50 * 1024 * 1024;
const FORMATS = /\.(epub|pdf|md|markdown|txt|json)$/i;
let dlg, fileIn, drop, pending = null;

function setDrop(...kids) { fill(drop, ...kids, fileIn); }
const idle = () => setDrop(h('b', {}, 'Drop a file here or choose one'), h('br'), 'EPUB, PDF, Markdown, text or JSON, up to 50 MB');
function curl() {
  const f = pending ? pending.name : '<file>';
  const k = $('up-kind').value, v = $('up-vis').value;
  $('up-curl').textContent = `curl -H "Authorization: Bearer $LIBRARY_KEY" \\\n  -F file=@${f} -F kind=${k} -F visibility=${v} \\\n  ${location.origin}/api/library`;
}
function showErr(text) { const err = $('up-err'); err.textContent = text; err.hidden = false; }
function pick(f) {
  $('up-err').hidden = true;
  if (!FORMATS.test(f.name)) { showErr(`${f.name} is not a supported format. Choose an EPUB, PDF, Markdown, text or JSON file.`); return; }
  if (f.size > LIMIT) { showErr(`${f.name} is ${fmtSize(f.size)}. The limit is 50 MB.`); return; }
  pending = f;
  setDrop(h('b', { dir: 'auto' }, f.name), h('br'), `${fmtSize(f.size)}. Choose another file to replace it.`);
  $('up-name').value = f.name.replace(/\.[^.]+$/, '').replace(/[-_]+/g, ' ');
  $('up-kind').value = /\.epub$/i.test(f.name) ? 'book' : 'document';
  $('up-go').disabled = false; curl();
}
/** Plain words for an upload refusal (409/413/415 and friends). */
export function uploadError(e, name, kind) {
  switch (e.status) {
    case 409: return `${name} is already in the library as an item you cannot see. Choose another file, or ask whoever uploaded it to share it.`;
    case 413: return `${name} is too large. The limit is 50 MB; choose a smaller file.`;
    case 415: return `${name} is not a supported format. Choose an EPUB, PDF, Markdown, text or JSON file.`;
    case 403: return `Your key cannot add ${KINDS[kindKey(kind)].plural.toLowerCase()}. Use a key with write access to that kind.`;
    case 401: return 'You are not signed in. Sign in, then try again.';
    case 400: return `Could not read ${name}: ${e.detail}. Check that the file opens elsewhere, then try again.`;
    case 0: return 'Could not reach the library. Check your connection, then try again.';
    default: return `The library refused ${name}: ${e.detail}. Try again in a moment.`;
  }
}
async function submit(e) {
  e.preventDefault(); if (!pending) return;
  const go = $('up-go'); go.disabled = true; go.textContent = 'Adding…';
  const f = pending, kind = $('up-kind').value;
  const form = new FormData();
  form.append('file', f, f.name);
  form.append('title', $('up-name').value.trim() || f.name);
  form.append('kind', kind);
  form.append('visibility', $('up-vis').value);
  let r;
  try {
    r = await api('/api/library', { method: 'POST', body: form });
  } catch (err) {
    showErr(uploadError(err, f.name, kind));
    go.disabled = false; go.textContent = 'Add to library';
    return;
  }
  go.textContent = 'Add to library'; dlg.close();
  try { await reloadEntries(); } catch (err) { /* the item is stored; the list refreshes on reload */ }
  home.drawShelf();
  openItem('item-' + r.item.id);
  if (!r.created) toast('Already in the library.');
}
export function init() {
  dlg = $('upload'); fileIn = $('up-file'); drop = $('drop');
  $('upload-open').addEventListener('click', () => {
    pending = null; $('up-form').reset(); idle();
    $('up-go').disabled = true; $('up-err').hidden = true; curl(); dlg.showModal();
  });
  $('up-cancel').addEventListener('click', () => dlg.close());
  fileIn.addEventListener('change', () => fileIn.files[0] && pick(fileIn.files[0]));
  ['up-kind', 'up-vis'].forEach(id => $(id).addEventListener('change', curl));
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files[0]) pick(e.dataTransfer.files[0]); });
  $('up-form').addEventListener('submit', submit);
}
