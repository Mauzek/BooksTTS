// Библиотека: папки и книги, как в облачном диске. В папке видны её
// подпапки и книги, лежащие прямо в ней; путь — «крошками» сверху. Книгу
// можно перетащить на папку или на крошку пути. Фильтры и поиск смотрят
// в текущую папку вместе со вложенными.

import { del, get, patch, post } from '../api.js';
import { MOOD_PALETTES, cover, learnBooks } from '../covers.js';
import * as player from '../player.js';
import { ask, confirmAction, dropdown, el, emit, formatSpan, guard, icon, plural, popupMenu, toast } from '../ui.js';

const PREFS_KEY = 'booktts-library';
const FILTERS = [
  ['all', 'Все'],
  ['listening', 'Слушаю'],
  ['ready', 'Готовы'],
  ['work', 'В работе'],
];
const SORTS = [['recent', 'Сначала недавние'], ['title', 'По названию']];
const DRAG_TYPE = 'application/x-booktts-book';

function loadPrefs() {
  try { return { filter: 'all', sort: 'recent', ...JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') }; } catch { return { filter: 'all', sort: 'recent' }; }
}
function savePrefs(prefs) {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* удобство, не данные */ }
}

/** Папки плоским списком с глубиной — для меню. */
function flattenFolders(folders, depth = 0, out = []) {
  for (const folder of folders) {
    out.push({ ...folder, depth });
    flattenFolders(folder.folders, depth + 1, out);
  }
  return out;
}

/** Путь до папки: [корень → … → папка]. */
function folderPath(folders, id, trail = []) {
  for (const folder of folders) {
    const path = [...trail, folder];
    if (folder.id === id) return path;
    const deeper = folderPath(folder.folders, id, path);
    if (deeper) return deeper;
  }
  return null;
}

/** Книги папки вместе с вложенными; без папки — вся библиотека. */
function booksIn(node) {
  const out = [];
  const walk = (current) => {
    out.push(...current.books);
    current.folders.forEach(walk);
  };
  walk(node);
  return out;
}

/** Выбрать папку из списка. undefined — отмена, null — «без папки». */
function chooseFolder(folders, current) {
  return new Promise((resolve) => {
    const dialog = el('dialog');
    const select = dropdown({
      className: 'wide', label: 'Папка', value: current ?? '',
      options: [{ value: '', label: 'Библиотека (без папки)' },
        ...flattenFolders(folders).map((f) => ({ value: f.id, label: f.name, depth: f.depth + 1 }))],
    });
    const done = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    dialog.append(
      el('h3', {}, 'Переместить в папку'),
      el('label', { class: 'field' }, select),
      el('div', { class: 'buttons' },
        el('button', { class: 'ghost', onclick: () => done(undefined) }, 'Отмена'),
        el('button', { class: 'primary', onclick: () => done(select.value ? Number(select.value) : null) }, 'Переместить')));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); done(undefined); });
    document.body.append(dialog);
    dialog.showModal();
    select.focus();
  });
}

export function render(view, folderId, { tag = '' } = {}) {
  let alive = true;
  let tree = { folders: [], books: [] };
  let listening = new Map();
  const prefs = loadPrefs();
  let query = '';
  // Жанр, настроение или метка: с экрана книги приходит через адрес, иначе — выбор в меню.
  let label = tag || '';

  const page = el('div', { class: 'page' });
  view.replaceChildren(page);

  const changed = () => emit('library-changed');
  const openFolder = (id) => { location.hash = id ? `#/library?folder=${id}` : '#/library'; };

  const load = guard(async () => {
    const [data, recent] = await Promise.all([
      get('/api/library'),
      get('/api/continue?limit=24').catch(() => ({ items: [] })),
    ]);
    if (!alive) return;
    tree = data;
    learnBooks(booksIn(tree));
    listening = new Map(recent.items.filter((item) => item.total_ms).map((item) => [item.book_id, item]));
    draw();
  });

  function status(book) {
    const item = listening.get(book.id);
    if (item && item.progress > 0 && item.progress < 1) {
      return { kind: 'listening', text: `${item.chapter_label || 'слушаю'} · ${Math.round(item.progress * 100)}%`, progress: item.progress };
    }
    if (book.chapters && book.voiced_chapters >= book.chapters) {
      return { kind: 'ready', text: `${formatSpan(book.duration_ms)} · готова` };
    }
    if (book.voiced_chapters) {
      return { kind: 'work', text: `озвучено ${book.voiced_chapters} из ${book.chapters} глав` };
    }
    return { kind: 'work', text: plural(book.chapters, 'глава', 'главы', 'глав') };
  }

  function matches(book, filter) {
    if (filter === 'all') return true;
    const kind = status(book).kind;
    if (filter === 'listening') return kind === 'listening';
    if (filter === 'ready') return kind === 'ready' || (kind === 'listening' && book.voiced_chapters >= book.chapters);
    return book.voiced_chapters < book.chapters;
  }

  const labelsOf = (book) => [...(book.genres || []), ...(book.moods || []), ...(book.tags || [])];
  const hasLabel = (book, value) => !value || labelsOf(book).some((item) => item.toLowerCase() === value.toLowerCase());

  /** Меню «Жанры и метки»: только то, что встречается в книгах раздела. */
  function labelMenu(anchor, books) {
    const count = (value) => books.filter((book) => hasLabel(book, value)).length;
    const unique = (key) => [...new Set(books.flatMap((book) => book[key] || []))].sort((a, b) => a.localeCompare(b, 'ru'));
    const items = [['Любые', () => { label = ''; draw(); }]];
    const group = (title, values) => {
      if (!values.length) return;
      items.push('-', [title, () => {}, 'menu-label', true]);
      for (const value of values) items.push([`${value} · ${count(value)}`, () => { label = value; draw(); }]);
    };
    group('Жанр', unique('genres'));
    group('Настроение', unique('moods'));
    group('Метки', unique('tags'));
    if (items.length === 1) items.push('-', ['Жанров и меток пока нет — их задают на странице книги', () => {}, '', true]);
    popupMenu(anchor, items);
  }

  const moveBook = guard(async (bookId, target) => {
    await post(`/api/books/${bookId}/move`, { parent_id: target, position: null });
    const name = target ? folderPath(tree.folders, target)?.at(-1)?.name : 'Библиотека';
    toast(`Книга перенесена в «${name}»`, 'ok');
    changed();
  });

  function bookMenu(book, anchor) {
    const folders = tree.folders;
    popupMenu(anchor, [
      ['Открыть', () => { location.hash = `#/book/${book.id}`; }],
      ['Слушать', guard(() => player.playBook(book.id)), '', !book.voiced_chapters],
      '-',
      ['Переименовать', guard(async () => {
        const title = await ask('Название книги', { value: book.title });
        if (title) { await patch(`/api/books/${book.id}`, { title }); changed(); }
      })],
      ['Переместить в папку…', guard(async () => {
        const target = await chooseFolder(folders, book.folder_id);
        if (target === undefined) return;
        await moveBook(book.id, target);
      }), '', !folders.length],
      '-',
      ['Удалить книгу', guard(async () => {
        const ok = await confirmAction(`Удалить книгу «${book.title}»?`,
          'Удалятся главы, разметка, озвучка и копия файла в библиотеке. Отменить это нельзя.');
        if (!ok) return;
        await del(`/api/books/${book.id}`);
        changed();
      }), 'danger'],
    ]);
  }

  /** Место, куда можно бросить книгу: папка-плитка или крошка пути. */
  function dropTarget(node, targetId) {
    node.addEventListener('dragover', (event) => {
      if (!event.dataTransfer.types.includes(DRAG_TYPE)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      node.classList.add('drop-here');
    });
    node.addEventListener('dragleave', () => node.classList.remove('drop-here'));
    node.addEventListener('drop', (event) => {
      node.classList.remove('drop-here');
      const id = Number(event.dataTransfer.getData(DRAG_TYPE));
      if (!id) return;
      event.preventDefault();
      moveBook(id, targetId);
    });
    return node;
  }

  function btile(book, index = 0) {
    const state = status(book);
    const more = el('button', { class: 'round sm btile-more', 'aria-label': `Действия: ${book.title}`, title: 'Действия' },
      icon('ellipsis'));
    more.onclick = (event) => { event.preventDefault(); event.stopPropagation(); bookMenu(book, more); };
    // Кнопка меню — рядом со ссылкой, а не внутри: вложенная кнопка ломает
    // и нажатие, и то, что читает экранный диктор.
    const link = el('a', { class: 'btile-link', href: `#/book/${book.id}`, draggable: 'true' },
      el('span', { class: 'btile-cover' }, cover(book)),
      el('span', { class: 'btile-caption' },
        el('span', { class: 'btile-title' }, book.title),
        el('span', { class: 'btile-sub' }, state.text),
        state.progress !== undefined
          ? el('span', { class: 'progress wide' }, el('i', { style: { width: `${state.progress * 100}%` } }))
          : null));
    link.addEventListener('dragstart', (event) => {
      event.dataTransfer.setData(DRAG_TYPE, String(book.id));
      event.dataTransfer.effectAllowed = 'move';
      page.classList.add('dragging');
    });
    link.addEventListener('dragend', () => page.classList.remove('dragging'));
    const tile = el('div', { class: 'btile', style: { '--i': index } }, link, more);
    tile.addEventListener('contextmenu', (event) => { event.preventDefault(); bookMenu(book, event); });
    return tile;
  }

  function folderMenu(anchor, folder) {
    popupMenu(anchor, [
      ['Открыть', () => openFolder(folder.id)],
      ['Переименовать…', guard(async () => {
        const name = await ask('Название папки', { value: folder.name });
        if (name) { await patch(`/api/folders/${folder.id}`, { name }); changed(); }
      })],
      ['Новая папка внутри…', () => newFolder(folder)],
      ['Озвучить все книги папки', guard(async () => {
        const ok = await confirmAction(`Озвучить папку «${folder.name}»?`,
          'В очередь встанут все главы всех книг папки, включая вложенные. Уже озвученное пропустится.', 'Озвучить');
        if (!ok) return;
        const job = await post(`/api/folders/${folder.id}/synthesize`, {});
        toast(`Задача добавлена: ${job.title}`, 'ok');
        emit('jobs-changed');
      })],
      '-',
      ['Удалить папку', guard(async () => {
        const ok = await confirmAction(`Удалить папку «${folder.name}»?`,
          'Вложенные папки удалятся вместе с ней. Книги не удалятся — переедут на уровень выше.');
        if (!ok) return;
        await del(`/api/folders/${folder.id}`);
        if (folder.id === folderId) openFolder(folder.parent_id);
        changed();
      }), 'danger'],
    ]);
  }

  /** Папка-плитка: корешки первых книг, название, сколько внутри. */
  function ftile(folder, index = 0) {
    const books = booksIn(folder);
    const more = el('button', { class: 'round sm btile-more', 'aria-label': `Действия с папкой ${folder.name}`, title: 'Действия' },
      icon('ellipsis'));
    more.onclick = (event) => { event.preventDefault(); event.stopPropagation(); folderMenu(more, folder); };
    const inside = [
      books.length ? plural(books.length, 'книга', 'книги', 'книг') : 'пусто',
      folder.folders.length ? plural(folder.folders.length, 'папка', 'папки', 'папок') : '',
    ].filter(Boolean).join(' · ');
    const link = el('a', { class: 'btile-link', href: `#/library?folder=${folder.id}` },
      el('span', { class: 'ftile-body' },
        el('span', { class: 'ftile-tab', 'aria-hidden': 'true' }),
        el('span', { class: 'ftile-covers', 'aria-hidden': 'true' },
          books.slice(0, 3).map((book) => el('span', { class: 'ftile-cover' }, cover(book, { text: false })))),
        el('span', { class: 'ftile-icon', 'aria-hidden': 'true' }, books.length ? null : icon('folder', { className: 'huge' }))),
      el('span', { class: 'btile-caption' },
        el('span', { class: 'btile-title' }, folder.name),
        el('span', { class: 'btile-sub' }, inside)));
    const tile = dropTarget(el('div', { class: 'btile ftile', style: { '--i': index } }, link, more), folder.id);
    tile.addEventListener('contextmenu', (event) => { event.preventDefault(); folderMenu(event, folder); });
    return tile;
  }

  const newFolder = guard(async (parent) => {
    const name = await ask(parent ? `Новая папка в «${parent.name}»` : 'Новая папка', { placeholder: 'Название' });
    if (!name) return;
    await post('/api/folders', { name, parent_id: parent?.id ?? null });
    toast(`Папка «${name}» создана`, 'ok');
    changed();
  });

  const grid = el('div', { class: 'btile-grid' });
  const searchInput = el('input', {
    type: 'search', placeholder: 'Название, автор или метка · Enter — искать в тексте', 'aria-label': 'Найти книгу',
  });
  searchInput.oninput = () => { query = searchInput.value.trim().toLowerCase(); drawGrid(); };
  searchInput.onkeydown = (event) => {
    if (event.key === 'Enter' && searchInput.value.trim()) {
      location.hash = `#/search?q=${encodeURIComponent(searchInput.value.trim())}`;
    }
  };
  const filterRow = el('div', { class: 'lib-filters' });
  const scopeNote = el('p', { class: 'hint scope-note', hidden: true });

  function sorted(books) {
    return [...books].sort(prefs.sort === 'title'
      ? (a, b) => a.title.localeCompare(b.title, 'ru')
      : (a, b) => (b.created_at || '').localeCompare(a.created_at || '') || b.id - a.id);
  }

  function drawGrid() {
    const folder = folderId ? folderPath(tree.folders, folderId)?.at(-1) : null;
    const node = folder || tree;
    // Фильтр или поиск — ищем во всей папке, со вложенными; иначе — как в
    // проводнике: подпапки и книги, лежащие прямо здесь.
    const searching = Boolean(query || label || prefs.filter !== 'all');
    const add = el('button', { class: 'add-tile', title: 'Добавить книгу (Ctrl+O)', onclick: () => emit('import-request', folderId) },
      el('span', { class: 'add-icon' }, icon('plus', { className: 'big' })),
      el('b', {}, folder ? `Добавить в «${folder.name}»` : 'Добавить книгу'),
      el('span', { class: 'hint' }, 'или перетащите файл в окно'),
      el('span', { class: 'hint small' }, 'txt · fb2 · epub · docx · pdf'));

    if (searching) {
      let books = booksIn(node).filter((book) => matches(book, prefs.filter) && hasLabel(book, label));
      if (query) books = books.filter((book) => [book.title, book.author, ...labelsOf(book)].join(' ').toLowerCase().includes(query));
      grid.replaceChildren(...sorted(books).map(btile));
      if (!books.length) grid.append(el('div', { class: 'btile-empty hint' }, query ? 'Ничего не нашлось.' : 'Под эти условия книг нет.'));
      scopeNote.hidden = !node.folders.length;
      scopeNote.textContent = folder ? `Показаны книги из «${folder.name}» и вложенных папок.` : 'Показаны книги из всех папок.';
      return;
    }
    scopeNote.hidden = true;
    grid.replaceChildren(add,
      ...node.folders.map(ftile),
      ...sorted(node.books).map((book, index) => btile(book, index + node.folders.length)));
    if (!node.books.length && !node.folders.length) {
      grid.append(el('div', { class: 'btile-empty hint' }, folder
        ? 'Папка пуста. Перетащите сюда книгу из библиотеки или добавьте новую.'
        : 'Здесь появятся ваши книги.'));
    }
  }

  function draw() {
    const path = folderId ? folderPath(tree.folders, folderId) : [];
    if (folderId && !path) { openFolder(null); return; }
    const folder = path.at(-1) || null;
    const scoped = booksIn(folder || tree);

    const chips = FILTERS.map(([key, text]) => {
      const count = scoped.filter((book) => matches(book, key)).length;
      const chip = el('button', { class: 'chip-filter', 'aria-pressed': String(prefs.filter === key) },
        text, el('span', { class: 'count' }, String(count)));
      chip.onclick = () => { prefs.filter = key; savePrefs(prefs); draw(); };
      return chip;
    });
    const labelButton = el('button', { class: label ? 'chip-filter' : 'surface', 'aria-pressed': String(Boolean(label)), 'aria-haspopup': 'menu' },
      label && MOOD_PALETTES[label] ? el('span', { class: 'mood-dot', style: { background: MOOD_PALETTES[label][0] } }) : icon('tag'),
      label || 'Жанры и метки', icon('chevron-down'));
    labelButton.onclick = () => labelMenu(labelButton, scoped);
    const clearLabel = el('button', {
      class: 'round ghost sm', 'aria-label': 'Сбросить жанр или метку', title: 'Сбросить', hidden: !label,
      onclick: () => { label = ''; if (location.hash.includes('?tag=')) location.hash = '#/library'; else draw(); },
    }, icon('x'));
    const sortButton = el('button', { class: 'ghost', 'aria-haspopup': 'menu' },
      SORTS.find(([key]) => key === prefs.sort)[1], icon('chevron-down'));
    sortButton.onclick = () => popupMenu(sortButton, SORTS.map(([key, text]) => [text, () => {
      prefs.sort = key; savePrefs(prefs); draw();
    }]));

    filterRow.replaceChildren(...chips,
      el('span', { class: 'divider' }),
      labelButton, clearLabel,
      el('span', { class: 'grow' }),
      el('button', { class: 'ghost accent', onclick: () => newFolder(folder) }, icon('folder-plus'), folder ? 'Папка внутри' : 'Новая папка'),
      sortButton);

    // Путь: «Библиотека › Папка › Подпапка». На крошку можно бросить книгу.
    const crumbs = el('nav', { class: 'crumbs lib-crumbs', 'aria-label': 'Путь' },
      dropTarget(el('a', { href: '#/library', class: folder ? '' : 'here' }, 'Библиотека'), null),
      path.flatMap((item, index) => [
        el('span', { 'aria-hidden': 'true' }, '›'),
        index === path.length - 1
          ? el('span', { class: 'here' }, item.name)
          : dropTarget(el('a', { href: `#/library?folder=${item.id}` }, item.name), item.id),
      ]));

    const folderActions = folder ? el('button', {
      class: 'round ghost', 'aria-label': `Действия с папкой ${folder.name}`, title: 'Действия с папкой',
      onclick: (event) => folderMenu(event.currentTarget, folder),
    }, icon('ellipsis')) : null;

    // replaceChildren печатает null текстом — пустые части отбрасываем.
    page.replaceChildren(...[
      el('div', { class: 'lib-head' },
        el('h1', {}, folder ? folder.name : 'Библиотека'),
        el('span', { class: 'hint' }, plural(scoped.length, 'книга', 'книги', 'книг')),
        folderActions,
        el('label', { class: 'search-field' }, icon('search'), searchInput)),
      folder ? crumbs : null,
      filterRow,
      scopeNote,
      grid,
    ].filter(Boolean));
    drawGrid();
  }

  const onChange = () => load();
  window.addEventListener('library-changed', onChange);
  load();
  return () => {
    alive = false;
    window.removeEventListener('library-changed', onChange);
  };
}
