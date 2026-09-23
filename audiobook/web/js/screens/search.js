// Результаты поиска по библиотеке.

import { get } from '../api.js';
import { el, emit, guard, icon, marked } from '../ui.js';

const KIND = { folder: 'Папка', book: 'Книга', chapter: 'Глава' };

export function render(view, query) {
  let alive = true;
  const page = el('div', { class: 'page' }, el('h1', {}, `Поиск: «${query}»`), el('p', { class: 'hint' }, 'Ищу…'));
  view.replaceChildren(page);

  guard(async () => {
    const { results } = await get(`/api/search?q=${encodeURIComponent(query)}`);
    if (!alive) return;
    if (!results.length) {
      page.replaceChildren(el('h1', {}, `Поиск: «${query}»`),
        el('div', { class: 'empty-state' },
          el('div', { class: 'big' }, icon('search', { className: 'huge' })),
          el('p', {}, 'Ничего не нашлось.')));
      return;
    }
    const open = (result) => {
      if (result.kind === 'folder') emit('reveal-folder', result.id);
      else if (result.kind === 'book') location.hash = `#/book/${result.id}`;
      else location.hash = `#/chapter/${result.id}/edit`;
    };
    page.replaceChildren(
      el('h1', {}, `Поиск: «${query}»`),
      el('p', { class: 'hint' }, `Найдено: ${results.length}`),
      el('div', { class: 'list' }, results.map((result) => el('div', {
        class: 'result', tabindex: '0',
        onclick: () => open(result),
        onkeydown: (event) => { if (event.key === 'Enter') open(result); },
      },
        el('div', { class: 'kind' }, KIND[result.kind] || result.kind,
          result.kind === 'chapter' ? ` · ${result.book_title}` : ''),
        el('div', {}, marked(result.title)),
        result.author ? el('div', { class: 'muted' }, marked(result.author)) : null,
        result.snippet ? el('div', { class: 'snippet' }, marked(result.snippet)) : null))),
    );
  })();

  return () => { alive = false; };
}
