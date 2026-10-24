/* Home: the intro shelf, Continue reading, and the Latest card grid (prototype "home"). */
import { $, KINDS, S, byKey, chip, fetchExcerpts, fill, fmtN, fmtSize, h, kc, repoName, sorted, store } from './app.js';

const PAGE = 60;
let limit = PAGE;

export function filterButtons(active, attr) {
  const kinds = ['all', ...Object.keys(KINDS).filter(k => S.entries.some(i => i.kind === k))];
  return kinds.map(k => {
    const n = k === 'all' ? S.entries.length : S.entries.filter(i => i.kind === k).length;
    return h('button', { [attr]: k, 'aria-pressed': String(active === k) }, `${k === 'all' ? 'All' : KINDS[k].plural} ${n}`);
  });
}
function cardTags(i) {
  return h('span', { class: 'tags' }, chip(i.kind),
    i.live ? null : h('span', { class: 'chip soon' }, 'Arriving'),
    i.vis === 'private' ? h('span', { class: 'chip soon' }, 'Only me') : null);
}
function cardMeta(i) {
  const when = new Date(i.at.replace(' ', 'T') + ':00').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
  if (i.src === 'doc') return `${when}, from ${repoName(i.repo)}`;
  return `${when}, ${(i.format || '').toUpperCase()} ${i.size ? fmtSize(i.size) : ''}`;
}
function cardText(i) {
  if (i.excerpt) return i.excerpt;
  if (i.excerpt === undefined) return '';
  if (i.kind === 'newspaper') return 'Daily newspaper from culture-rules. Its full text opens in the reader.';
  if (i.format === 'pdf') return 'A PDF. Its text opens in the reader once the server has extracted it.';
  return 'Opens in the reader.';
}
export function card(i, big) {
  const prog = Number(store.get('pos.' + i.key) || 0);
  return h('button', { class: big ? 'card big' : 'card', dir: 'auto', 'data-open': i.key },
    cardTags(i),
    h('h3', { class: 'ct', dir: 'auto' }, i.title),
    h('p', { class: 'cx', dir: 'auto' }, cardText(i)),
    big && prog ? h('span', { class: 'bar', role: 'img', 'aria-label': `${prog}% read` }, h('span', { style: { width: prog + '%' } })) : null,
    h('span', { class: 'meta' }, cardMeta(i)));
}
function moreBox() {
  let box = $('cards-more');
  if (!box) {
    box = h('div', { class: 'more', id: 'cards-more' });
    $('cards').after(box);
    box.addEventListener('click', e => { if (e.target.closest('button')) { limit += PAGE; drawHome(); } });
  }
  return box;
}
export function drawHome() {
  if (!S.loaded) { fill($('continue')); fill($('cards')); return; }
  const last = byKey(store.get('last'));
  const recent = sorted();
  const lead = last || recent[0];
  $('cont-title').textContent = last ? 'Continue reading' : 'Start with the newest';
  fill($('home-filters'), filterButtons(S.homeFilter, 'data-hfilter'));
  if (!lead) {
    fill($('continue'), h('p', { class: 'empty' }, 'Nothing on the shelf yet. Upload a file, or let culture-rules push the first item.'));
    fill($('cards'));
    moreBox().replaceChildren();
    return;
  }
  const others = recent.filter(i => i !== lead).slice(0, 2);
  fill($('continue'), card(lead, true), others.map(i => card(i)));
  const shown = recent.filter(i => S.homeFilter === 'all' || i.kind === S.homeFilter);
  fill($('cards'), shown.slice(0, limit).map(i => card(i)));
  const more = shown.length - limit;
  fill(moreBox(), more > 0 ? h('button', { class: 'btn' }, `Show ${fmtN(Math.min(more, PAGE))} more`) : null);
  fetchExcerpts([lead, ...others, ...shown.slice(0, limit)])
    .then(got => { if (got && !$('view-home').hidden) drawHome(); })
    .catch(() => { /* excerpts are optional; cards keep their placeholder text */ });
}
export function drawShelf() {
  const asc = (a, b) => a.at.localeCompare(b.at) || a.id - b.id;
  const spine = i => {
    const ht = i.size ? Math.round(52 + Math.min(1, Math.log10(Math.max(i.size, 10)) / 7) * 56) : 46 + ((i.id * 37) % 50);
    const what = i.src === 'doc' ? `from ${repoName(i.repo)}` : fmtSize(i.size);
    return h('button', { class: 'spine', style: { height: ht + 'px', background: kc(i.kind) }, 'data-open': i.key, 'aria-label': i.title, 'data-tip': `${i.title}\n${KINDS[i.kind].label}, ${what}` });
  };
  const items = S.entries.filter(i => i.src === 'item').sort(asc), docs = S.entries.filter(i => i.src === 'doc').sort(asc);
  fill($('shelf'), items.map(spine), items.length && docs.length ? h('span', { class: 'gap' }) : null, docs.map(spine));
  fill($('legend'), Object.keys(KINDS).map(k => h('span', {}, h('i', { class: 'sw', style: { background: kc(k) } }), KINDS[k].plural)));
  $('shelf-count').textContent = S.loaded ? `${fmtN(S.entries.length)} on the shelf` : 'Loading the shelf…';
}
export function drawError(e) {
  fill($('continue'), h('p', { class: 'empty' }, `The library did not answer (${e.detail || e.message}). Reload the page to try again.`));
}
export function init() { /* home has no listeners of its own: app.js delegates clicks */ }
