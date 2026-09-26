// Каркас приложения: шапка, маршруты, история переходов, импорт.

import { get, importBook, notify, pickBookFiles, post, tauri } from './api.js';
import { applyTheme, currentTheme, nextTheme, themeIcon, themeTitle } from './theme.js';
import { $, el, emit, guard, humanError, icon, loadIcons, plural, toast } from './ui.js';
import { jobFix, jobRoute } from './screens/studio/common.js';
import { setActiveJobs } from './jobs-state.js';
import { maybeShowWhatsNew, showWhatsNew, updateDialog } from './whatsnew.js';
import * as playerBar from './player-bar.js';
import * as bookScreen from './screens/book.js';
import * as chapterScreen from './screens/chapter.js';
import * as editorScreen from './screens/editor.js';
import * as homeScreen from './screens/home.js';
import * as libraryScreen from './screens/library.js';
import * as searchScreen from './screens/search.js';
import * as settingsScreen from './screens/settings.js';
import * as studioWorkspace from './screens/studio/workspace.js';
import * as studioCatalog from './screens/studio/catalog.js';
import * as studioJobs from './screens/studio/jobs.js';
import * as studioOverview from './screens/studio/overview.js';
import * as studioPronunciation from './screens/studio/pronunciation.js';
import * as studioStats from './screens/studio/stats.js';

const view = $('#view');
let extensions = ['.txt', '.epub', '.fb2', '.docx', '.pdf'];

// [шаблон, отрисовка, раздел в шапке]
const routes = [
  [/^#?\/?$/, () => homeScreen.render(view), 'listen'],
  [/^#\/library(?:\?folder=(\d+))?$/, ([folder]) => libraryScreen.render(view, folder ? Number(folder) : null), 'library'],
  [/^#\/library\?tag=([^&]*)$/, ([tag]) => libraryScreen.render(view, null, { tag: decodeURIComponent(tag) }), 'library'],
  [/^#\/book\/(\d+)$/, ([id]) => bookScreen.render(view, Number(id)), 'library'],
  [/^#\/chapter\/(\d+)\/edit$/, ([id]) => editorScreen.render(view, Number(id)), 'studio'],
  [/^#\/chapter\/(\d+)$/, ([id]) => chapterScreen.render(view, Number(id)), 'library'],
  [/^#\/search(?:\?q=(.*))?$/, ([q]) => searchScreen.render(view, decodeURIComponent(q || '')), ''],
  [/^#\/studio$/, () => studioOverview.render(view), 'studio'],
  [/^#\/studio\/jobs$/, () => studioJobs.render(view), 'studio'],
  [/^#\/studio\/voices$/, () => studioCatalog.render(view), 'studio'],
  [/^#\/studio\/pronunciation(?:\?book=(\d+))?$/, ([book]) => studioPronunciation.render(view, book ? Number(book) : null), 'studio'],
  [/^#\/studio\/stats$/, () => studioStats.render(view), 'studio'],
  [/^#\/studio\/book\/(\d+)(?:\/(text|roles|voices|voice|done))?$/, ([id, step]) => studioWorkspace.render(view, Number(id), step || null), 'studio'],
  // Старые адреса — на новые места, без лишней записи в истории.
  [/^#\/jobs$/, () => location.replace('#/studio/jobs'), 'studio'],
  [/^#\/stats$/, () => location.replace('#/studio/stats'), 'studio'],
  [/^#\/voices\?book=(\d+)$/, ([book]) => location.replace(`#/studio/book/${book}/voices`), 'studio'],
  [/^#\/voices$/, () => location.replace('#/studio/voices'), 'studio'],
  [/^#\/settings$/, () => settingsScreen.render(view), 'settings'],
];

// ---------- импорт ----------

/** Папка, в которую кладём новые книги: открытая в библиотеке, иначе корень. */
function currentFolder() {
  const match = location.hash.match(/^#\/library\?folder=(\d+)$/);
  return match ? Number(match[1]) : null;
}

async function importItems(items, folderId = currentFolder()) {
  let last = null;
  for (const item of items) {
    const name = item.path ? item.path.split(/[\\/]/).pop() : item.file.name;
    toast(`Добавляю «${name}»…`);
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

const requestImport = guard(async (folderId = currentFolder()) => {
  const items = await pickBookFiles(extensions);
  if (items.length) await importItems(items, folderId ?? null);
});

$('#import').onclick = () => requestImport();
window.addEventListener('import-request', (event) => requestImport(event.detail ?? currentFolder()));

// ---------- перетаскивание файлов в окно ----------

let overlay = null;
let dragDepth = 0;
// Тащат что-то изнутри окна (картинку, ссылку) — это не файл книги снаружи:
// Chromium помечает перетаскиваемую картинку как «Files», и без этой отметки
// поверх окна вылезало «Отпустите, чтобы добавить».
let draggingInside = false;
document.addEventListener('dragstart', () => { draggingInside = true; });
document.addEventListener('dragend', () => { draggingInside = false; });
const hasFiles = (event) => !draggingInside && [...(event.dataTransfer?.types || [])].includes('Files');

window.addEventListener('dragenter', (event) => {
  if (!hasFiles(event)) return;
  dragDepth += 1;
  if (overlay) return;
  const count = event.dataTransfer.items?.length || 0;
  overlay = el('div', { class: 'drop-overlay' },
    el('div', { class: 'drop-card' },
      el('span', { class: 'drop-icon' }, icon('book-plus')),
      el('b', {}, count > 1 ? `Отпустите, чтобы добавить ${plural(count, 'книгу', 'книги', 'книг')}` : 'Отпустите, чтобы добавить'),
      el('span', { class: 'hint' }, 'Главы найдём сами. Потом книгу можно будет разобрать по ролям в студии.')));
  document.body.append(overlay);
});
window.addEventListener('dragleave', (event) => {
  if (!hasFiles(event)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) { overlay?.remove(); overlay = null; }
});
window.addEventListener('dragover', (event) => { if (hasFiles(event)) event.preventDefault(); });
window.addEventListener('drop', (event) => {
  dragDepth = 0; overlay?.remove(); overlay = null;
  if (draggingInside) { draggingInside = false; return; }
  if (!hasFiles(event) || event.defaultPrevented) return;
  event.preventDefault();
  importItems([...event.dataTransfer.files].map((file) => ({ file })));
});

// ---------- история переходов: «Назад» и «Вперёд» ----------
// Браузерная история не говорит, есть ли куда идти вперёд, поэтому номер
// шага храним в history.state, а самый дальний — в sessionStorage.

const NAV_KEY = 'booktts-nav-max';
const backButton = $('#nav-back');
const forwardButton = $('#nav-forward');
let navIndex = 0;
let navMax = 0;

function readNavMax() {
  try { return Number(sessionStorage.getItem(NAV_KEY)) || 0; } catch { return 0; }
}
function saveNavMax() {
  try { sessionStorage.setItem(NAV_KEY, String(navMax)); } catch { /* не страшно */ }
}

function trackHistory() {
  const known = history.state?.navIndex;
  if (typeof known === 'number') {
    navIndex = known;  // шаг назад или вперёд по уже пройденному
  } else {
    navIndex += 1;     // новый переход: всё, что было «впереди», теряется
    navMax = navIndex;
    history.replaceState({ navIndex }, '');
    saveNavMax();
  }
  backButton.disabled = navIndex <= 0;
  forwardButton.disabled = navIndex >= navMax;
}

if (typeof history.state?.navIndex === 'number') {
  navIndex = history.state.navIndex;
  navMax = Math.max(readNavMax(), navIndex);
} else {
  history.replaceState({ navIndex: 0 }, '');
  navMax = 0;
  saveNavMax();
}
backButton.disabled = navIndex <= 0;
forwardButton.disabled = navIndex >= navMax;

const goBack = () => { if (navIndex > 0) history.back(); };
const goForward = () => { if (navIndex < navMax) history.forward(); };
backButton.onclick = goBack;
forwardButton.onclick = goForward;

// Боковые кнопки мыши — как в браузере.
window.addEventListener('mouseup', (event) => {
  if (event.button === 3) { event.preventDefault(); goBack(); }
  if (event.button === 4) { event.preventDefault(); goForward(); }
});

// ---------- горячие клавиши ----------

document.addEventListener('keydown', (event) => {
  if (document.querySelector('dialog[open]')) return;
  if (event.altKey && event.key === 'ArrowLeft') { event.preventDefault(); goBack(); return; }
  if (event.altKey && event.key === 'ArrowRight') { event.preventDefault(); goForward(); return; }
  if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
  const key = event.key.toLowerCase();
  if (key === 'k' || key === 'л') {
    event.preventDefault();
    if (location.hash.startsWith('#/search')) emit('focus-search');
    else location.hash = '#/search';
  } else if (key === 'o' || key === 'щ') {
    event.preventDefault();
    requestImport();
  }
});

// ---------- маршруты ----------

/** Подвести «пилюлю» под выбранную вкладку; нет выбранной — спрятать. */
const pill = $('.tabs-pill');
function placePill() {
  const active = document.querySelector('#nav a.active');
  pill.style.opacity = active ? '1' : '0';
  if (!active) return;
  pill.style.width = `${active.offsetWidth}px`;
  pill.style.transform = `translateX(${active.offsetLeft}px)`;
}
// Первый раз — без анимации, иначе пилюля «выезжает» из левого края при запуске.
// Ширина вкладок зависит от шрифта, поэтому пересчитываем, когда он загрузится.
document.fonts?.ready.then(() => { placePill(); requestAnimationFrame(() => pill.classList.add('ready')); });
// Счётчик задач у «Студии» расширяет вкладку — пилюля должна подрасти вместе с ней.
new ResizeObserver(placePill).observe($('.tabs-track'));


let cleanup = null;
let enterTimer = null;
const ENTER_MS = 700;

function route() {
  if (typeof cleanup === 'function') cleanup();
  cleanup = null;
  const hash = location.hash || '#/';
  for (const [pattern, handler, section] of routes) {
    const match = hash.match(pattern);
    if (!match) continue;
    view.scrollTop = 0;
    // Появление элементов — только при переходе. Экраны перерисовываются и
    // по ходу работы (задачи, плеер), и повторная анимация выглядит миганием.
    view.classList.add('entering');
    clearTimeout(enterTimer);
    enterTimer = setTimeout(() => view.classList.remove('entering'), ENTER_MS);
    // На главной свой большой плеер, нижний там лишний.
    document.body.classList.toggle('on-listen', section === 'listen');
    cleanup = handler(match.slice(1));
    for (const link of document.querySelectorAll('#nav a')) {
      link.classList.toggle('active', link.dataset.route === section);
      if (link.dataset.route === section) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    placePill();
    const settings = $('#settings-link');
    if (section === 'settings') settings.setAttribute('aria-current', 'page');
    else settings.removeAttribute('aria-current');
    return;
  }
  location.hash = '#/';
}

window.addEventListener('hashchange', () => { trackHistory(); route(); });

// ---------- наблюдатель за задачами ----------
// Живёт поверх экранов: уведомление о готовой книге придёт, даже если открыт
// другой экран. Пока задач нет, опрос редкий.

const JOBS_IDLE_MS = 5000;
const JOBS_BUSY_MS = 1500;
let jobsTimer = null;

let polling = false;

async function watchJobs() {
  if (polling) return;
  polling = true;
  let delay = JOBS_IDLE_MS;
  try {
    const data = await get('/api/jobs?active=true');
    setActiveJobs(data.jobs);
    const badge = $('#jobs-badge');
    if (badge.textContent !== String(data.active || '')) {
      badge.hidden = !data.active;
      badge.textContent = data.active || '';
      placePill();
    }
    if (data.active) delay = JOBS_BUSY_MS;

    const finished = data.announce || [];
    for (const job of finished) {
      announce(job, finished.length);
      await post(`/api/jobs/${job.id}/announced`);
    }
    if (finished.length > 2 && !windowActive()) {
      notify('BookTTS', `Завершено задач: ${finished.length}. Подробности — в «Студии», раздел «Задачи».`, '#/studio/jobs');
    }
    if (finished.length) {
      emit('library-changed');
      emit('jobs-changed');
    }
  } catch {
    delay = JOBS_IDLE_MS;  // бэкенд перезапускается — попробуем позже
  }
  polling = false;
  clearTimeout(jobsTimer);
  jobsTimer = setTimeout(watchJobs, delay);
}

// Поставили задачу — узнать об этом сразу, а не через пять секунд:
// кнопки «Озвучить» по всему приложению сменятся на «В очереди».
window.addEventListener('jobs-changed', () => {
  clearTimeout(jobsTimer);
  jobsTimer = setTimeout(watchJobs, 50);
});

const JOB_DONE = {
  synthesis: 'Глава озвучена',
  book: 'Книга озвучена',
  folder: 'Папка озвучена',
  markup: 'Глава размечена',
  markup_book: 'Книга размечена по ролям',
  export: 'Экспорт готов',
  qwen_move: 'Qwen3-TTS перенесён',
  previews: 'Образцы голосов готовы',
};

const windowActive = () => document.hasFocus() && !document.hidden;

/**
 * Сообщить о завершённой задаче. В окне — карточка с кнопкой «Открыть» (или
 * «Разметить», если ошибка лечится разметкой). Окно свёрнуто или в фоне —
 * ещё и уведомление Windows от имени BookTTS: нажатие откроет тот же экран.
 */
function announce(job, batch) {
  const ok = job.status === 'done';
  const fix = ok ? null : jobFix(job);
  const target = fix ? fix[1] : jobRoute(job);
  const heading = ok ? JOB_DONE[job.kind] || 'Готово' : 'Не получилось';
  const text = ok ? job.title : `${job.title}: ${humanError(job.error || 'ошибка')}`;
  toast(text, ok ? 'ok' : 'error', {
    title: heading,
    action: target ? [fix ? fix[0] : 'Открыть', () => { location.hash = target; }] : null,
  });
  if (batch <= 2 && !windowActive()) notify(heading, text, target);
}

// Нажатие на уведомление Windows: оболочка подняла окно, осталось открыть экран.
tauri?.event?.listen?.('notification-open', (event) => {
  if (typeof event.payload === 'string' && event.payload.startsWith('#/')) location.hash = event.payload;
});

// ---------- обновления ----------
// Проверяет оболочка Tauri: она скачивает latest.json из GitHub Releases и
// сверяет подпись. В браузере (режим разработки) обновлять нечего.

const UPDATE_DELAY_MS = 5000;

/** Скачать и поставить обновление, сообщая о прогрессе; оболочка потом перезапустит приложение. */
async function installUpdate(onProgress) {
  const unlisten = await tauri.event.listen('update-progress', (event) => {
    const { downloaded, total } = event.payload;
    onProgress(total
      ? `Скачано ${Math.round((downloaded / total) * 100)}%`
      : `Скачано ${(downloaded / 1048576).toFixed(1).replace('.', ',')} МБ`);
  });
  try {
    await tauri.core.invoke('install_update');
    onProgress('Перезапуск…');
  } finally {
    unlisten();
  }
}

/** Плашка в шапке «Обновить до X» — открывает окно с описанием изменений. */
function showUpdate(update) {
  const pill = $('#update');
  pill.replaceChildren(icon('download'), `Обновить до ${update.version}`);
  pill.title = `Доступна версия ${update.version}, у вас ${update.current}. Нажмите, чтобы узнать, что нового.`;
  pill.hidden = false;
  pill.onclick = () => updateDialog(update, { install: installUpdate });
}

window.addEventListener('whats-new', (event) => showWhatsNew(event.detail || undefined));

async function checkForUpdates({ quiet = true } = {}) {
  if (!tauri?.core?.invoke) {
    if (!quiet) toast('Обновления проверяются только в установленном приложении');
    return null;
  }
  try {
    const update = await tauri.core.invoke('check_update');
    if (update) showUpdate(update);
    else if (!quiet) toast('Установлена последняя версия', 'ok');
    return update;
  } catch (error) {
    // Нет сети или релизов ещё не было — при фоновой проверке это не повод шуметь.
    if (!quiet) toast(String(error), 'error');
    return null;
  }
}

// Кнопка в «Настройках» ждёт конца проверки: крутится, пока идёт запрос.
window.addEventListener('check-updates', (event) => {
  checkForUpdates({ quiet: false }).finally(() => event.detail?.onDone?.());
});
setTimeout(() => checkForUpdates(), UPDATE_DELAY_MS);

// ---------- тема ----------

function drawThemeButton() {
  const theme = currentTheme();
  const button = $('#theme');
  button.replaceChildren(icon(themeIcon(theme)));
  button.title = `Тема: ${themeTitle(theme).toLowerCase()} — нажмите, чтобы сменить`;
  button.setAttribute('aria-label', `Тема: ${themeTitle(theme).toLowerCase()}`);
}

$('#theme').onclick = () => { nextTheme(); drawThemeButton(); };
window.addEventListener('theme-changed', drawThemeButton);
applyTheme(currentTheme());
drawThemeButton();

await loadIcons();
// После обновления — один раз «Что нового».
Promise.all([get('/api/config'), get('/api/library')]).then(([config, tree]) => {
  const hasBooks = tree.books.length > 0 || tree.folders.length > 0;
  maybeShowWhatsNew(config.version, { hasBooks });
}).catch(() => {});
playerBar.mount($('#player'));
get('/api/formats').then((data) => { extensions = data.extensions; }).catch(() => {});
watchJobs();
route();
