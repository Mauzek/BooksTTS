// Каркас приложения: маршруты, боковая панель, импорт.

import { get, importBook, notify, pickBookFiles, post } from './api.js';
import { LibraryTree } from './tree.js';
import { applyTheme, currentTheme, nextTheme, themeIcon, themeTitle } from './theme.js';
import { $, ask, el, emit, guard, icon, plural, toast } from './ui.js';
import * as playerBar from './player-bar.js';
import * as bookScreen from './screens/book.js';
import * as chapterScreen from './screens/chapter.js';
import * as editorScreen from './screens/editor.js';
import * as homeScreen from './screens/home.js';
import * as jobsScreen from './screens/jobs.js';
import * as searchScreen from './screens/search.js';
import * as settingsScreen from './screens/settings.js';
import * as statsScreen from './screens/stats.js';
import * as voicesScreen from './screens/voices.js';

const view = $('#view');
let extensions = ['.txt', '.epub', '.fb2', '.docx', '.pdf'];

const routes = [
  [/^#?\/?$/, () => homeScreen.render(view), 'library'],
  [/^#\/book\/(\d+)$/, ([id]) => bookScreen.render(view, Number(id)), 'library'],
  [/^#\/chapter\/(\d+)\/edit$/, ([id]) => editorScreen.render(view, Number(id)), 'library'],
  [/^#\/chapter\/(\d+)$/, ([id]) => chapterScreen.render(view, Number(id)), 'library'],
  [/^#\/search\?q=(.*)$/, ([q]) => searchScreen.render(view, decodeURIComponent(q)), 'library'],
  [/^#\/voices$/, () => voicesScreen.render(view), 'voices'],
  [/^#\/settings$/, () => settingsScreen.render(view), 'settings'],
  [/^#\/jobs$/, () => jobsScreen.render(view), 'jobs'],
  [/^#\/stats$/, () => statsScreen.render(view), 'stats'],
];

// ---------- импорт ----------

async function importItems(items, folderId = null) {
  let last = null;
  for (const item of items) {
    const name = item.path ? item.path.split(/[\\/]/).pop() : item.file.name;
    toast(`Импортирую «${name}»…`);
    try {
      const book = await importBook(item, folderId);
      last = book;
      toast(`Добавлена «${book.title}»: ${plural(book.chapters.length, 'глава', 'главы', 'глав')}`, 'ok');
    } catch (error) {
      toast(`${name}: ${error.message}`, 'error');
    }
  }
  if (last) {
    emit('library-changed');
    location.hash = `#/book/${last.id}`;
  }
}

const requestImport = guard(async (folderId = tree.selectedFolder) => {
  const items = await pickBookFiles(extensions);
  if (items.length) await importItems(items, folderId ?? null);
});

const tree = new LibraryTree($('#tree'), {
  onImportFiles: (files, folderId) => importItems(files.map((file) => ({ file })), folderId),
  onImportRequest: (folderId) => requestImport(folderId),
});

$('#import').onclick = () => requestImport();
$('#new-folder').onclick = guard(async () => {
  const parent = tree.selectedFolder;
  const name = await ask(parent ? 'Новая подпапка' : 'Новая папка', { placeholder: 'Название' });
  if (!name) return;
  const folder = await post('/api/folders', { name, parent_id: parent ?? null });
  tree.expand(parent);
  tree.selectedFolder = folder.id;
  emit('library-changed');
});

// ---------- перетаскивание файлов в окно ----------

let overlay = null;
let dragDepth = 0;
const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes('Files');

window.addEventListener('dragenter', (event) => {
  if (!hasFiles(event)) return;
  dragDepth += 1;
  if (!overlay) {
    overlay = el('div', { class: 'drop-overlay' }, 'Отпустите, чтобы добавить книги');
    document.body.append(overlay);
  }
});
window.addEventListener('dragleave', (event) => {
  if (!hasFiles(event)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) { overlay?.remove(); overlay = null; }
});
window.addEventListener('dragover', (event) => { if (hasFiles(event)) event.preventDefault(); });
window.addEventListener('drop', (event) => {
  dragDepth = 0; overlay?.remove(); overlay = null;
  if (!hasFiles(event)) return;
  event.preventDefault();
  // Бросили на папку в дереве — это обработало дерево; сюда доходит всё остальное.
  importItems([...event.dataTransfer.files].map((file) => ({ file })), null);
});

// ---------- поиск ----------

let searchTimer = null;
$('#search').addEventListener('input', (event) => {
  clearTimeout(searchTimer);
  const query = event.target.value.trim();
  searchTimer = setTimeout(() => {
    if (query) location.hash = `#/search?q=${encodeURIComponent(query)}`;
    else if (location.hash.startsWith('#/search')) location.hash = '#/';
  }, 250);
});

// ---------- маршруты ----------

let cleanup = null;

function route() {
  if (typeof cleanup === 'function') cleanup();
  cleanup = null;
  const hash = location.hash || '#/';
  for (const [pattern, handler, section] of routes) {
    const match = hash.match(pattern);
    if (!match) continue;
    cleanup = handler(match.slice(1));
    const active = section || match[1];
    for (const link of document.querySelectorAll('#nav a')) {
      link.classList.toggle('active', link.dataset.route === active);
    }
    const bookMatch = hash.match(/^#\/book\/(\d+)$/);
    tree.setActiveBook(bookMatch ? Number(bookMatch[1]) : null);
    if (!hash.startsWith('#/search')) $('#search').value = '';
    return;
  }
  location.hash = '#/';
}

window.addEventListener('hashchange', route);
window.addEventListener('library-changed', () => tree.refresh().catch((e) => toast(e.message, 'error')));
window.addEventListener('active-book', (event) => tree.setActiveBook(event.detail));
window.addEventListener('import-request', (event) => requestImport(event.detail));
window.addEventListener('reveal-folder', (event) => tree.revealFolder(event.detail));

// ---------- наблюдатель за задачами ----------
// Живёт поверх экранов: уведомление о готовой книге придёт, даже если открыт
// другой экран. Пока задач нет, опрос редкий.

const JOBS_IDLE_MS = 5000;
const JOBS_BUSY_MS = 1500;
let jobsTimer = null;

async function watchJobs() {
  let delay = JOBS_IDLE_MS;
  try {
    const data = await get('/api/jobs?active=true');
    const badge = $('#jobs-badge');
    badge.hidden = !data.active;
    badge.textContent = data.active || '';
    if (data.active) delay = JOBS_BUSY_MS;

    for (const job of data.announce || []) {
      const ok = job.status === 'done';
      notify(ok ? 'Готово' : 'Задача не доделана', job.title +
        (job.error ? `: ${job.error}` : ''));
      toast(`${job.title}: ${ok ? 'готово' : job.error || 'ошибка'}`, ok ? 'ok' : 'error');
      await post(`/api/jobs/${job.id}/announced`);
      emit('library-changed');
    }
    if (data.announce?.length) emit('jobs-changed');
  } catch {
    delay = JOBS_IDLE_MS;  // бэкенд перезапускается — попробуем позже
  }
  clearTimeout(jobsTimer);
  jobsTimer = setTimeout(watchJobs, delay);
}

// ---------- тема ----------

function drawThemeButton() {
  const theme = currentTheme();
  const button = $('#theme');
  button.replaceChildren(icon(themeIcon(theme)), themeTitle(theme));
  button.title = 'Тема оформления: нажмите, чтобы сменить';
}

$('#theme').onclick = () => { nextTheme(); drawThemeButton(); };
window.addEventListener('theme-changed', drawThemeButton);
applyTheme(currentTheme());
drawThemeButton();

playerBar.mount($('#player'));
get('/api/formats').then((data) => { extensions = data.extensions; }).catch(() => {});
watchJobs();
tree.refresh().catch((error) => toast(error.message, 'error'));
route();
