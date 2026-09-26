// Поиск по библиотеке: названия книг и папок, главы и текст глав.

import { get } from '../api.js';
import { cover } from '../covers.js';
import { el, guard, icon, marked, plain } from '../ui.js';

const DELAY_MS = 250;
const GROUPS = [
  ['all', 'Всё'],
  ['book', 'Книги'],
  ['chapter', 'Главы'],
  ['folder', 'Папки'],
];

export function render(view, initial) {
  let alive = true;
  let results = [];
  let group = 'all';
  let timer = null;

  const input = el('input', { type: 'search', value: initial, placeholder: 'Название, автор или фраза из текста', 'aria-label': 'Поиск' });
  const clear = el('button', { class: 'round ghost', 'aria-label': 'Очистить', title: 'Очистить' }, icon('x'));
  // Пустое поле — крестик закрывает поиск и возвращает туда, откуда пришли.
  const drawClear = () => {
    const empty = !input.value;
    clear.title = empty ? 'Закрыть поиск' : 'Очистить';
    clear.setAttribute('aria-label', clear.title);
  };
  const chips = el('div', { class: 'lib-filters' });
  const list = el('div', { class: 'hits' });
  const page = el('div', { class: 'page' },
    el('label', { class: 'search-big' }, icon('search', { className: 'big' }), input, clear),
    chips, list);
  view.replaceChildren(page);

  function href(result) {
    if (result.kind === 'folder') return `#/library?folder=${result.id}`;
    if (result.kind === 'book') return `#/book/${result.id}`;
    return `#/chapter/${result.id}`;
  }

  function hit(result) {
    const bookLike = result.kind === 'chapter'
      ? { id: result.book_id, title: result.book_title, cover_path: result.cover_path }
      : { id: result.id, title: plain(result.title), author: plain(result.author || ''), cover_path: result.cover_path };
    const meta = result.kind === 'chapter'
      ? `${result.book_title} · глава`
      : result.kind === 'book' ? (plain(result.author || '') || 'книга') : 'папка';
    return el('a', { class: 'hit', href: href(result) },
      el('span', { class: 'mini' }, result.kind === 'folder'
        ? el('span', { class: 'round button', 'aria-hidden': 'true' }, icon('folder'))
        : cover(bookLike, { text: false })),
      el('span', { class: 'text' },
        el('span', { class: 'title' }, marked(result.title)),
        result.snippet ? el('span', { class: 'snippet' }, marked(result.snippet)) : null,
        el('span', { class: 'meta' }, meta)),
      icon('chevron-right'));
  }

  function draw() {
    const counts = Object.fromEntries(GROUPS.map(([key]) => [key, key === 'all'
      ? results.length : results.filter((r) => r.kind === key).length]));
    chips.replaceChildren(...GROUPS.filter(([key]) => key === 'all' || counts[key]).map(([key, label]) => {
      const chip = el('button', { class: 'chip-filter', 'aria-pressed': String(group === key) },
        label, el('span', { class: 'count' }, String(counts[key])));
      chip.onclick = () => { group = key; draw(); };
      return chip;
    }));
    const query = input.value.trim();
    const shown = group === 'all' ? results : results.filter((r) => r.kind === group);
    if (!query) {
      list.replaceChildren(el('p', { class: 'hint' }, 'Ищет по названиям книг, папок и глав и по всему тексту книг. Ctrl+K — сюда из любого места.'));
    } else if (!shown.length) {
      list.replaceChildren(el('div', { class: 'empty-state' },
        el('div', { class: 'big' }, icon('search', { className: 'huge' })),
        el('p', {}, `По запросу «${query}» ничего не нашлось.`)));
    } else {
      list.replaceChildren(...shown.map(hit));
    }
  }

  const search = guard(async () => {
    const query = input.value.trim();
    // Адрес меняем без новой записи в истории: «Назад» не должен листать буквы.
    history.replaceState(history.state, '', query ? `#/search?q=${encodeURIComponent(query)}` : '#/search');
    if (!query) { results = []; draw(); return; }
    const data = await get(`/api/search?q=${encodeURIComponent(query)}`);
    if (!alive || input.value.trim() !== query) return;
    results = data.results;
    draw();
  });

  input.oninput = () => { drawClear(); clearTimeout(timer); timer = setTimeout(search, DELAY_MS); };
  clear.onclick = (event) => {
    event.preventDefault();
    if (!input.value) {
      if (history.state?.navIndex > 0) history.back();
      else location.hash = '#/';
      return;
    }
    input.value = '';
    drawClear();
    input.focus();
    search();
  };
  input.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !input.value) clear.click(); });
  drawClear();
  const focus = () => { input.focus(); input.select(); };
  window.addEventListener('focus-search', focus);
  setTimeout(focus, 0);
  search();

  return () => {
    alive = false;
    clearTimeout(timer);
    window.removeEventListener('focus-search', focus);
  };
}
