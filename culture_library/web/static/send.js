/* Send to device (prototype "send to device").
 *
 * Targets come from GET /api/send/targets; choosing one POSTs /api/library/{id}/send,
 * which publishes library.item.send_requested (202). The page then shows the toast and
 * "Sent to <target> at <time>. culture-rules takes it from here." and nothing about
 * delivery, which happens outside this repo. A publish failure (503 publish_unavailable)
 * shows an error with a working Retry.
 */
import { $, ApiError, S, SVG, api, byKey, fill, h, icon } from './app.js';

const sendBtn = $('send-btn'), sendMenu = $('send-menu');
let targets = null, busy = false;

const canSend = i => Boolean(i && i.src === 'item' && i.live);
/* The prototype's "<type>. Enabled" / "<type>. Not enabled yet"; type is optional free text. */
const targetNote = t => {
  const status = t.enabled ? 'Enabled' : 'Not enabled yet';
  return t.type ? `${t.type}. ${status}` : status;
};
export function paintSendBtn(i) {
  sendBtn.disabled = !canSend(i);
  sendBtn.title = canSend(i) ? '' : 'Available once this item is in the library';
}
async function loadTargets() {
  try { targets = (await api('/api/send/targets')).targets; } catch (e) { targets = null; throw e; }
  return targets;
}
async function openSendMenu() {
  fill(sendMenu, h('div', { class: 'mfoot' }, 'Loading send targets…'));
  sendMenu.hidden = false; sendBtn.setAttribute('aria-expanded', 'true');
  let list;
  try { list = targets || await loadTargets(); } catch (e) {
    fill(sendMenu, h('div', { class: 'mfoot' }, `Could not load send targets (${e.detail || e.message}). Close this menu and try again.`));
    return;
  }
  if (sendMenu.hidden) return;
  fill(sendMenu,
    list.map(t => h('button', { role: 'menuitem', 'data-send': t.label, disabled: !t.enabled },
      icon(SVG.dev), h('span', { class: 'mt' }, t.label), h('span', { class: 'ms' }, targetNote(t)))),
    list.length ? null : h('div', { class: 'ms' }, 'No send targets are configured for this library.'),
    h('div', { class: 'mfoot' }, 'Sending publishes an event. culture-rules picks it up and delivers it. Agents use the MCP tool send_to_device.'));
  const first = sendMenu.querySelector('button:not(:disabled)'); if (first) first.focus();
}
function closeSendMenu() { sendMenu.hidden = true; sendBtn.setAttribute('aria-expanded', 'false'); }
let toastT;
export function toast(msg) {
  const el = $('toast'); el.textContent = msg; el.hidden = false;
  clearTimeout(toastT); toastT = setTimeout(() => { el.hidden = true; }, 3600);
}
function failure(e, target) {
  if (e.code === 'publish_unavailable' || e.status === 503) return { text: `Could not send to ${target}: the event broker is unavailable.`, retry: true };
  if (e.status === 0) return { text: `Could not send to ${target}: the library did not answer.`, retry: true };
  if (e.code === 'target_disabled') return { text: `${target} is not enabled yet. Choose another target.`, retry: false };
  if (e.code === 'unknown_target') return { text: `${target} is no longer a send target. Reload the page to see the current list.`, retry: false };
  if (e.status === 404) return { text: 'This item is no longer in the library.', retry: false };
  if (e.status === 403) return { text: 'Your key cannot send items. Use a key with the write scope.', retry: false };
  return { text: `Could not send to ${target}: ${e.detail || e.message}.`, retry: true };
}
/** The line under the reader head: sent, or a failure with Retry. */
export function paintSendRow(key) {
  const art = $('reader');
  let row = art.querySelector('.sendrow');
  const s = S.sends[key];
  if (!s) { if (row) row.remove(); return; }
  if (!row) {
    const head = art.querySelector('.reader-head'); if (!head) return;
    row = h('div', { class: 'sendrow' });
    head.after(row);
  }
  if (s.error) {
    fill(row, h('div', { class: 'senderr', role: 'alert' }, icon(SVG.critical), h('span', {}, s.error),
      s.retry ? h('button', { class: 'btn', type: 'button', 'data-send-retry': s.target }, 'Retry') : null));
  } else {
    fill(row, h('span', { class: 'sendline' }, icon(SVG.dev), `Sent to ${s.target} at ${s.at}. culture-rules takes it from here.`));
  }
}
async function doSend(key, target) {
  const i = byKey(key);
  if (!canSend(i) || busy) return;
  busy = true;
  try {
    await api(`/api/library/${i.id}/send`, { method: 'POST', json: { target } });
    S.sends[key] = { target, at: new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) };
    if (S.current === key) paintSendRow(key);
    toast(`Sent to ${target}.`);
  } catch (e) {
    const f = failure(e instanceof ApiError ? e : new ApiError(0, 'network', String(e)), target);
    S.sends[key] = { target, error: f.text, retry: f.retry };
    if (S.current === key) paintSendRow(key);
  } finally {
    busy = false;
  }
}
export function init() {
  sendBtn.addEventListener('click', e => { e.stopPropagation(); if (sendMenu.hidden) openSendMenu(); else closeSendMenu(); });
  sendMenu.addEventListener('click', e => {
    const b = e.target.closest('[data-send]'); if (!b || b.disabled) return;
    const key = S.current;
    closeSendMenu();
    doSend(key, b.dataset.send);
  });
  $('reader').addEventListener('click', e => {
    const b = e.target.closest('[data-send-retry]'); if (!b) return;
    b.disabled = true;
    doSend(S.current, b.dataset.sendRetry);
  });
  document.addEventListener('click', e => { if (!sendMenu.hidden && !e.target.closest('.sendwrap')) closeSendMenu(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !sendMenu.hidden) { closeSendMenu(); sendBtn.focus(); } });
}
