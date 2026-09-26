// Студия — задачи: что делается сейчас, что ждёт и что уже сделано.
// Фильтры по состоянию и виду, история по дням, «Показать ещё» вместо
// бесконечного списка.

import { del, get, post } from '../../api.js';
import { confirmAction, dropdown, el, guard, icon, toast } from '../../ui.js';
import { ACTIVE, JOB_STATUS, humanError, jobFix, jobIcon, jobRoute, retryJob, setStudioJobs, studioFrame } from './common.js';

const POLL_MS = 1500;
const PAGE = 20;

const STATES = [
  ['all', 'Все', () => true],
  ['active', 'В работе', (job) => ACTIVE.has(job.status)],
  ['failed', 'С ошибкой', (job) => job.status === 'failed'],
  ['done', 'Готово', (job) => job.status === 'done'],
  ['cancelled', 'Отменённые', (job) => job.status === 'cancelled'],
];
const KINDS = [
  ['', 'Все виды'],
  ['voice', 'Озвучка', ['synthesis', 'book', 'folder']],
  ['markup', 'Разметка', ['markup', 'markup_book']],
  ['export', 'Экспорт', ['export']],
  ['service', 'Служебные', ['previews', 'qwen_move']],
];

/** Дата задачи: SQLite отдаёт UTC без пояса — добавляем его, иначе время уедет. */
function parse(stamp) {
  if (!stamp) return null;
  const date = new Date(`${stamp.replace(' ', 'T')}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function dayTitle(date) {
  if (!date) return 'Раньше';
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return 'Сегодня';
  if (date.toDateString() === yesterday.toDateString()) return 'Вчера';
  return date.toLocaleDateString('ru', { day: 'numeric', month: 'long', year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
}

const clock = (date) => (date ? date.toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' }) : '');

function took(job) {
  const start = parse(job.started_at);
  const end = parse(job.finished_at);
  if (!start || !end) return '';
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds} с`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} мин` : `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
}

export function render(view) {
  let alive = true;
  let timer = null;
  let data = null;
  let state = 'all';
  let kind = '';
  let shown = PAGE;
  const page = studioFrame(view, 'jobs');

  const load = guard(async () => {
    data = await get('/api/jobs');
    if (!alive) return;
    draw();
    clearTimeout(timer);
    if (data.active) timer = setTimeout(load, POLL_MS);
  });

  const cancel = (job) => guard(async () => {
    await post(`/api/jobs/${job.id}/cancel`);
    load();
  });

  function titleLink(job, className = '') {
    const route = jobRoute(job);
    return route
      ? el('a', { class: `job-title ${className}`.trim(), href: route, title: 'Открыть' }, job.title)
      : el('b', { class: className }, job.title);
  }

  function current(job) {
    const percent = Math.round((job.progress || 0) * 100);
    return el('section', { class: 'card-box job-now' },
      el('div', { class: 'job-now-head' },
        el('span', { class: 'job-icon lg' }, icon(jobIcon(job), { className: 'big' })),
        el('div', { class: 'grow' },
          el('div', { class: 'hint' }, `Сейчас · ${JOB_STATUS[job.status]}`),
          titleLink(job, 'job-now-title')),
        el('button', { class: 'ghost', onclick: cancel(job), disabled: job.status === 'cancelling' },
          job.status === 'cancelling' ? 'Останавливается…' : 'Отменить')),
      el('div', { class: 'progress wide thick' }, el('i', { style: { width: `${percent}%` } })),
      el('div', { class: 'row hint' },
        el('span', {}, job.total ? `${job.done} из ${job.total}${job.kind === 'qwen_move' ? ' МБ' : ''}` : 'готовлюсь…'),
        el('span', { class: 'grow' }),
        el('span', {}, `${percent}%`)));
  }

  /** Строка задачи: значок вида, название-ссылка, время и итог, состояние, действия. */
  function row(job) {
    const ok = job.status === 'done';
    const failed = job.status === 'failed';
    const active = ACTIVE.has(job.status);
    const date = parse(job.finished_at || job.started_at || job.created_at);
    const facts = [clock(date)];
    if (job.total && ok) facts.push(`${job.done} из ${job.total}`);
    if (ok && took(job)) facts.push(`за ${took(job)}`);
    const badge = active
      ? el('span', { class: 'job-badge active' }, job.status === 'pending' ? 'в очереди' : `${Math.round((job.progress || 0) * 100)}%`)
      : el('span', { class: `job-badge ${job.status}` }, JOB_STATUS[job.status]);
    const fix = failed ? jobFix(job) : null;
    const actions = [];
    if (active) {
      actions.push(el('button', { class: 'round ghost sm', 'aria-label': `Отменить: ${job.title}`, title: 'Отменить', onclick: cancel(job) }, icon('x')));
    } else if (fix) {
      actions.push(el('a', { class: 'button sm surface', href: fix[1] }, fix[0]));
    } else if (failed || job.status === 'cancelled') {
      actions.push(el('button', { class: 'sm surface', onclick: guard(async () => { await retryJob(job); load(); }) },
        icon('refresh-cw', { size: 14 }), 'Повторить'));
    }
    const route = jobRoute(job);
    if (route) actions.push(el('a', { class: 'button round ghost sm', href: route, 'aria-label': `Открыть: ${job.title}`, title: 'Открыть' }, icon('chevron-right')));
    return el('div', { class: `job-row${failed ? ' failed' : ''}` },
      el('span', { class: `job-kind ${job.status}` }, icon(jobIcon(job), { size: 16 })),
      el('div', { class: 'grow' },
        titleLink(job),
        el('div', { class: 'hint' }, facts.filter(Boolean).join(' · ')),
        failed && job.error ? el('div', { class: 'job-error' }, humanError(job.error)) : null),
      badge,
      actions.length ? el('div', { class: 'job-actions' }, actions) : null);
  }

  function draw() {
    const jobs = data.jobs;
    setStudioJobs(data.active);
    const running = jobs.find((job) => job.status === 'running' || job.status === 'cancelling');
    const kindFilter = KINDS.find(([key]) => key === kind)?.[2];
    const ofKind = jobs.filter((job) => !kindFilter || kindFilter.includes(job.kind));
    const test = STATES.find(([key]) => key === state)[2];
    const filtered = ofKind.filter(test);

    const chips = el('div', { class: 'lib-filters' }, STATES.map(([key, label, match]) => {
      const count = ofKind.filter(match).length;
      if (key !== 'all' && !count && key !== state) return null;
      return el('button', {
        class: 'chip-filter', 'aria-pressed': String(state === key),
        onclick: () => { state = key; shown = PAGE; draw(); },
      }, label, el('span', { class: 'count' }, String(count)));
    }),
    el('span', { class: 'grow' }),
    dropdown({
      label: 'Вид задач', value: kind, className: 'kind-pick',
      options: KINDS.map(([key, label]) => ({ value: key, label })),
      onChange: (value) => { kind = value; shown = PAGE; draw(); },
    }),
    el('button', {
      class: 'ghost sm', disabled: !jobs.some((job) => !ACTIVE.has(job.status)),
      onclick: guard(async () => {
        if (!await confirmAction('Очистить историю задач?', 'Уйдут завершённые, упавшие и отменённые задачи. Те, что в работе, останутся.', 'Очистить')) return;
        const { deleted } = await del('/api/jobs');
        toast(`Убрано записей: ${deleted}`, 'ok');
        load();
      }),
    }, 'Очистить историю'));

    // История по дням; длинная — порциями.
    const visible = filtered.slice(0, shown);
    const groups = [];
    for (const job of visible) {
      const title = ACTIVE.has(job.status) ? 'В работе' : dayTitle(parse(job.finished_at || job.created_at));
      if (!groups.length || groups.at(-1).title !== title) groups.push({ title, jobs: [] });
      groups.at(-1).jobs.push(job);
    }

    page.replaceChildren(
      el('div', { class: 'jobs-column' },
        running ? current(running) : el('section', { class: 'card-box jobs-idle' },
          el('span', { class: 'job-icon' }, icon('circle-check')),
          el('div', {},
            el('b', {}, 'Сейчас ничего не делается'),
            el('div', { class: 'hint' }, 'Очередь хранится в библиотеке: если закрыть приложение посреди озвучки, при следующем запуске она продолжится с того же места.'))),
        chips,
        filtered.length
          ? el('section', { class: 'card-box jobs-list' },
            groups.map((group) => el('div', { class: 'job-group' },
              el('div', { class: 'job-day' }, group.title),
              group.jobs.map(row))),
            filtered.length > shown
              ? el('button', { class: 'surface jobs-more', onclick: () => { shown += PAGE; draw(); } },
                `Показать ещё · осталось ${filtered.length - shown}`)
              : null)
          : el('p', { class: 'hint jobs-empty' }, jobs.length ? 'Под этот фильтр задач нет.' : 'Здесь появятся задачи: озвучка, разметка, экспорт.')));
  }

  const onJobs = () => load();
  window.addEventListener('jobs-changed', onJobs);
  load();
  return () => {
    alive = false;
    clearTimeout(timer);
    window.removeEventListener('jobs-changed', onJobs);
  };
}
