// Студия — обзор: книги в работе с одним понятным следующим шагом,
// что делается прямо сейчас, готовы ли движки.

import { get, post } from '../../api.js';
import { cover } from '../../covers.js';
import { confirmAction, el, emit, guard, icon, plural, toast } from '../../ui.js';
import { ACTIVE, HINT_KEY, bookSteps, jobIcon, jobRoute, setStudioJobs, stepPills, studioFrame } from './common.js';

const POLL_MS = 2000;

function hintHidden() {
  try { return localStorage.getItem(HINT_KEY) === '1'; } catch { return false; }
}

function howTo(onHide) {
  const step = (n, title, text) => el('div', { class: 'howto-step' },
    el('span', { class: 'n' }, n), el('span', {}, el('b', {}, title), el('span', { class: 'hint' }, text)));
  return el('section', { class: 'howto' },
    el('div', { class: 'howto-title' }, 'Как сделать аудиокнигу'),
    step('1', 'Добавьте книгу', 'txt, fb2, epub, docx или pdf'),
    step('2', 'Разберите по ролям', 'Claude отметит, кто что говорит'),
    step('3', 'Выберите голоса', 'подберём сами, можно поменять'),
    step('4', 'Озвучьте', 'главы соберутся в mp3 и m4b'),
    el('button', {
      class: 'round ghost', 'aria-label': 'Скрыть подсказку',
      title: 'Скрыть подсказку — вернуть её можно кнопкой «?» справа от вкладок', onclick: onHide,
    }, icon('x')));
}

export function render(view) {
  let alive = true;
  let timer = null;
  const page = studioFrame(view, 'overview');
  let finishedOpen = false;
  let last = null;

  const load = guard(async () => {
    const [studio, jobs, engines, tokens] = await Promise.all([
      get('/api/studio'),
      get('/api/jobs'),
      get('/api/engines').catch(() => ({ engines: [] })),
      get('/api/tokens').catch(() => ({ tokens: [] })),
    ]);
    if (!alive) return;
    last = [studio.books, jobs, engines.engines, tokens];
    draw(...last);
    clearTimeout(timer);
    if (jobs.active) timer = setTimeout(load, POLL_MS);
  });

  /**
   * Что делать с книгой дальше. Кнопка ведёт на нужный шаг книги — там
   * объяснено, что будет, и запускается само действие. Вслепую отсюда ничего
   * не стартует.
   */
  function nextAction(book, running) {
    const { next } = bookSteps(book);
    const go = (step) => () => { location.hash = `#/studio/book/${book.id}/${step}`; };
    if (running) return { text: 'Смотреть, как идёт', run: go(running.kind.startsWith('markup') ? 'roles' : 'voice') };
    if (next === 1) return { text: book.marked ? 'Шаг 2: роли' : 'Начать: роли', run: go('roles') };
    if (next === 2) return { text: 'Шаг 3: голоса', run: go('voices') };
    if (next === 3) return { text: book.voiced ? 'Шаг 4: доозвучить' : 'Шаг 4: озвучка', run: go('voice') };
    return { text: 'Слушать', run: () => { location.hash = `#/book/${book.id}`; } };
  }

  /** Объяснение простыми словами: что с книгой сейчас. */
  function status(book, running) {
    if (running) {
      const percent = Math.round((running.progress || 0) * 100);
      return `${running.title} — ${running.status === 'pending' ? 'в очереди' : `${percent}%`}`;
    }
    if (book.errors) return `${plural(book.errors, 'реплика не озвучилась', 'реплики не озвучились', 'реплик не озвучились')} — запустите озвучку ещё раз.`;
    const { next } = bookSteps(book);
    if (next === 1 && !book.marked) return 'Claude прочитает текст и отметит, кто говорит. Это платно — по вашему ключу.';
    if (next === 1) return `Разобрано ${book.marked} из ${book.chapters} глав. Остальные ждут разметки.`;
    if (next === 2) return `У ${plural(book.missing_voice + book.unavailable_voice, 'роли', 'ролей', 'ролей')} нет голоса — подберём сами, можно поменять.`;
    if (next === 3 && book.voiced) return `Озвучено ${book.voiced} из ${book.chapters} глав. Остальные можно поставить в очередь разом.`;
    if (next === 3) return `Всё готово к озвучке: ${plural(book.roles, 'роль', 'роли', 'ролей')} с голосами.`;
    return 'Книга готова — можно слушать и выгрузить в m4b.';
  }

  function bookCard(book, jobs) {
    const running = jobs.find((job) => ACTIVE.has(job.status) && jobTouches(job, book));
    const action = nextAction(book, running);
    return el('div', { class: 'studio-book' },
      el('a', { class: 'studio-cover', href: `#/studio/book/${book.id}`, 'aria-label': `Открыть «${book.title}» в студии` }, cover(book)),
      el('div', { class: 'studio-book-body' },
        el('div', { class: 'studio-book-title' },
          el('a', { href: `#/studio/book/${book.id}` }, book.title),
          el('span', { class: 'hint' }, [plural(book.chapters, 'глава', 'главы', 'глав'), book.author].filter(Boolean).join(' · '))),
        stepPills(book),
        el('div', { class: `hint${book.errors ? ' warn' : ''}` }, status(book, running))),
      el('button', { class: running ? '' : 'primary', onclick: action.run }, action.text));
  }

  const jobTouches = (job, book) => job.book_id === book.id;

  function nowPanel(jobs) {
    const active = jobs.filter((job) => ACTIVE.has(job.status));
    const running = active.find((job) => job.status === 'running') || active[0];
    const waiting = active.filter((job) => job !== running);
    const body = running
      ? [el('div', { class: 'now-job' },
        el('span', { class: 'job-icon' }, icon(jobIcon(running))),
        el('div', { class: 'grow' },
          jobRoute(running) ? el('a', { class: 'job-title', href: jobRoute(running) }, running.title) : el('b', {}, running.title),
          el('div', { class: 'hint' },
            running.status === 'pending' ? 'ждёт своей очереди'
              : running.total ? `${running.done} из ${running.total} · ${Math.round((running.progress || 0) * 100)}%` : 'выполняется'),
          el('div', { class: 'progress wide' }, el('i', { style: { width: `${(running.progress || 0) * 100}%` } })))),
      el('div', { class: 'row' },
        el('button', {
          class: 'ghost', onclick: guard(async () => { await post(`/api/jobs/${running.id}/cancel`); load(); }),
        }, 'Отменить')),
      waiting.length
        ? el('div', { class: 'hint now-next' }, icon('list-music'),
          `Дальше: ${waiting[0].title}${waiting.length > 1 ? ` и ещё ${waiting.length - 1}` : ''}`)
        : null]
      : [el('p', { class: 'hint' }, 'Сейчас ничего не делается. Озвучка и разметка идут в фоне — можно слушать и закрывать окно.')];
    return el('section', { class: 'card-box side-card' },
      el('div', { class: 'section-head' }, el('h2', {}, 'Сейчас'), el('a', { href: '#/studio/jobs' }, 'все задачи')),
      ...body);
  }

  function enginesPanel(engines, tokens) {
    const key = (tokens.tokens || []).find((t) => t.service === 'anthropic');
    return el('section', { class: 'card-box side-card' },
      el('h2', {}, 'Голоса и движки'),
      engines.map((engine) => el('div', { class: 'engine-line' },
        el('span', { class: `state-dot ${engine.ready ? 'ok' : ''}`, 'aria-hidden': 'true' }),
        el('div', { class: 'grow' },
          el('b', {}, engine.title || engine.name),
          el('div', { class: 'hint' }, engine.detail || (engine.ready ? 'готов' : 'недоступен'))),
        engine.needs_key && !engine.ready ? el('a', { href: '#/settings' }, 'добавить ключ') : null)),
      el('div', { class: 'engine-foot hint' },
        'Разбор по ролям: Claude · ', key?.present ? 'ключ добавлен' : el('a', { href: '#/settings' }, 'нужен ключ')));
  }

  function draw(books, jobs, engines, tokens) {
    const hideHint = () => {
      try { localStorage.setItem(HINT_KEY, '1'); } catch { /* удобство */ }
      const card = page.querySelector('.howto');
      if (!card) return;
      card.classList.add('leaving');
      setTimeout(() => card.remove(), 200);
    };
    const working = books.filter((book) => !(book.chapters && book.voiced >= book.chapters));
    const finished = books.filter((book) => book.chapters && book.voiced >= book.chapters);
    setStudioJobs(jobs.active);
    page.replaceChildren(
      hintHidden() ? '' : howTo(hideHint),
      el('div', { class: 'studio-grid' },
        el('div', { class: 'section' },
          el('div', { class: 'section-head' }, el('h2', {}, 'В работе'),
            el('a', { href: '#', onclick: (event) => { event.preventDefault(); emit('import-request', null); } }, 'добавить книгу')),
          working.length
            ? working.map((book) => bookCard(book, jobs.jobs))
            : el('div', { class: 'placeholder' }, books.length
              ? 'Все книги готовы. Добавьте новую — кнопкой «Книга» вверху или перетащив файл в окно.'
              : 'Библиотека пуста. Добавьте книгу — кнопкой «Книга» вверху или перетащив файл в окно.'),
          finished.length
            ? el('details', {
              class: 'finished', open: finishedOpen,
              ontoggle: (event) => { finishedOpen = event.currentTarget.open; },
            },
              el('summary', {}, `Готовые · ${finished.length}`),
              finished.map((book) => bookCard(book, jobs.jobs)))
            : null),
        el('aside', { class: 'studio-side' }, nowPanel(jobs.jobs), enginesPanel(engines, tokens))));
  }

  const onChange = () => load();
  // «?» у вкладок: подсказка возвращается на место.
  const onHint = () => {
    if (last) draw(...last);
    const card = page.querySelector('.howto');
    card?.classList.add('appear');
    card?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  };
  window.addEventListener('jobs-changed', onChange);
  window.addEventListener('library-changed', onChange);
  window.addEventListener('studio-hint', onHint);
  load();
  return () => {
    alive = false;
    clearTimeout(timer);
    window.removeEventListener('jobs-changed', onChange);
    window.removeEventListener('library-changed', onChange);
    window.removeEventListener('studio-hint', onHint);
  };
}
