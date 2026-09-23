// Главная: что слушали последним и что лежит в библиотеке.

import { get } from '../api.js';
import * as player from '../player.js';
import { el, emit, formatDuration, formatSpan, guard, icon, plural } from '../ui.js';

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

function cover(bookId, coverPath, className = 'cover') {
  return coverPath
    ? el('img', { class: className, src: `/api/books/${bookId}/cover`, alt: '', loading: 'lazy' })
    : el('div', { class: className }, icon('book-open', { className: 'huge' }));
}

function card(book) {
  const voiced = book.voiced_chapters || 0;
  return el('a', { class: 'card', href: `#/book/${book.id}` },
    cover(book.id, book.cover_path),
    el('div', { class: 'card-title' }, book.title),
    el('div', { class: 'card-sub' },
      book.author || book.path || plural(book.chapters, 'глава', 'главы', 'глав')),
    voiced
      ? el('div', { class: 'card-meta' }, icon('headphones', { size: 13 }), formatSpan(book.duration_ms))
      : null);
}

function continueCard(item) {
  const resume = guard(async (event) => {
    event.preventDefault();
    await player.playBook(item.book_id, { fromChapter: item.chapter_id });
  });
  const left = Math.max(0, item.total_ms - item.listened_ms);
  return el('div', { class: 'resume' },
    el('a', { href: `#/book/${item.book_id}`, class: 'resume-cover' }, cover(item.book_id, item.cover_path)),
    el('div', { class: 'resume-body' },
      el('a', { class: 'resume-title', href: `#/book/${item.book_id}` }, item.title),
      el('div', { class: 'hint' }, item.chapter_label || 'ещё не начата'),
      el('div', { class: 'progress wide', title: `${Math.round(item.progress * 100)}%` },
        el('i', { style: { width: `${item.progress * 100}%` } })),
      el('div', { class: 'row' },
        el('button', { class: 'primary', onclick: resume }, icon('play'), 'Продолжить'),
        el('span', { class: 'hint' },
          item.total_ms ? `осталось ${formatDuration(left)}` : 'нет озвученных глав'))));
}

export function render(view) {
  let alive = true;
  const page = el('div', { class: 'page' });
  view.replaceChildren(page);

  const load = guard(async () => {
    const [tree, recent] = await Promise.all([
      get('/api/library'),
      get('/api/continue').catch(() => ({ items: [] })),
    ]);
    if (!alive) return;
    const books = flatten(tree);

    if (!books.length) {
      page.replaceChildren(el('div', { class: 'empty-state' },
        el('div', { class: 'big' }, icon('library', { className: 'huge' })),
        el('h1', {}, 'Библиотека пуста'),
        el('p', {}, 'Добавьте книгу в формате txt, epub, fb2, docx или pdf — или просто перетащите файлы в окно.'),
        el('button', { class: 'primary', onclick: () => emit('import-request', null) },
          icon('book-plus'), 'Добавить книгу')));
      return;
    }

    const chapters = books.reduce((n, book) => n + book.chapters, 0);
    const duration = books.reduce((n, book) => n + (book.duration_ms || 0), 0);
    const recentBooks = [...books].sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    const listening = recent.items.filter((item) => item.total_ms);

    page.replaceChildren(
      el('h1', {}, 'Библиотека'),
      el('div', { class: 'stats' },
        el('span', {}, el('b', {}, plural(books.length, 'книга', 'книги', 'книг'))),
        el('span', {}, el('b', {}, plural(chapters, 'глава', 'главы', 'глав'))),
        el('span', {}, 'озвучено ', el('b', {}, formatSpan(duration)))),
      listening.length ? el('h2', {}, 'Продолжить слушать') : null,
      listening.length ? el('div', { class: 'resume-list' }, listening.map(continueCard)) : null,
      el('h2', {}, 'Недавно добавленные'),
      el('div', { class: 'cards' }, recentBooks.map(card)),
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
