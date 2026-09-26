// Главная: приветствие, большой плеер того, что играет (или что слушали
// последним), и полки — «Продолжить слушать» и «Недавно добавленные».
// Пустая библиотека — экран первого запуска.

import { get } from '../api.js';
import { avatar, cover, learnBooks, roleTint, speakerName } from '../covers.js';
import * as player from '../player.js';
import { el, emit, formatDuration, formatSpan, guard, icon, plural, popupMenu, toast } from '../ui.js';

const RATES = [0.75, 1, 1.25, 1.5, 1.75, 2];
const SLEEP = [[15, '15 минут'], [30, '30 минут'], [60, '1 час'], ['chapter', 'До конца главы'], [0, 'Выключить']];
const SHELF = 6;
const MAX_ROLES = 5;

function flatten(tree) {
  const books = [...tree.books];
  const walk = (folders) => folders.forEach((folder) => { books.push(...folder.books); walk(folder.folders); });
  walk(tree.folders);
  return books;
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 5) return 'Доброй ночи';
  if (hour < 12) return 'Доброе утро';
  if (hour < 18) return 'Добрый день';
  return 'Добрый вечер';
}

function shelfTile(book, listening) {
  const item = listening.get(book.id);
  const inProgress = item && item.progress > 0 && item.progress < 1;
  const caption = inProgress
    ? `${item.chapter_label || 'слушаю'} · ${Math.round(item.progress * 100)}%`
    : book.chapters && book.voiced_chapters >= book.chapters
      ? formatSpan(book.duration_ms)
      : book.voiced_chapters ? `${book.voiced_chapters} из ${book.chapters} глав` : 'не озвучена';
  return el('a', { class: 'btile', href: `#/book/${book.id}` },
    el('span', { class: 'btile-cover' }, cover(book)),
    el('span', { class: 'btile-caption' },
      el('span', { class: 'btile-title' }, book.title),
      el('span', { class: 'btile-sub' }, caption),
      inProgress ? el('span', { class: 'progress wide' }, el('i', { style: { width: `${item.progress * 100}%` } })) : null));
}

function firstRun() {
  const art = el('div', { class: 'first-art', 'aria-hidden': 'true' },
    el('span', { style: { top: '-60px', right: '-60px', width: '190px', height: '190px', borderRadius: '50%', background: 'var(--accent-deco)' } }),
    el('span', { style: { top: '44px', right: '40px', width: '44px', height: '44px', borderRadius: '50%', background: 'var(--hero-ink)' } }),
    el('span', { style: { left: '28px', bottom: '48px', width: '110px', height: '10px', borderRadius: '5px', background: 'var(--hero-2)' } }),
    el('span', { style: { left: '28px', bottom: '28px', width: '72px', height: '10px', borderRadius: '5px', background: 'var(--hero-2)' } }));
  const step = (n, title, text) => el('div', { class: 'step' },
    el('span', { class: 'n' }, n), el('span', {}, el('b', {}, title), el('span', { class: 'hint' }, text)));
  return el('div', { class: 'first-run' },
    art,
    el('h1', {}, 'Здесь будут ваши аудиокниги'),
    el('p', {}, 'Добавьте книгу — BookTTS разберёт, кто что говорит, и прочитает разными голосами.'),
    el('button', { class: 'primary xl', onclick: () => emit('import-request', null) }, icon('plus', { className: 'big' }), 'Добавить книгу'),
    el('span', { class: 'hint' }, 'или перетащите файл в это окно'),
    el('div', { class: 'steps' },
      step('1', 'Добавьте книгу', 'txt, fb2, epub, docx или pdf — главы найдутся сами'),
      step('2', 'Разберите по ролям', 'Claude отметит, кто что говорит; нужен ключ API'),
      step('3', 'Слушайте', 'каждый персонаж звучит своим голосом')));
}

/** Кегль реплики по её длине: длинная не раздувает карточку. */
function lineSize(text) {
  const length = (text || '').length;
  if (length > 420) return 'small';
  if (length > 220) return 'medium';
  return '';
}

/**
 * Большая карточка плеера: обложка во всю высоту, реплика в поле постоянной
 * высоты (карточка не прыгает от длины фразы), кнопки — как в плеерах:
 * глава назад, −15 с, пуск, +30 с, глава вперёд.
 */
function nowCard(resume) {
  // Обложка и название книги ведут на страницу книги.
  const coverHost = el('a', { class: 'now-cover', title: 'Открыть книгу' });
  const metaLead = el('span');
  const bookLink = el('a', { class: 'now-book' });
  const meta = el('div', { class: 'now-meta' }, metaLead, bookLink);
  const title = el('h2', { class: 'now-title' });
  const roles = el('div', { class: 'now-roles' });
  const speaker = el('div', { class: 'now-speaker' });
  const line = el('p', { class: 'now-line' });
  const quote = el('div', { class: 'now-quote' }, speaker, line);
  const fill = el('i');
  const track = el('div', { class: 'seek-track', role: 'slider', tabindex: '0', 'aria-label': 'Позиция в главе', 'aria-valuemin': '0', 'aria-valuemax': '100' }, fill);
  const position = el('span', {}, '0:00');
  const duration = el('span', {}, '0:00');
  const play = el('button', { class: 'big-play', 'aria-label': 'Пуск' }, icon('play', { className: 'filled' }));
  const prev = el('button', { class: 'round lg', 'aria-label': 'Предыдущая глава', title: 'Предыдущая глава (или в начало этой)' }, icon('skip-back', { className: 'big' }));
  const back = el('button', { class: 'round lg', 'aria-label': 'Назад 15 секунд', title: 'Назад 15 секунд' }, icon('rotate-ccw', { className: 'big' }));
  const forward = el('button', { class: 'round lg', 'aria-label': 'Вперёд 30 секунд', title: 'Вперёд 30 секунд' }, icon('rotate-cw', { className: 'big' }));
  const next = el('button', { class: 'round lg', 'aria-label': 'Следующая глава', title: 'Следующая глава' }, icon('skip-forward', { className: 'big' }));
  const rate = el('button', { class: 'surface' }, '1×');
  const sleep = el('button', { class: 'round surface', 'aria-label': 'Таймер сна', title: 'Таймер сна' }, icon('moon'));
  const text = el('a', { class: 'button surface', title: 'Читать текст вместе со звуком' }, icon('book-open'), 'Текст');
  const queue = el('button', { class: 'round surface', 'aria-label': 'Очередь', title: 'Очередь' }, icon('list-music'));

  const card = el('section', { class: 'now-card' },
    coverHost,
    el('div', { class: 'now-body' },
      el('div', { class: 'now-top' }, meta, title, roles),
      quote,
      el('div', { class: 'now-foot' },
        el('div', { class: 'now-seek' }, position, track, duration),
        el('div', { class: 'now-controls' },
          el('div', { class: 'now-side' }, rate, sleep),
          el('div', { class: 'now-transport' }, prev, back, play, forward, next),
          el('div', { class: 'now-side end' }, text, queue)))));

  // Пока ничего не играет — карточка «продолжить» с последней книгой.
  const start = guard(() => player.playBook(resume.book_id, { fromChapter: resume.chapter_id }));
  play.onclick = () => (player.snapshot().entry ? player.toggle() : start());
  prev.onclick = guard(() => player.previous());
  back.onclick = () => player.skip(-15000);
  forward.onclick = () => player.skip(30000);
  next.onclick = guard(() => player.next());
  track.onclick = (event) => {
    const state = player.snapshot();
    if (!state.entry || !state.durationMs) return;
    const rect = track.getBoundingClientRect();
    player.seek(Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)) * state.durationMs);
  };
  rate.onclick = () => popupMenu(rate, RATES.map((value) => [`${String(value).replace('.', ',')}×`, () => player.setRate(value)]));
  sleep.onclick = () => popupMenu(sleep, SLEEP.map(([value, label]) => [label, () => {
    player.setSleepTimer(value);
    toast(value ? `Таймер сна: ${label.toLowerCase()}` : 'Таймер сна выключен', 'ok');
  }]));
  queue.onclick = () => {
    const state = player.snapshot();
    popupMenu(queue, state.queue.length
      ? state.queue.map((entry, index) => [`${entry.book_title} — ${entry.label}`,
        guard(() => player.playChapter(entry.chapter_id, { index })),
        entry.chapter_id === state.entry?.chapter_id ? 'current' : ''])
      : [['Очередь пуста', () => {}, '', true]]);
  };

  let drawnBook = null;
  let drawnChapter = null;
  let drawnLine = null;
  let drawnSpeaker = null;
  let chips = new Map();
  let playIcon = null;

  /** Иконку «пуск/пауза» меняем, только когда она правда поменялась: иначе мигает. */
  function setPlayIcon(name, label) {
    if (playIcon === name) return;
    playIcon = name;
    play.replaceChildren(icon(name, { className: 'filled' }));
    play.setAttribute('aria-label', label);
  }

  /** Обложка во всю высоту карточки, а её цвет — лёгким отсветом на карточку. */
  function setCover(book) {
    coverHost.href = `#/book/${book.id}`;
    bookLink.href = `#/book/${book.id}`;
    const art = cover(book);
    coverHost.replaceChildren(art);
    const glow = art.style?.getPropertyValue('--cv-cut');
    if (glow) card.style.setProperty('--glow', glow);
    else card.style.removeProperty('--glow');
  }

  function drawResume() {
    setCover({ id: resume.book_id, title: resume.title, author: resume.author, cover_path: resume.cover_path, genres: resume.genres, moods: resume.moods });
    metaLead.textContent = 'Продолжить · ';
    bookLink.textContent = resume.title;
    title.textContent = resume.chapter_label || resume.title;
    text.href = resume.chapter_id ? `#/chapter/${resume.chapter_id}` : `#/book/${resume.book_id}`;
    roles.replaceChildren();
    chips = new Map();
    drawnChapter = null;
    drawnLine = null;
    speaker.replaceChildren();
    quote.classList.add('plain');
    line.className = 'now-line';
    line.replaceChildren(el('span', { class: 'before' },
      resume.total_ms ? `Прослушано ${Math.round(resume.progress * 100)}% · осталось ${formatSpan(resume.total_ms - resume.listened_ms)}` : ''));
    fill.style.width = `${(resume.chapter_ms ? resume.position_ms / resume.chapter_ms : 0) * 100}%`;
    position.textContent = formatDuration(resume.position_ms);
    duration.textContent = formatDuration(resume.chapter_ms);
    setPlayIcon('play', 'Продолжить');
    prev.disabled = true;
    next.disabled = true;
  }

  function drawLive(state) {
    const entry = state.entry;
    if (drawnBook !== entry.book_id) {
      drawnBook = entry.book_id;
      setCover({
        id: entry.book_id, title: entry.book_title, author: entry.book_author, has_cover: entry.has_cover,
        ...('cover_path' in entry ? { cover_path: entry.cover_path } : {}), genres: entry.genres, moods: entry.moods,
      });
    }
    const count = state.queue.filter((item) => item.book_id === entry.book_id).length;
    const place = state.queue.filter((item) => item.book_id === entry.book_id).findIndex((item) => item.chapter_id === entry.chapter_id);
    if (bookLink.textContent !== entry.book_title) bookLink.textContent = entry.book_title;
    const lead = count > 1 && place >= 0 ? `Глава ${place + 1} из ${count} · ` : '';
    if (metaLead.textContent !== lead) metaLead.textContent = lead;
    if (title.textContent !== entry.label) title.textContent = entry.label;
    text.href = `#/chapter/${entry.chapter_id}`;

    const segment = state.segment;
    const speaking = segment?.speaker || null;
    // Роли главы рисуем один раз, дальше только переключаем, кто говорит, —
    // тогда подсветка переезжает плавно, а не мигает.
    if (drawnChapter !== entry.chapter_id) {
      drawnChapter = entry.chapter_id;
      drawnSpeaker = undefined;
      const seen = new Map();
      for (const item of state.segments) if (!seen.has(item.speaker)) seen.set(item.speaker, item.slot);
      const names = [...seen.keys()];
      chips = new Map(names.slice(0, MAX_ROLES).map((name) => [name, roleTint(el('span', { class: 'role-chip' },
        avatar(name, seen.get(name), 'sm'), el('span', { class: 'role-chip-name', dataset: { text: speakerName(name) } }, speakerName(name))), seen.get(name))]));
      roles.replaceChildren(...chips.values(),
        names.length > MAX_ROLES ? el('span', { class: 'role-chip more' }, `ещё ${names.length - MAX_ROLES}`) : '');
    }
    if (drawnSpeaker !== speaking) {
      drawnSpeaker = speaking;
      roles.classList.toggle('has-speaker', Boolean(speaking && chips.has(speaking)));
      for (const [name, chip] of chips) chip.classList.toggle('speaking', name === speaking);
    }

    const lineKey = `${entry.chapter_id}:${segment?.id ?? ''}:${state.playing}`;
    if (drawnLine !== lineKey) {
      const sameLine = drawnLine?.split(':').slice(0, 2).join(':') === lineKey.split(':').slice(0, 2).join(':');
      drawnLine = lineKey;
      quote.classList.toggle('plain', !segment);
      if (segment && !sameLine) {
        roleTint(quote, segment.slot);
        const mood = segment.emotion && segment.emotion !== 'нейтрально' ? segment.emotion : '';
        speaker.replaceChildren(avatar(segment.speaker, segment.slot, 'sm'),
          el('b', {}, speakerName(segment.speaker)), mood ? el('span', { class: 'hint' }, mood) : '');
        line.className = `now-line ${lineSize(segment.text)}`.trim();
        line.replaceChildren(segment.speaker === 'narrator' ? segment.text : `«${segment.text}»`);
        // Новая реплика проявляется, а не выскакивает.
        quote.classList.remove('swap');
        void quote.offsetWidth;
        quote.classList.add('swap');
      } else if (!segment) {
        speaker.replaceChildren();
        line.className = 'now-line';
        line.replaceChildren(el('span', { class: 'before' }, state.playing ? '' : state.notice || 'На паузе'));
      }
    }

    const share = state.durationMs ? state.positionMs / state.durationMs : 0;
    fill.style.width = `${share * 100}%`;
    track.setAttribute('aria-valuenow', String(Math.round(share * 100)));
    position.textContent = formatDuration(state.positionMs);
    duration.textContent = formatDuration(state.durationMs);
    setPlayIcon(state.playing ? 'pause' : 'play', state.playing ? 'Пауза' : 'Пуск');
    prev.disabled = false;
    next.disabled = state.index + 1 >= state.queue.length;
    const rateText = `${String(state.rate).replace('.', ',')}×`;
    if (rate.textContent !== rateText) rate.textContent = rateText;
    sleep.classList.toggle('active', Boolean(state.sleep));
  }

  const unsubscribe = player.subscribe((state) => {
    if (state.entry) drawLive(state);
    else if (resume) drawResume();
  });
  return { card, unsubscribe };
}

export function render(view) {
  let alive = true;
  let unsubscribe = null;
  const page = el('div', { class: 'page home-page' });
  view.replaceChildren(page);

  const load = guard(async () => {
    const [tree, recent] = await Promise.all([
      get('/api/library'),
      get('/api/continue?limit=12').catch(() => ({ items: [] })),
    ]);
    if (!alive) return;
    unsubscribe?.();
    unsubscribe = null;
    const books = flatten(tree);
    learnBooks(books);
    if (!books.length) {
      page.replaceChildren(firstRun());
      return;
    }

    const listening = new Map(recent.items.filter((item) => item.total_ms).map((item) => [item.book_id, item]));
    const resume = recent.items.find((item) => item.total_ms) || null;
    let hero;
    if (player.snapshot().entry || resume) {
      const now = nowCard(resume);
      unsubscribe = now.unsubscribe;
      hero = now.card;
    } else {
      const voiced = books.find((book) => book.voiced_chapters);
      hero = el('section', { class: 'now-card idle' },
        el('div', { class: 'now-cover' }, cover(voiced || books[0])),
        el('div', { class: 'now-body' },
          el('div', { class: 'now-top' },
            el('div', { class: 'now-meta' }, voiced ? 'Можно начинать' : 'Пока нечего слушать'),
            el('h2', { class: 'now-title' }, voiced ? voiced.title : 'Книги ещё не озвучены')),
          el('p', { class: 'now-line' }, el('span', { class: 'before' }, voiced
            ? `${plural(voiced.voiced_chapters, 'глава озвучена', 'главы озвучены', 'глав озвучено')} · ${formatSpan(voiced.duration_ms)}`
            : 'Откройте студию: разберите книгу по ролям и озвучьте её.')),
          el('div', { class: 'now-foot' },
            voiced
              ? el('div', { class: 'row' }, el('button', { class: 'primary xl', onclick: guard(() => player.playBook(voiced.id)) },
                icon('play', { className: 'filled' }), 'Слушать'))
              : el('div', { class: 'row' }, el('a', { class: 'button primary xl', href: '#/studio' }, icon('wand-sparkles'), 'Открыть студию')))));
    }

    // «Продолжить» — начатые книги, кроме той, что уже в большой карточке.
    const current = player.snapshot().entry?.book_id ?? resume?.book_id;
    const started = recent.items
      .filter((item) => item.total_ms && item.progress > 0 && item.progress < 1 && item.book_id !== current)
      .map((item) => books.find((book) => book.id === item.book_id))
      .filter(Boolean)
      .slice(0, SHELF);
    const fresh = [...books]
      .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || '') || b.id - a.id)
      .filter((book) => !started.includes(book))
      .slice(0, SHELF);

    const shelf = (title, items, link) => el('section', { class: 'section' },
      el('div', { class: 'section-head' }, el('h2', {}, title), link),
      el('div', { class: 'btile-grid' }, items.map((book) => shelfTile(book, listening))));

    page.replaceChildren(...[
      el('div', { class: 'home-head' },
        el('h1', {}, greeting()),
        el('span', { class: 'hint' }, `${plural(books.length, 'книга', 'книги', 'книг')} в библиотеке`)),
      hero,
      started.length ? shelf('Продолжить слушать', started, null) : null,
      shelf('Недавно добавленные', fresh, el('a', { href: '#/library' }, 'вся библиотека')),
    ].filter(Boolean));
  });

  const onChange = () => load();
  window.addEventListener('library-changed', onChange);
  load();
  return () => {
    alive = false;
    unsubscribe?.();
    window.removeEventListener('library-changed', onChange);
  };
}
