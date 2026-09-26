// Общее для экранов студии: вкладки разделов, шаги книги, задачи.

import { post } from '../../api.js';
import { el, emit, humanError, icon, toast } from '../../ui.js';

export { humanError };

const SECTIONS = [
  ['overview', 'Обзор', '#/studio'],
  ['jobs', 'Задачи', '#/studio/jobs'],
  ['voices', 'Голоса', '#/studio/voices'],
  ['pronunciation', 'Произношение', '#/studio/pronunciation'],
  ['stats', 'Статистика', '#/studio/stats'],
];

export const HINT_KEY = 'booktts-studio-hint-hidden';

let shell = null;

function buildShell() {
  const links = new Map(SECTIONS.map(([key, label, href]) => [key, el('a', { href, dataset: { section: key } },
    el('span', { class: 'fixed-bold', dataset: { text: label } }, label))]));
  const badge = el('span', { class: 'badge', hidden: true });
  links.get('jobs').append(badge);
  const pill = el('span', { class: 'studio-pill', 'aria-hidden': 'true' });
  const track = el('div', { class: 'studio-track' }, pill, ...links.values());
  const help = el('button', {
    class: 'round surface studio-help', 'aria-label': 'Как сделать аудиокнигу', title: 'Как сделать аудиокнигу',
    // Подсказка живёт на «Обзоре» — и кнопка только там.
    onclick: () => {
      try { localStorage.removeItem(HINT_KEY); } catch { /* удобство */ }
      emit('studio-hint');
    },
  }, icon('circle-help'));
  const nav = el('nav', { class: 'studio-nav', 'aria-label': 'Разделы студии' }, track, el('span', { class: 'grow' }), help);
  const content = el('div', { class: 'studio-content' });
  const root = el('div', { class: 'page studio-shell' }, nav, content);

  let activeKey = null;
  const place = () => {
    const link = links.get(activeKey);
    if (!link || !link.offsetWidth) return;
    pill.style.width = `${link.offsetWidth}px`;
    pill.style.transform = `translateX(${link.offsetLeft}px)`;
    pill.style.opacity = '1';
  };
  new ResizeObserver(place).observe(track);
  return {
    root, content,
    setActive(key) {
      activeKey = key;
      help.hidden = key !== 'overview';
      for (const [name, link] of links) {
        link.classList.toggle('active', name === key);
        if (name === key) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      }
      place();
    },
    setJobs(count) {
      const text = count ? String(count) : '';
      if (badge.textContent === text) return;
      badge.textContent = text;
      badge.hidden = !count;
      place();
    },
    // Первый раз пилюля встаёт без анимации, дальше — переезжает.
    arm() { requestAnimationFrame(() => { place(); requestAnimationFrame(() => pill.classList.add('ready')); }); },
  };
}

/**
 * Каркас студии: вкладки разделов живут между переходами, меняется только
 * содержимое под ними. Раньше вкладки рисовались заново вместе со страницей
 * и «прыгали» анимацией появления.
 *
 * Возвращает элемент, в который экран рисует своё содержимое. Прежнее
 * содержимое остаётся на месте, пока новое не нарисуется, и плавно уходит —
 * высота страницы не проваливается на время загрузки.
 */
export function studioFrame(view, active) {
  const fresh = !shell || shell.root.parentNode !== view;
  if (fresh) {
    shell = buildShell();
    view.replaceChildren(shell.root);
    shell.arm();
  }
  shell.setActive(active);
  const body = el('div', { class: 'studio-body' });
  const previous = [...shell.content.children];
  shell.content.append(body);
  if (previous.length) {
    for (const node of previous) node.classList.add('leaving');
    const done = () => { previous.forEach((node) => node.remove()); watcher.disconnect(); };
    const watcher = new MutationObserver(() => { if (body.childElementCount) done(); });
    watcher.observe(body, { childList: true });
    setTimeout(done, 1500);
  }
  return body;
}

/** Счётчик задач в работе у вкладки «Задачи». */
export function setStudioJobs(count) {
  shell?.setJobs(count || 0);
}

export const STEPS = ['Текст', 'Роли', 'Голоса', 'Озвучка', 'Готово'];
const STEP_ROUTES = ['text', 'roles', 'voices', 'voice', 'done'];

/**
 * Где книга на пути к аудиокниге. Для каждого шага — состояние:
 * done — сделан, part — начат, now — следующее действие, todo — впереди.
 */
export function bookSteps(book) {
  const chapters = book.chapters || 0;
  const roles = chapters && book.marked >= chapters ? 'done' : book.marked ? 'part' : 'todo';
  const voices = !book.marked ? 'todo' : book.missing_voice || book.unavailable_voice ? 'part' : 'done';
  const audio = chapters && book.voiced >= chapters ? 'done' : book.voiced ? 'part' : 'todo';
  const ready = audio === 'done' ? 'done' : 'todo';
  const states = [chapters ? 'done' : 'todo', roles, voices, audio, ready];
  // Следующее действие — первый несделанный шаг; голоса без разметки не подобрать.
  const next = states.findIndex((state, index) => index > 0 && state !== 'done');
  if (next > 0) states[next] = 'now';
  return { states, next };
}

export function stepNote(index, book) {
  if (index === 1) return book.marked ? `${book.marked}/${book.chapters}` : '';
  if (index === 3) return book.voiced ? `${book.voiced}/${book.chapters}` : '';
  return '';
}

/** Ряд шагов книги: сделанное с галочкой, текущий — акцентом. */
export function stepPills(book) {
  const { states } = bookSteps(book);
  return el('div', { class: 'step-pills' }, STEPS.map((name, index) => {
    const state = states[index];
    const note = stepNote(index, book);
    // Шаг — ссылка на свой экран в рабочем месте книги.
    return el('a', { class: `step-pill ${state}`, href: `#/studio/book/${book.id}/${STEP_ROUTES[index]}` },
      state === 'done' ? icon('check', { size: 12 }) : null,
      note ? `${name} ${note}` : name);
  }));
}

/** Степпер на экранах книги в студии: шаги кликабельны. */
export function stepper(bookId, current, links = {}) {
  const items = [];
  STEPS.forEach((name, index) => {
    const n = index + 1;
    const state = n < current ? 'done' : n === current ? 'now' : 'todo';
    items.push(el('a', {
      class: `stepper-step ${state}`, href: links[n] || '#', 'aria-current': state === 'now' ? 'step' : null,
    }, el('span', { class: 'stepper-dot' }, state === 'done' ? icon('check', { size: 14 }) : String(n)), name));
    if (n < STEPS.length) items.push(el('span', { class: `stepper-line${n < current ? ' done' : ''}`, 'aria-hidden': 'true' }));
  });
  return el('nav', { class: 'stepper', 'aria-label': 'Шаги' }, items);
}

export const JOB_STATUS = {
  pending: 'в очереди',
  running: 'выполняется',
  cancelling: 'останавливается',
  done: 'готово',
  failed: 'ошибка',
  cancelled: 'отменена',
};
export const ACTIVE = new Set(['pending', 'running', 'cancelling']);

/** Запустить задачу заново — тем же способом, каким её поставили. */
export async function retryJob(job) {
  const target = job.target_id;
  const routes = {
    synthesis: [`/api/chapters/${target}/synthesize`, {}],
    book: [`/api/books/${target}/synthesize`, {}],
    folder: [`/api/folders/${target}/synthesize`, {}],
    markup: [`/api/chapters/${target}/markup-job`, {}],
    markup_book: [`/api/books/${target}/markup`, {}],
    export: [`/api/books/${target}/export`, { kind: job.params?.kind || 'm4b' }],
    qwen_move: ['/api/engines/qwen/move', { target: job.params?.target }],
  };
  const route = routes[job.kind];
  if (!route) throw new Error('эту задачу нельзя повторить отсюда');
  const created = await post(...route);
  toast(`Задача снова в очереди: ${created.title}`, 'ok');
  emit('jobs-changed');
  return created;
}

/** Экран, к которому относится задача: туда ведёт нажатие на неё. */
export function jobRoute(job) {
  const target = job.target_id;
  if (job.kind === 'qwen_move') return '#/settings';
  if (job.kind === 'previews') return '#/studio/voices';
  if (target === null || target === undefined) return null;
  switch (job.kind) {
    case 'synthesis': return job.status === 'failed' && /не размечен/.test(job.error || '') ? `#/chapter/${target}/edit` : `#/chapter/${target}`;
    case 'markup': return `#/chapter/${target}/edit`;
    case 'markup_book': return `#/studio/book/${target}/roles`;
    case 'book':
    case 'export': return `#/book/${target}`;
    case 'folder': return `#/library?folder=${target}`;
    case 'qwen_move': return '#/settings';
    default: return null;
  }
}

/**
 * Как исправить упавшую задачу. Повтор не всегда помогает: неразмеченную
 * главу нужно сначала разметить, без ключа — открыть настройки.
 */
export function jobFix(job) {
  const error = job.error || '';
  if (/не размечен/.test(error) && job.kind === 'synthesis') return ['Разметить', `#/chapter/${job.target_id}/edit`];
  if (/нет голоса|без голоса|голос не назначен/.test(error) && job.book_id) return ['Выбрать голоса', `#/studio/book/${job.book_id}/voices`];
  if (/ключ|Error code: 40[13]|Настройк/i.test(error)) return ['Настройки', '#/settings'];
  return null;
}

/** Иконка задачи по её виду. */
export function jobIcon(job) {
  if (job.kind.startsWith('markup')) return 'wand-sparkles';
  if (job.kind === 'export') return 'download';
  if (job.kind === 'qwen_move') return 'folder';
  if (job.kind === 'previews') return 'volume-2';
  return 'mic';
}

/** Короткое имя движка: «Qwen3-TTS (видеокарта)» → «Qwen3-TTS». */
export const shortEngine = (engine) => String(engine?.title || engine?.name || engine || '').replace(/\s*\(.*\)$/, '');

