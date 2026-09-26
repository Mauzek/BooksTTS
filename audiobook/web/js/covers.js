// Обложки книг и знаки ролей.
//
// У книги из txt или fb2 картинки почти никогда нет, поэтому обложку рисуем
// сами. Жанр задаёт фигуру, настроение — цвета: у детектива лупа, у фэнтези
// месяц, мрачная книга тёмная, светлая — светлая. Два настроения — плавный
// переход между их цветами; второй и третий жанр — маленькие знаки над
// названием. Пока жанра и настроения нет, цвет и фигура выбираются по
// названию — у одной и той же книги они всегда одинаковые.

import { el } from './ui.js';

// фон, текст, подпись, фигура 1, фигура 2
const PALETTES = [
  ['var(--cover-dark)', '#f7f5f2', '#b8b1a8', 'var(--accent-deco)', '#f7f5f2'],
  ['#c2410c', '#ffffff', '#fde3d2', '#f5b489', '#1c1b1a'],
  ['var(--surface)', 'var(--ink)', 'var(--muted)', '#c2410c', 'var(--track)'],
  ['#34312e', '#f7f5f2', '#b8b1a8', '#f5b489', '#c2410c'],
  ['var(--cover-sand)', 'var(--ink)', 'var(--ink-2)', '#c2410c', 'var(--surface)'],
  ['#f5b489', '#1c1b1a', '#5a2a0c', '#1c1b1a', '#c2410c'],
];

/** Настроение → цвета обложки. */
export const MOOD_PALETTES = {
  'Светлое': ['#f4e6c8', '#1c1b1a', '#6b5a3e', '#e8590c', '#fffaf0'],
  'Весёлое': ['#ffcf4a', '#1c1b1a', '#5a4300', '#e8590c', '#fff6d6'],
  'Уютное': ['#8a4b2a', '#fff4e6', '#f1c9a5', '#f5b489', '#5e2f18'],
  'Романтичное': ['#f2c1cd', '#3a0f1c', '#7a3b4c', '#d0457a', '#fff0f3'],
  'Спокойное': ['#cfe3d4', '#18261c', '#4a6150', '#5e8f6b', '#f4faf5'],
  'Грустное': ['#5b6b80', '#f1f4f8', '#c9d2de', '#a9bad0', '#3b4757'],
  'Мрачное': ['#161417', '#ece8e3', '#8f8a85', '#8a2a2a', '#3a3436'],
  'Напряжённое': ['#b3261e', '#ffffff', '#ffd5cf', '#1c1b1a', '#ff8a7a'],
  'Загадочное': ['#2c2548', '#f1ecff', '#b8acde', '#7c6bd6', '#e8d9ff'],
  'Эпичное': ['#1e2d3b', '#f7f5f2', '#a9b6c3', '#e8a33d', '#f7f5f2'],
};

const STAR = 'polygon(50% 0%, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%)';
const PEAK = 'polygon(50% 0, 100% 100%, 0 100%)';

// Фигуры задаются в долях ширины обложки (cqi), поэтому одинаково смотрятся
// и на плитке библиотеки, и в крошечной картинке плеера.
const SHAPES = {
  sun: (a, b) => [
    shape({ top: '-22cqi', right: '-22cqi', width: '62cqi', height: '62cqi', background: a }),
    shape({ top: '20cqi', right: '15cqi', width: '16cqi', height: '16cqi', background: b }),
  ],
  ring: (a, b) => [
    shape({ top: '10cqi', left: '10cqi', width: '42cqi', height: '42cqi', border: `6cqi solid ${a}`, boxSizing: 'border-box' }),
    shape({ top: '38cqi', left: '42cqi', width: '10cqi', height: '10cqi', background: b }),
  ],
  bands: (a, b) => [
    shape({ top: '12cqi', left: '0', right: '0', height: '8cqi', background: a, borderRadius: '0' }),
    shape({ top: '24cqi', left: '0', width: '60%', height: '8cqi', background: b, borderRadius: '0' }),
  ],
  // ---- по жанрам ----
  moon: (a, b) => [
    shape({ top: '8cqi', right: '12cqi', width: '44cqi', height: '44cqi', background: a }),
    shape({ top: '1cqi', right: '3cqi', width: '40cqi', height: '40cqi', background: 'var(--cv-cut, var(--cv-bg))' }),
    star({ top: '14cqi', left: '16cqi', size: 9, background: b }),
    star({ top: '36cqi', left: '30cqi', size: 5, background: b }),
  ],
  planet: (a, b) => [
    shape({ top: '12cqi', left: '28cqi', width: '44cqi', height: '44cqi', background: a }),
    shape({ top: '28cqi', left: '10cqi', width: '80cqi', height: '14cqi', border: `2.5cqi solid ${b}`, boxSizing: 'border-box', transform: 'rotate(-16deg)' }),
    shape({ top: '8cqi', left: '12cqi', width: '4cqi', height: '4cqi', background: b }),
  ],
  ripple: (a, b) => [
    shape({ top: '6cqi', left: '10cqi', width: '80cqi', height: '80cqi', border: `1.2cqi solid ${a}`, boxSizing: 'border-box', opacity: '.45' }),
    shape({ top: '16cqi', left: '20cqi', width: '60cqi', height: '60cqi', border: `1.6cqi solid ${a}`, boxSizing: 'border-box', opacity: '.7' }),
    shape({ top: '26cqi', left: '30cqi', width: '40cqi', height: '40cqi', border: `2cqi solid ${a}`, boxSizing: 'border-box' }),
    shape({ top: '41cqi', left: '45cqi', width: '10cqi', height: '10cqi', background: b }),
  ],
  portal: (a, b) => [
    shape({ top: '4cqi', left: '18cqi', width: '64cqi', height: '64cqi', border: `3cqi solid ${a}`, boxSizing: 'border-box' }),
    shape({ top: '14cqi', left: '28cqi', width: '44cqi', height: '44cqi', border: `3cqi solid ${a}`, boxSizing: 'border-box', opacity: '.7' }),
    shape({ top: '24cqi', left: '38cqi', width: '24cqi', height: '24cqi', background: b }),
  ],
  lens: (a, b) => [
    shape({ top: '8cqi', left: '14cqi', width: '44cqi', height: '44cqi', border: `6cqi solid ${a}`, boxSizing: 'border-box' }),
    shape({ top: '48cqi', left: '50cqi', width: '26cqi', height: '8cqi', background: b, borderRadius: '4cqi', transform: 'rotate(45deg)', transformOrigin: 'left center' }),
  ],
  slash: (a, b) => [
    shape({ top: '18cqi', left: '-20cqi', width: '140cqi', height: '16cqi', background: a, borderRadius: '0', transform: 'rotate(-24deg)' }),
    shape({ top: '40cqi', left: '-20cqi', width: '140cqi', height: '3cqi', background: b, borderRadius: '0', transform: 'rotate(-24deg)' }),
  ],
  drips: (a) => [
    shape({ top: '0', left: '0', right: '0', height: '14cqi', background: a, borderRadius: '0' }),
    shape({ top: '10cqi', left: '14cqi', width: '8cqi', height: '26cqi', background: a, borderRadius: '0 0 4cqi 4cqi' }),
    shape({ top: '10cqi', left: '42cqi', width: '7cqi', height: '16cqi', background: a, borderRadius: '0 0 4cqi 4cqi' }),
    shape({ top: '10cqi', left: '68cqi', width: '9cqi', height: '36cqi', background: a, borderRadius: '0 0 5cqi 5cqi' }),
  ],
  phases: (a) => [
    shape({ top: '16cqi', left: '10cqi', width: '22cqi', height: '22cqi', background: a }),
    shape({ top: '16cqi', left: '39cqi', width: '22cqi', height: '22cqi', background: `linear-gradient(90deg, ${a} 50%, transparent 50%)`, border: `1.5cqi solid ${a}`, boxSizing: 'border-box' }),
    shape({ top: '16cqi', left: '68cqi', width: '22cqi', height: '22cqi', border: `1.5cqi solid ${a}`, boxSizing: 'border-box' }),
  ],
  peaks: (a, b) => [
    shape({ top: '12cqi', left: '4cqi', width: '64cqi', height: '46cqi', background: a, clipPath: PEAK, borderRadius: '0' }),
    shape({ top: '26cqi', left: '46cqi', width: '48cqi', height: '32cqi', background: b, clipPath: PEAK, borderRadius: '0' }),
    shape({ top: '6cqi', right: '10cqi', width: '10cqi', height: '10cqi', background: b }),
  ],
  pair: (a, b) => [
    shape({ top: '10cqi', left: '14cqi', width: '40cqi', height: '40cqi', background: a }),
    shape({ top: '10cqi', left: '40cqi', width: '40cqi', height: '40cqi', background: b, opacity: '.85' }),
  ],
  arch: (a, b) => [
    shape({ top: '6cqi', left: '26cqi', width: '48cqi', height: '56cqi', background: a, borderRadius: '24cqi 24cqi 0 0' }),
    shape({ top: '18cqi', left: '38cqi', width: '24cqi', height: '44cqi', background: 'var(--cv-cut, var(--cv-bg))', borderRadius: '12cqi 12cqi 0 0' }),
    shape({ top: '60cqi', left: '16cqi', width: '68cqi', height: '3cqi', background: b, borderRadius: '0' }),
  ],
  half: (a, b) => [
    shape({ top: '8cqi', left: '18cqi', width: '64cqi', height: '32cqi', background: a, borderRadius: '32cqi 32cqi 0 0' }),
    shape({ top: '42cqi', left: '30cqi', width: '40cqi', height: '20cqi', background: b, borderRadius: '0 0 20cqi 20cqi' }),
  ],
  confetti: (a, b) => [
    shape({ top: '8cqi', left: '12cqi', width: '12cqi', height: '12cqi', background: a }),
    shape({ top: '14cqi', left: '58cqi', width: '16cqi', height: '6cqi', background: b, borderRadius: '3cqi', transform: 'rotate(30deg)' }),
    shape({ top: '30cqi', left: '32cqi', width: '9cqi', height: '9cqi', background: b }),
    star({ top: '36cqi', left: '72cqi', size: 12, background: a }),
    shape({ top: '46cqi', left: '10cqi', width: '16cqi', height: '6cqi', background: a, borderRadius: '3cqi', transform: 'rotate(-25deg)' }),
  ],
  stars: (a, b) => [
    star({ top: '6cqi', left: '22cqi', size: 44, background: a }),
    star({ top: '8cqi', left: '78cqi', size: 12, background: b }),
    star({ top: '46cqi', left: '12cqi', size: 9, background: b }),
  ],
  frame: (a, b) => [
    shape({ top: '8cqi', left: '10cqi', right: '10cqi', height: '58cqi', border: `2cqi solid ${a}`, boxSizing: 'border-box', borderRadius: '2cqi' }),
    shape({ top: '14cqi', left: '16cqi', right: '16cqi', height: '46cqi', border: `0.8cqi solid ${a}`, boxSizing: 'border-box', borderRadius: '1cqi' }),
    shape({ top: '30cqi', left: '44cqi', width: '12cqi', height: '12cqi', background: b, borderRadius: '1cqi', transform: 'rotate(45deg)' }),
  ],
  bubbles: (a, b) => [
    shape({ top: '8cqi', left: '12cqi', width: '30cqi', height: '30cqi', background: a }),
    shape({ top: '22cqi', left: '48cqi', width: '20cqi', height: '20cqi', background: b }),
    shape({ top: '6cqi', left: '70cqi', width: '12cqi', height: '12cqi', background: a, opacity: '.7' }),
    shape({ top: '44cqi', left: '30cqi', width: '10cqi', height: '10cqi', background: b }),
  ],
  bars: (a, b) => [
    shape({ top: '30cqi', left: '14cqi', width: '12cqi', height: '30cqi', background: a, borderRadius: '2cqi 2cqi 0 0' }),
    shape({ top: '14cqi', left: '32cqi', width: '12cqi', height: '46cqi', background: b, borderRadius: '2cqi 2cqi 0 0' }),
    shape({ top: '38cqi', left: '50cqi', width: '12cqi', height: '22cqi', background: a, borderRadius: '2cqi 2cqi 0 0' }),
    shape({ top: '22cqi', left: '68cqi', width: '12cqi', height: '38cqi', background: b, borderRadius: '2cqi 2cqi 0 0' }),
  ],
};
const PLAIN_SHAPES = ['sun', 'ring', 'bands'];

/** Жанр → фигура. */
export const GENRE_SHAPES = {
  'Фэнтези': 'moon', 'Фантастика': 'planet', 'Детектив': 'lens', 'Триллер': 'slash',
  'Ужасы': 'drips', 'Мистика': 'phases', 'Приключения': 'peaks', 'Любовный роман': 'pair',
  'Исторический': 'arch', 'Драма': 'half', 'Юмор': 'confetti', 'Сказка': 'stars',
  'Классика': 'frame', 'Поэзия': 'ripple', 'Детское': 'bubbles', 'Нон-фикшн': 'bars',
};

function shape(style) {
  return el('span', { class: 'shape', style });
}

function star({ top, left, size, background }) {
  return shape({ top, left, width: `${size}cqi`, height: `${size}cqi`, background, clipPath: STAR, borderRadius: '0' });
}

function hash(text) {
  let h = 2166136261;
  for (const ch of String(text)) {
    h ^= ch.codePointAt(0);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

// Что известно о книгах: плеер и поиск знают только id и название, а жанр,
// настроение и картинку берём отсюда — обложка везде одна и та же.
const known = new Map();
const FIELDS = ['genres', 'moods', 'cover_path'];

function pick(book) {
  const out = {};
  for (const key of FIELDS) if (key in book && book[key] !== undefined) out[key] = book[key];
  return out;
}

/** Запомнить книги из ответа библиотеки. */
export function learnBooks(books) {
  for (const book of books || []) {
    if (book?.id !== undefined) known.set(book.id, { ...known.get(book.id), ...pick(book) });
  }
}

function look(book) {
  const h = hash(book.title || book.book_title || '');
  const moods = (book.moods || []).filter((name) => MOOD_PALETTES[name]);
  const genres = (book.genres || []).filter((name) => GENRE_SHAPES[name]);
  const top = moods.length ? MOOD_PALETTES[moods[0]] : PALETTES[h % PALETTES.length];
  // Второе настроение — низ обложки: там название, поэтому и цвет текста его.
  const bottom = moods.length > 1 ? MOOD_PALETTES[moods[1]] : top;
  const [bg, , , a, b] = top;
  const [, ink, sub] = bottom;
  return {
    bg: bottom === top ? bg : `linear-gradient(165deg, ${bg} 0%, ${bg} 30%, ${bottom[0]} 100%)`,
    cut: bg, ink, sub, a, b,
    shape: genres.length ? GENRE_SHAPES[genres[0]] : PLAIN_SHAPES[Math.floor(h / PALETTES.length) % PLAIN_SHAPES.length],
    marks: genres.slice(1, 3).map((name) => GENRE_SHAPES[name]),
  };
}

function paint(node, style) {
  node.style.setProperty('--cv-cut', style.cut || style.bg);
  node.style.setProperty('--cv-bg', style.bg);
  node.style.setProperty('--cv-ink', style.ink);
  node.style.setProperty('--cv-sub', style.sub);
  if (style.longest) node.style.setProperty('--longest', String(style.longest));
  return node;
}

/** Обложка для плиток и карточек: картинка из книги или нарисованная. */
export function cover(book, { className = '', text = true, remember = true } = {}) {
  const id = book.id ?? book.book_id;
  const own = pick(book);
  // Предпросмотр несохранённых жанров не должен перекрашивать обложку в других местах.
  if (remember && Object.keys(own).length) known.set(id, { ...known.get(id), ...own });
  const info = { ...known.get(id), ...own };
  const picture = info.cover_path || (book.has_cover && !('cover_path' in info));
  if (picture) {
    // Адрес меняется вместе с файлом — иначе окно покажет старую картинку из кеша.
    const version = info.cover_path ? `?v=${hash(info.cover_path)}` : '';
    return el('img', { class: `cover-img ${className}`.trim(), src: `/api/books/${id}/cover${version}`, alt: '', loading: 'lazy' });
  }
  const style = look({ ...book, ...info });
  const name = book.title || book.book_title || '';
  // Самое длинное слово задаёт кегль: «Реинкарнация» не рвётся посередине.
  style.longest = Math.max(4, ...name.split(/\s+/).map((word) => word.length));
  return paint(el('div', { class: `cover-gen ${className}`.trim(), 'aria-hidden': 'true' },
    SHAPES[style.shape](style.a, style.b),
    // Знаки остальных жанров — уменьшенные фигуры в своих квадратиках.
    text && style.marks.length ? el('span', { class: 'cover-marks' }, style.marks.map((mark) =>
      el('span', { class: 'cover-mark' }, SHAPES[mark](style.a, style.b)))) : null,
    text ? el('span', { class: 'author' }, book.author || book.book_author || '') : null,
    text ? el('span', { class: 'name' }, book.title || book.book_title || '') : null), style);
}

// ---------- роли ----------

export const NARRATOR = 'narrator';

export const speakerName = (name) => (name === NARRATOR ? 'Рассказчик' : name);

function tint(node, slot) {
  const n = Number(slot) || 0;
  node.style.setProperty('--av', `var(--c-${n})`);
  node.style.setProperty('--av-on', `var(--c-${n}-on)`);
  return node;
}

/** Кружок с первой буквой роли в её цвете. */
export function avatar(name, slot, size = '') {
  const letter = speakerName(name).trim().charAt(0).toUpperCase() || '?';
  return tint(el('span', { class: `av ${size}`.trim(), 'aria-hidden': 'true' }, letter), slot);
}

/** Точка цвета роли — рядом с именем, никогда вместо него. */
export function roleDot(slot) {
  return tint(el('span', { class: 'dot', 'aria-hidden': 'true' }), slot);
}

/** Раскрасить элемент в цвет роли: --av и --av-on. */
export function roleTint(node, slot) {
  return tint(node, slot);
}

export const ROLE_COLORS = 7;  // 0 — цвет рассказчика, 1–6 — персонажей

/** Палитра цветов роли у кнопки. Вернёт выбранный номер или null. */
export function pickRoleColor(anchor, current) {
  return new Promise((resolve) => {
    document.querySelector('.color-pop')?.remove();
    const done = (value) => {
      pop.remove();
      document.removeEventListener('mousedown', outside, true);
      document.removeEventListener('keydown', escape, true);
      resolve(value);
    };
    const pop = el('div', { class: 'menu-pop color-pop', role: 'listbox', 'aria-label': 'Цвет роли' },
      Array.from({ length: ROLE_COLORS }, (_, slot) => tint(el('button', {
        class: `color-swatch${slot === current ? ' current' : ''}`, role: 'option',
        'aria-selected': String(slot === current), 'aria-label': slot ? `Цвет ${slot}` : 'Цвет рассказчика',
        title: slot ? '' : 'Как у рассказчика', onclick: () => done(slot),
      }), slot)));
    const outside = (event) => { if (!pop.contains(event.target)) done(null); };
    const escape = (event) => { if (event.key === 'Escape') { event.preventDefault(); done(null); } };
    document.body.append(pop);
    const rect = anchor.getBoundingClientRect();
    pop.style.top = `${Math.min(innerHeight - pop.offsetHeight - 8, rect.bottom + 6)}px`;
    pop.style.left = `${Math.max(8, Math.min(innerWidth - pop.offsetWidth - 8, rect.left))}px`;
    pop.querySelector('.current, button')?.focus();
    setTimeout(() => {
      document.addEventListener('mousedown', outside, true);
      document.addEventListener('keydown', escape, true);
    }, 0);
  });
}
