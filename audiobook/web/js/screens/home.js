// Главная: что лежит в библиотеке.

import { get } from '../api.js';
import { el, emit, formatHours, guard, icon, plural } from '../ui.js';

function flatten(tree) {
  const books = tree.books.map((book) => ({ ...book, path: '' }));
  const walk = (folders, trail) => {
    for (const folder of folders) {
      const here = [...trail, folder.name];
      folder.books.forEach((book) => books.push({ ...book, path: here.join(' / ') }));
      walk(folder.folders, here);
    }
  };
  walk(tree.folders, []);
  return books;
}

function card(book) {
  return el('a', { class: 'card', href: `#/book/${book.id}` },
    book.cover_path
      ? el('img', { class: 'cover', src: `/api/books/${book.id}/cover`, alt: '', loading: 'lazy' })
      : el('div', { class: 'cover' }, icon('book-open', { className: 'huge' })),
    el('div', { class: 'card-title' }, book.title),
    el('div', { class: 'card-sub' }, book.author || book.path || plural(book.chapters, 'глава', 'главы', 'глав')));
}

export function render(view) {
  let alive = true;
  const page = el('div', { class: 'page' });
  view.replaceChildren(page);

  const load = guard(async () => {
    const tree = await get('/api/library');
    if (!alive) return;
    const books = flatten(tree);

    if (!books.length) {
      page.replaceChildren(el('div', { class: 'empty-state' },
        el('div', { class: 'big' }, icon('library', { className: 'huge' })),
        el('h1', {}, 'Библиотека пуста'),
        el('p', {}, 'Добавьте книгу в формате txt, epub, fb2, docx или pdf — или просто перетащите файлы в окно.'),
        el('button', { class: 'primary', onclick: () => emit('import-request', null) }, 'Добавить книгу')));
      return;
    }

    const chapters = books.reduce((n, book) => n + book.chapters, 0);
    const duration = books.reduce((n, book) => n + (book.duration_ms || 0), 0);
    const recent = [...books].sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));

    page.replaceChildren(
      el('h1', {}, 'Библиотека'),
      el('div', { class: 'stats' },
        el('span', {}, el('b', {}, plural(books.length, 'книга', 'книги', 'книг'))),
        el('span', {}, el('b', {}, plural(chapters, 'глава', 'главы', 'глав'))),
        el('span', {}, 'озвучено ', el('b', {}, formatHours(duration)))),
      el('h2', {}, 'Недавно добавленные'),
      el('div', { class: 'cards' }, recent.map(card)),
    );
  });

  const onChange = () => load();
  window.addEventListener('library-changed', onChange);
  load();
  return () => {
    alive = false;
    window.removeEventListener('library-changed', onChange);
  };
}
