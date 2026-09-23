// Дерево библиотеки: папки с вложенностью, книги, перетаскивание, переименование.

import { del, get, patch, post } from './api.js';
import { confirmAction, el, emit, icon, toast } from './ui.js';

const EXPANDED_KEY = 'booktts-tree-expanded';
const NODE_MIME = 'application/x-booktts-node';

function readExpanded() {
  try {
    return JSON.parse(localStorage.getItem(EXPANDED_KEY) || '[]');
  } catch {
    return [];
  }
}

function saveExpanded(expanded) {
  try {
    localStorage.setItem(EXPANDED_KEY, JSON.stringify([...expanded]));
  } catch {
    // свёрнутость папок — удобство, а не данные
  }
}

function countBooks(folder) {
  return folder.books.length + folder.folders.reduce((n, child) => n + countBooks(child), 0);
}

function popupMenu(x, y, items) {
  document.querySelector('.menu-pop')?.remove();
  const menu = el('div', { class: 'menu-pop', role: 'menu' },
    items.map(([label, action, extra]) => el('button', {
      class: `ghost ${extra || ''}`,
      role: 'menuitem',
      onclick: () => { menu.remove(); action(); },
    }, label)));
  document.body.append(menu);
  const { width, height } = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, innerWidth - width - 8)}px`;
  menu.style.top = `${Math.min(y, innerHeight - height - 8)}px`;
  const close = (event) => {
    if (menu.contains(event.target)) return;
    menu.remove();
    document.removeEventListener('mousedown', close, true);
  };
  setTimeout(() => document.addEventListener('mousedown', close, true), 0);
  menu.querySelector('button')?.focus();
}

export class LibraryTree {
  constructor(host, { onImportFiles, onImportRequest }) {
    this.host = host;
    this.onImportFiles = onImportFiles;
    this.onImportRequest = onImportRequest;
    this.data = { folders: [], books: [] };
    this.activeBook = null;
    this.selectedFolder = null;
    this.dragging = null;
    this.expanded = new Set(readExpanded());
  }

  async refresh() {
    this.data = await get('/api/library');
    this.draw();
  }

  expand(folderId) {
    if (folderId === null || folderId === undefined) return;
    this.expanded.add(folderId);
    saveExpanded(this.expanded);
  }

  setActiveBook(bookId) {
    this.activeBook = bookId;
    if (bookId !== null) this.ancestorsOfBook(bookId).forEach((id) => this.expand(id));
    this.draw();
  }

  revealFolder(folderId) {
    this.ancestorsOfFolder(folderId).forEach((id) => this.expand(id));
    this.selectedFolder = folderId;
    this.draw();
    this.host.querySelector(`[data-folder="${folderId}"]`)?.scrollIntoView({ block: 'nearest' });
  }

  ancestorsOfBook(bookId) {
    const walk = (folders, trail) => {
      for (const folder of folders) {
        const here = [...trail, folder.id];
        if (folder.books.some((b) => b.id === bookId)) return here;
        const deeper = walk(folder.folders, here);
        if (deeper) return deeper;
      }
      return null;
    };
    return walk(this.data.folders, []) || [];
  }

  ancestorsOfFolder(folderId) {
    const walk = (folders, trail) => {
      for (const folder of folders) {
        if (folder.id === folderId) return trail;
        const deeper = walk(folder.folders, [...trail, folder.id]);
        if (deeper) return deeper;
      }
      return null;
    };
    return walk(this.data.folders, []) || [];
  }

  childrenOf(folderId) {
    if (folderId === null || folderId === undefined) return this.data;
    const find = (folders) => {
      for (const folder of folders) {
        if (folder.id === folderId) return folder;
        const deeper = find(folder.folders);
        if (deeper) return deeper;
      }
      return null;
    };
    return find(this.data.folders) || { folders: [], books: [] };
  }

  changed() {
    emit('library-changed');
  }

  // ---------- отрисовка ----------

  draw() {
    const list = el('ul', {},
      this.data.folders.map((folder) => this.folderItem(folder)),
      this.data.books.map((book) => this.bookItem(book, null)));
    const nothing = !this.data.folders.length && !this.data.books.length;
    const rootDrop = el('div', { class: 'root-drop hint' },
      nothing ? 'Пока пусто. Добавьте книгу кнопкой выше или перетащите файл в окно.' : '');
    this.dropTarget(rootDrop, () => ({ kind: 'root' }), 'root');
    this.host.replaceChildren(list, rootDrop);
  }

  folderItem(folder) {
    const open = this.expanded.has(folder.id);
    const hasChildren = folder.folders.length || folder.books.length;
    const node = el('div', {
      class: `node${this.selectedFolder === folder.id ? ' selected' : ''}`,
      draggable: 'true', tabindex: '0', title: folder.name, dataset: { folder: folder.id },
    },
      el('span', { class: 'twisty' },
        hasChildren ? icon(open ? 'chevron-down' : 'chevron-right', { size: 12 }) : ''),
      el('span', { class: 'glyph' }, icon(open ? 'folder-open' : 'folder')),
      el('span', { class: 'name' }, folder.name),
      el('span', { class: 'meta' }, countBooks(folder) || ''),
    );

    const actions = () => [
      ['Переименовать', () => this.renameInline(node, folder.name,
        (name) => patch(`/api/folders/${folder.id}`, { name }))],
      ['Новая подпапка', () => this.createSubfolder(folder)],
      ['Добавить книги сюда…', () => this.onImportRequest(folder.id)],
      ['Озвучить все книги папки', () => this.synthesizeFolder(folder)],
      ['Удалить папку', () => this.deleteFolder(folder), 'danger'],
    ];
    node.addEventListener('click', () => {
      this.selectedFolder = folder.id;
      if (open) this.expanded.delete(folder.id); else this.expanded.add(folder.id);
      saveExpanded(this.expanded);
      this.draw();
    });
    node.addEventListener('dblclick', (event) => { event.preventDefault(); actions()[0][1](); });
    node.addEventListener('contextmenu', (event) => { event.preventDefault(); popupMenu(event.clientX, event.clientY, actions()); });
    node.addEventListener('keydown', (event) => {
      if (event.key === 'F2') actions()[0][1]();
      if (event.key === 'Delete') this.deleteFolder(folder);
      if (event.key === 'Enter') node.click();
    });

    this.dragSource(node, { kind: 'folder', id: folder.id, parentId: folder.parent_id });
    this.dropTarget(node, (zone) => ({ kind: 'folder', folder, zone }), 'folder');

    const item = el('li', {}, node);
    if (open) {
      item.append(el('ul', {},
        folder.folders.map((child) => this.folderItem(child)),
        folder.books.map((book) => this.bookItem(book, folder.id))));
    }
    return item;
  }

  bookItem(book, folderId) {
    const node = el('div', {
      class: `node${this.activeBook === book.id ? ' active' : ''}`,
      draggable: 'true', tabindex: '0', title: book.author ? `${book.title} — ${book.author}` : book.title,
      dataset: { book: book.id },
    },
      el('span', { class: 'twisty' }),
      el('span', { class: 'glyph' }, icon('book-marked')),
      el('span', { class: 'name' }, book.title),
      el('span', { class: 'meta' }, book.chapters || ''),
    );
    const actions = () => [
      ['Открыть', () => { location.hash = `#/book/${book.id}`; }],
      ['Переименовать', () => this.renameInline(node, book.title,
        (title) => patch(`/api/books/${book.id}`, { title }))],
      ['Удалить книгу', () => this.deleteBook(book), 'danger'],
    ];
    node.addEventListener('click', () => { location.hash = `#/book/${book.id}`; });
    node.addEventListener('dblclick', (event) => { event.preventDefault(); actions()[1][1](); });
    node.addEventListener('contextmenu', (event) => { event.preventDefault(); popupMenu(event.clientX, event.clientY, actions()); });
    node.addEventListener('keydown', (event) => {
      if (event.key === 'F2') actions()[1][1]();
      if (event.key === 'Delete') this.deleteBook(book);
      if (event.key === 'Enter') node.click();
    });
    this.dragSource(node, { kind: 'book', id: book.id, parentId: folderId });
    this.dropTarget(node, (zone) => ({ kind: 'book', book, folderId, zone }), 'book');
    return el('li', {}, node);
  }

  // ---------- правка ----------

  renameInline(node, current, save) {
    const name = node.querySelector('.name');
    if (!name) return;
    const input = el('input', { class: 'rename', value: current });
    name.replaceWith(input);
    node.draggable = false;
    input.focus();
    input.select();
    let finished = false;
    const finish = async (commit) => {
      if (finished) return;
      finished = true;
      const value = input.value.trim();
      if (commit && value && value !== current) {
        try {
          await save(value);
        } catch (error) {
          toast(error.message, 'error');
        }
      }
      this.changed();
    };
    for (const type of ['click', 'dblclick', 'mousedown']) input.addEventListener(type, (e) => e.stopPropagation());
    input.addEventListener('keydown', (event) => {
      event.stopPropagation();
      if (event.key === 'Enter') finish(true);
      if (event.key === 'Escape') finish(false);
    });
    input.addEventListener('blur', () => finish(true));
  }

  async createSubfolder(parent) {
    const { ask } = await import('./ui.js');
    const name = await ask(`Новая папка в «${parent.name}»`, { placeholder: 'Название' });
    if (!name) return;
    try {
      await post('/api/folders', { name, parent_id: parent.id });
      this.expand(parent.id);
      this.changed();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async synthesizeFolder(folder) {
    const ok = await confirmAction(
      `Озвучить папку «${folder.name}»?`,
      'В очередь встанут все главы всех книг папки, включая вложенные. Уже озвученное пропустится.',
      'Озвучить',
    );
    if (!ok) return;
    try {
      const job = await post(`/api/folders/${folder.id}/synthesize`, {});
      toast(`Задача добавлена: ${job.title}`, 'ok');
      emit('jobs-changed');
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async deleteFolder(folder) {
    const ok = await confirmAction(
      `Удалить папку «${folder.name}»?`,
      'Вложенные папки удалятся вместе с ней. Книги останутся — переедут на уровень выше.',
    );
    if (!ok) return;
    try {
      await del(`/api/folders/${folder.id}`);
      if (this.selectedFolder === folder.id) this.selectedFolder = null;
      this.changed();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async deleteBook(book) {
    const ok = await confirmAction(
      `Удалить книгу «${book.title}»?`,
      'Удалятся главы, разметка, озвучка и копия файла в библиотеке. Отменить это нельзя.',
    );
    if (!ok) return;
    try {
      await del(`/api/books/${book.id}`);
      if (this.activeBook === book.id) location.hash = '#/';
      this.changed();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  // ---------- перетаскивание ----------

  dragSource(node, payload) {
    node.addEventListener('dragstart', (event) => {
      this.dragging = payload;
      event.dataTransfer.setData(NODE_MIME, JSON.stringify(payload));
      event.dataTransfer.effectAllowed = 'move';
    });
    node.addEventListener('dragend', () => {
      this.dragging = null;
      this.clearMarks();
    });
  }

  clearMarks() {
    for (const marked of this.host.querySelectorAll('.drop-into, .drop-before, .drop-after')) {
      marked.classList.remove('drop-into', 'drop-before', 'drop-after');
    }
  }

  zone(event, node, kind, files) {
    if (kind === 'root') return 'into';
    if (files) return kind === 'folder' ? 'into' : null;
    const rect = node.getBoundingClientRect();
    const ratio = (event.clientY - rect.top) / rect.height;
    if (kind === 'folder') return ratio < 0.25 ? 'before' : ratio > 0.75 ? 'after' : 'into';
    return ratio < 0.5 ? 'before' : 'after';
  }

  dropTarget(node, describe, kind) {
    node.addEventListener('dragover', (event) => {
      const types = [...event.dataTransfer.types];
      const files = types.includes('Files');
      if (!files && !types.includes(NODE_MIME)) return;
      const zone = this.zone(event, node, kind, files);
      if (!zone) return;
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = files ? 'copy' : 'move';
      this.clearMarks();
      node.classList.add(`drop-${zone}`);
    });
    node.addEventListener('dragleave', () => node.classList.remove('drop-into', 'drop-before', 'drop-after'));
    node.addEventListener('drop', async (event) => {
      const types = [...event.dataTransfer.types];
      const files = types.includes('Files');
      const zone = this.zone(event, node, kind, files);
      if (!zone) return;
      event.preventDefault();
      event.stopPropagation();
      this.clearMarks();
      const target = describe(zone);
      if (files) {
        const folderId = target.kind === 'folder' ? target.folder.id : null;
        this.onImportFiles([...event.dataTransfer.files], folderId);
        return;
      }
      const item = this.dragging;
      this.dragging = null;
      if (!item) return;
      try {
        await this.move(item, target);
        this.changed();
      } catch (error) {
        toast(error.message, 'error');
      }
    });
  }

  async move(item, target) {
    const url = item.kind === 'folder' ? `/api/folders/${item.id}/move` : `/api/books/${item.id}/move`;
    const send = (parentId, position = null) => post(url, { parent_id: parentId, position });

    if (target.kind === 'root') return send(null);

    if (target.kind === 'folder') {
      const { folder, zone } = target;
      if (item.kind === 'folder' && item.id === folder.id) return undefined;
      if (zone === 'into') {
        this.expand(folder.id);
        return send(folder.id);
      }
      if (item.kind === 'book') return send(folder.parent_id);
      const siblings = this.childrenOf(folder.parent_id).folders.map((f) => f.id).filter((id) => id !== item.id);
      const index = siblings.indexOf(folder.id) + (zone === 'after' ? 1 : 0);
      return send(folder.parent_id, index);
    }

    const { book, folderId, zone } = target;
    if (item.kind === 'folder') return send(folderId);
    if (item.id === book.id) return undefined;
    const siblings = this.childrenOf(folderId).books.map((b) => b.id).filter((id) => id !== item.id);
    const index = siblings.indexOf(book.id) + (zone === 'after' ? 1 : 0);
    return send(folderId, index);
  }
}
