// Студия — рабочее место книги: пять шагов от текста до готовой аудиокниги.
//
// Каждый шаг — отдельный экран: что на нём происходит, что уже сделано и
// одна главная кнопка. Шаги видны всегда и кликабельны; их состояние берётся
// из книги, а не из того, где человек сейчас. Ничего не запускается вслепую:
// разметка и озвучка стартуют только отсюда, с объяснением, что будет.

import { get, post } from '../../api.js';
import { avatar, cover, learnBooks, speakerName } from '../../covers.js';
import { busyPill, markupJobForChapter, synthJobForBook, synthJobForChapter, watchActiveJobs } from '../../jobs-state.js';
import * as player from '../../player.js';
import { confirmAction, el, emit, formatDuration, formatSpan, guard, icon, plural, toast } from '../../ui.js';
import { ensureVoices } from '../../voices.js';
import { mountVoices } from './book.js';

export const STEP_KEYS = ['text', 'roles', 'voices', 'voice', 'done'];
const STEP_TITLES = {
  text: ['Текст', 'Главы книги', 'Проверьте, что главы нашлись правильно: названия и границы. Лишнее можно склеить, разделить или удалить на странице книги.'],
  roles: ['Роли', 'Кто что говорит', 'Claude читает главу и отмечает, чья это реплика и с какой интонацией. Проверить и поправить можно в разметке главы: там текст подсвечен цветом роли.'],
  voices: ['Голоса', 'Кто читает книгу', 'Каждой роли — свой голос. Подобраны по полу персонажей; нажмите ▶ — прозвучит реплика из книги этим голосом.'],
  voice: ['Озвучка', 'Озвучка глав', 'Главы озвучиваются в фоне по очереди. Можно закрыть окно — очередь продолжится при следующем запуске.'],
  done: ['Готово', 'Аудиокнига готова', 'Слушайте в приложении или выгрузите в m4b для телефона и плееров аудиокниг.'],
};

/** Состояние шагов по данным книги: done — сделан, part — начат, todo — впереди. */
function stepStates(chapters, readiness) {
  const total = chapters.length;
  const marked = chapters.filter((c) => c.segments).length;
  const voiced = chapters.filter((c) => c.duration_ms).length;
  const missing = readiness.missing_voice.length + readiness.unavailable_voice.length;
  return {
    text: total ? 'done' : 'todo',
    roles: total && marked >= total ? 'done' : marked ? 'part' : 'todo',
    voices: !marked ? 'todo' : missing ? 'part' : 'done',
    voice: total && voiced >= total ? 'done' : voiced ? 'part' : 'todo',
    done: total && voiced >= total ? 'done' : 'todo',
  };
}

/** С какого шага начать: первый несделанный. */
export function nextStep(states) {
  return STEP_KEYS.find((key) => key !== 'text' && states[key] !== 'done') || 'done';
}

export function render(view, bookId, requested = null) {
  let alive = true;
  let data = null;
  let readiness = null;
  let step = requested;
  let cleanupStep = null;
  const page = el('div', { class: 'page workspace' });
  view.replaceChildren(page);

  const load = guard(async () => {
    const [bookData, ready] = await Promise.all([
      get(`/api/books/${bookId}`),
      get(`/api/books/${bookId}/readiness`),
    ]);
    if (!alive) return;
    data = bookData;
    readiness = ready;
    learnBooks([data.book]);
    const states = stepStates(data.chapters, readiness);
    if (!step) {
      // Без шага в адресе — сразу туда, где книга сейчас; адрес без новой записи в истории.
      step = nextStep(states);
      history.replaceState(history.state, '', `#/studio/book/${bookId}/${step}`);
    }
    draw(states);
  });

  function draw(states) {
    cleanupStep?.();
    cleanupStep = null;
    const { book, chapters } = data;
    const index = STEP_KEYS.indexOf(step);
    const [, heading, lead] = STEP_TITLES[step];
    const body = el('div', { class: 'step-body' });

    const nav = el('nav', { class: 'work-steps', 'aria-label': 'Шаги' }, STEP_KEYS.map((key, n) => {
      const state = states[key];
      return el('a', {
        class: `work-step ${state}${key === step ? ' current' : ''}`, href: `#/studio/book/${bookId}/${key}`,
        'aria-current': key === step ? 'step' : null,
      },
        el('span', { class: 'work-dot' }, state === 'done' ? icon('check', { size: 14 }) : String(n + 1)),
        el('span', { class: 'work-step-text' },
          el('b', {}, STEP_TITLES[key][0]),
          el('span', { class: 'hint' }, stepNote(key, state))));
    }));

    const prev = STEP_KEYS[index - 1];
    const next = STEP_KEYS[index + 1];
    const footer = el('div', { class: 'step-footer' },
      prev ? el('a', { class: 'button ghost', href: `#/studio/book/${bookId}/${prev}` }, icon('arrow-left'), `Назад: ${STEP_TITLES[prev][0]}`) : el('span'),
      next ? el('a', { class: `button ${states[step] === 'done' ? 'primary' : 'surface'}`, href: `#/studio/book/${bookId}/${next}` },
        `Дальше: ${STEP_TITLES[next][0]}`, icon('arrow-right')) : el('span'));

    page.replaceChildren(
      el('header', { class: 'work-head' },
        el('a', { class: 'work-cover', href: `#/book/${bookId}`, title: 'Открыть книгу' }, cover(book, { text: false })),
        el('div', { class: 'grow' },
          el('nav', { class: 'crumbs', 'aria-label': 'Путь' },
            el('a', { href: '#/studio' }, 'Студия'), el('span', { 'aria-hidden': 'true' }, '›'), el('span', { class: 'here' }, 'Книга')),
          el('h1', { class: 'work-title' }, book.title),
          el('a', { class: 'hint', href: `#/book/${bookId}` }, 'страница книги →'))),
      nav,
      el('section', { class: 'step-intro' },
        el('h2', {}, `${index + 1}. ${heading}`),
        el('p', { class: 'lead' }, lead)),
      body,
      footer);

    const steps = { text: drawText, roles: drawRoles, voices: drawVoices, voice: drawVoice, done: drawDone };
    cleanupStep = steps[step](body, states) || null;
  }

  function stepNote(key, state) {
    const total = data.chapters.length;
    const marked = data.chapters.filter((c) => c.segments).length;
    const voiced = data.chapters.filter((c) => c.duration_ms).length;
    if (key === 'text') return plural(total, 'глава', 'главы', 'глав');
    if (key === 'roles') return state === 'done' ? 'все главы' : `${marked} из ${total}`;
    if (key === 'voices') {
      const missing = readiness.missing_voice.length + readiness.unavailable_voice.length;
      return !marked ? 'после ролей' : missing ? `без голоса: ${missing}` : 'назначены';
    }
    if (key === 'voice') return `${voiced} из ${total}`;
    return state === 'done' ? formatSpan(data.chapters.reduce((n, c) => n + (c.duration_ms || 0), 0)) : 'впереди';
  }

  // ---------- 1. текст ----------

  function drawText(body) {
    const rows = data.chapters.map((chapter) => el('div', { class: 'work-row' },
      el('span', { class: 'work-num' }, String(chapter.number)),
      el('div', { class: 'grow' },
        el('b', {}, chapter.title || `Глава ${chapter.number}`),
        el('span', { class: 'hint' }, `${chapter.n_chars.toLocaleString('ru')} знаков`)),
      el('a', { class: 'button sm surface', href: `#/chapter/${chapter.id}` }, icon('book-open', { size: 14 }), 'Читать')));
    body.append(
      el('div', { class: 'work-actions' },
        el('a', { class: 'button surface', href: `#/book/${bookId}` }, icon('pencil'), 'Поправить главы на странице книги')),
      el('div', { class: 'work-list' }, rows));
  }

  // ---------- 2. роли ----------

  const markupAll = guard(async () => {
    const unmarked = data.chapters.filter((c) => !c.segments).length;
    const ok = await confirmAction('Разобрать по ролям?',
      `${plural(unmarked, 'глава уйдёт', 'главы уйдут', 'глав уйдут')} в Claude по очереди. ` +
      'Это платно — по вашему ключу Anthropic. Уже разобранное и ручные правки не тронутся.', 'Разобрать');
    if (!ok) return;
    const job = await post(`/api/books/${bookId}/markup`, {});
    toast(`Задача добавлена: ${job.title}`, 'ok');
    emit('jobs-changed');
  });

  const markupOne = (chapter) => guard(async () => {
    const job = await post(`/api/chapters/${chapter.id}/markup-job`, {});
    toast(`Задача добавлена: ${job.title}`, 'ok');
    emit('jobs-changed');
  });

  function drawRoles(body) {
    const chapters = data.chapters;
    const unmarked = chapters.filter((c) => !c.segments);
    const summary = el('div', { class: 'work-summary' });
    const list = el('div', { class: 'work-list' });

    const draw = () => {
      const bookJob = chapters.map((c) => markupJobForChapter(c.id, bookId)).find(Boolean);
      // replaceChildren печатает null текстом — пустые части отбрасываем.
      summary.replaceChildren(...[
        data.speakers.length
          ? el('div', { class: 'role-summary' },
            el('span', { class: 'hint' }, `Найдено ${plural(data.speakers.length, 'роль', 'роли', 'ролей')}:`),
            data.speakers.map((speaker) => el('span', { class: 'role-chip' },
              avatar(speaker.name, speaker.slot, 'sm'), `${speakerName(speaker.name)} · ${speaker.count}`)))
          : el('span', { class: 'hint' }, 'Ролей пока нет — ни одна глава не разобрана.'),
        readiness.rare_speakers?.length
          ? el('div', { class: 'callout caution' }, icon('triangle-alert'),
            el('div', {}, el('b', {}, 'Проверьте редкие роли. '),
              `У ${readiness.rare_speakers.map((r) => `«${r.speaker}»`).join(', ')} всего одна-две реплики на книгу — возможно, это не персонаж, а имя из обращения. `,
              'Откройте разметку главы и отдайте реплики рассказчику, если так.'))
          : null,
        unmarked.length && !bookJob
          ? el('div', { class: 'work-actions' },
            el('button', { class: 'primary lg', onclick: markupAll }, icon('wand-sparkles'),
              unmarked.length === chapters.length ? 'Разобрать книгу по ролям' : `Разобрать оставшиеся · ${unmarked.length}`),
            el('span', { class: 'hint' }, 'Платно — по вашему ключу Anthropic. Можно разобрать и одну главу — кнопкой в её строке.'))
          : bookJob ? el('div', { class: 'work-actions' }, busyPill(bookJob, 'Размечаю')) : null,
      ].filter(Boolean));

      list.replaceChildren(...chapters.map((chapter) => {
        const job = markupJobForChapter(chapter.id, bookId);
        const busy = job && (job.kind === 'markup' || !chapter.segments);
        const status = chapter.segments
          ? `разобрана · ${plural(chapter.segments, 'реплика', 'реплики', 'реплик')}`
          : 'не разобрана';
        return el('div', { class: `work-row${chapter.segments ? ' ok' : ''}` },
          el('span', { class: 'work-num' }, chapter.segments ? icon('check', { size: 14 }) : String(chapter.number)),
          el('div', { class: 'grow' },
            el('b', {}, chapter.title || `Глава ${chapter.number}`),
            el('span', { class: 'hint' }, status)),
          busy ? busyPill(job, 'Размечаю')
            : chapter.segments
              ? el('a', { class: 'button sm surface', href: `#/chapter/${chapter.id}/edit` }, icon('pencil', { size: 14 }), 'Открыть разметку')
              : el('span', { class: 'row' },
                el('a', { class: 'button sm ghost', href: `#/chapter/${chapter.id}/edit` }, 'Разметить вручную'),
                el('button', { class: 'sm surface', onclick: markupOne(chapter) }, icon('wand-sparkles', { size: 14 }), 'Разобрать')));
      }));
    };
    draw();
    body.append(summary, list);
    return watchJobs(draw);
  }

  // ---------- 3. голоса ----------

  function drawVoices(body) {
    return mountVoices(body, bookId);
  }

  // ---------- 4. озвучка ----------

  const voiceBook = guard(async () => {
    if (!(await ensureVoices(bookId))) return;
    const job = await post(`/api/books/${bookId}/synthesize`, {});
    toast(`Задача добавлена: ${job.title}`, 'ok');
    emit('jobs-changed');
  });

  const voiceOne = (chapter) => guard(async () => {
    if (!(await ensureVoices(bookId, { chapterId: chapter.id }))) return;
    const job = await post(`/api/chapters/${chapter.id}/synthesize`, {});
    toast(`Задача добавлена: ${job.title}`, 'ok');
    emit('jobs-changed');
  });

  function drawVoice(body) {
    const chapters = data.chapters;
    const summary = el('div', { class: 'work-summary' });
    const list = el('div', { class: 'work-list' });
    const draw = () => {
      const bookJob = synthJobForBook(bookId);
      const marked = chapters.filter((c) => c.segments);
      const left = marked.filter((c) => !c.duration_ms || c.voiced < c.segments);
      const missing = readiness.missing_voice.length + readiness.unavailable_voice.length;
      summary.replaceChildren(...[
        !marked.length
          ? el('div', { class: 'callout caution' }, icon('triangle-alert'),
            el('div', {}, 'Озвучивать пока нечего: ни одна глава не разобрана по ролям. ',
              el('a', { href: `#/studio/book/${bookId}/roles` }, 'К шагу «Роли»')))
          : null,
        missing
          ? el('div', { class: 'callout caution' }, icon('mic'),
            el('div', { class: 'grow' }, `Не у всех ролей есть голос (${missing}) — их реплики не озвучатся. `),
            el('button', { class: 'surface sm', onclick: guard(async () => { if (await ensureVoices(bookId)) load(); }) }, 'Выбрать голоса'))
          : null,
        bookJob
          ? el('div', { class: 'work-actions' }, busyPill(bookJob))
          : left.length
            ? el('div', { class: 'work-actions' },
              el('button', { class: 'primary lg', onclick: voiceBook }, icon('mic'),
                left.length === marked.length ? 'Озвучить книгу' : `Озвучить оставшиеся · ${left.length}`),
              el('span', { class: 'hint' }, 'Уже озвученные реплики не переозвучиваются.'))
            : marked.length ? el('div', { class: 'work-actions' },
              el('button', { class: 'primary lg', onclick: guard(() => player.playBook(bookId)) }, icon('play', { className: 'filled' }), 'Слушать книгу')) : null,
      ].filter(Boolean));

      list.replaceChildren(...chapters.map((chapter) => {
        let job = synthJobForChapter(chapter.id, bookId);
        if (job?.kind === 'book' && (chapter.duration_ms || !chapter.segments)) job = null;
        const status = !chapter.segments ? 'не разобрана — сначала роли'
          : chapter.duration_ms ? `озвучена · ${formatDuration(chapter.duration_ms)}`
            : chapter.voiced ? `озвучено ${chapter.voiced} из ${chapter.segments} реплик`
              : 'не озвучена';
        return el('div', { class: `work-row${chapter.duration_ms ? ' ok' : ''}` },
          el('span', { class: 'work-num' }, chapter.duration_ms ? icon('check', { size: 14 }) : String(chapter.number)),
          el('div', { class: 'grow' },
            el('b', {}, chapter.title || `Глава ${chapter.number}`),
            el('span', { class: `hint${chapter.errors ? ' warn' : ''}` }, chapter.errors ? `не озвучено реплик: ${chapter.errors}` : status)),
          job ? busyPill(job)
            : chapter.duration_ms
              ? el('button', { class: 'sm surface', onclick: guard(() => player.playChapter(chapter.id)) }, icon('play', { className: 'filled', size: 12 }), 'Слушать')
              : chapter.segments
                ? el('button', { class: 'sm surface', onclick: voiceOne(chapter) }, icon('mic', { size: 14 }), 'Озвучить')
                : el('a', { class: 'button sm ghost', href: `#/chapter/${chapter.id}/edit` }, 'Разметить'));
      }));
    };
    draw();
    body.append(summary, list);
    return watchJobs(draw);
  }

  // ---------- 5. готово ----------

  function drawDone(body, states) {
    const duration = data.chapters.reduce((n, c) => n + (c.duration_ms || 0), 0);
    const voiced = data.chapters.filter((c) => c.duration_ms).length;
    body.append(states.done === 'done'
      ? el('div', { class: 'done-card' },
        el('div', { class: 'done-stats' },
          el('div', {}, el('b', {}, formatSpan(duration)), el('span', { class: 'hint' }, 'звучания')),
          el('div', {}, el('b', {}, String(voiced)), el('span', { class: 'hint' }, plural(voiced, 'глава', 'главы', 'глав').replace(/^\d+ /, ''))),
          el('div', {}, el('b', {}, String(data.speakers.length)), el('span', { class: 'hint' }, plural(data.speakers.length, 'роль', 'роли', 'ролей').replace(/^\d+ /, '')))),
        el('div', { class: 'work-actions' },
          el('button', { class: 'primary lg', onclick: guard(() => player.playBook(bookId)) }, icon('play', { className: 'filled' }), 'Слушать'),
          el('a', { class: 'button surface lg', href: `#/book/${bookId}` }, icon('download'), 'Экспорт — на странице книги')))
      : el('div', { class: 'callout caution' }, icon('triangle-alert'),
        el('div', {}, `Озвучено ${voiced} из ${data.chapters.length} глав. `,
          el('a', { href: `#/studio/book/${bookId}/voice` }, 'К шагу «Озвучка»'))));
  }

  // ---------- задачи ----------

  /**
   * Перерисовать шаг, когда меняется его задача; когда задача закончилась —
   * перечитать книгу: появилась разметка или озвучка.
   */
  function watchJobs(draw) {
    let before = '';
    return watchActiveJobs((jobs) => {
      const mine = jobs.filter((job) => job.book_id === bookId);
      const key = mine.map((job) => `${job.id}:${job.status}:${Math.round((job.progress || 0) * 100)}`).join('|');
      if (key === before) return;
      const finished = before && mine.length < before.split('|').filter(Boolean).length;
      before = key;
      if (finished) load();
      else draw();
    });
  }

  const onChange = () => load();
  window.addEventListener('library-changed', onChange);
  load();
  return () => {
    alive = false;
    cleanupStep?.();
    window.removeEventListener('library-changed', onChange);
  };
}
