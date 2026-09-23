// Задачи: что делается сейчас, что стоит в очереди, что уже готово.

import { del, get, post } from '../api.js';
import { el, guard, icon, toast } from '../ui.js';

const STATUS = {
  pending: 'в очереди',
  running: 'выполняется',
  cancelling: 'останавливается',
  done: 'готово',
  failed: 'ошибка',
  cancelled: 'отменена',
};
const ACTIVE = new Set(['pending', 'running', 'cancelling']);
const POLL_MS = 1500;

export function render(view) {
  let alive = true;
  let timer = null;
  const page = el('div', { class: 'page' });
  view.replaceChildren(page);

  const load = guard(async () => {
    const data = await get('/api/jobs');
    if (!alive) return;
    draw(data.jobs);
    clearTimeout(timer);
    if (data.active) timer = setTimeout(load, POLL_MS);
  });

  function draw(jobs) {
    const clear = el('button', {
      disabled: !jobs.some((job) => !ACTIVE.has(job.status)),
      onclick: guard(async () => {
        const { deleted } = await del('/api/jobs');
        toast(`Убрано записей: ${deleted}`, 'ok');
        load();
      }),
    }, 'Очистить завершённые');

    page.replaceChildren(
      el('h1', {}, 'Задачи'),
      el('p', { class: 'hint' },
        'Очередь хранится в библиотеке: если закрыть приложение посреди озвучки, ',
        'при следующем запуске она продолжится с того же места.'),
      el('div', { class: 'row' }, clear),
      jobs.length
        ? el('div', { class: 'list' }, jobs.map(row))
        : el('div', { class: 'empty-state' },
          el('div', { class: 'big' }, icon('list-checks', { className: 'huge' })),
          el('p', {}, 'Задач пока нет. Озвучку запускают из книги или из меню папки.')),
    );
  }

  function row(job) {
    const active = ACTIVE.has(job.status);
    const percent = Math.round((job.progress || 0) * 100);
    return el('div', { class: 'list-row job-row' },
      el('div', { class: 'title' },
        el('div', {}, job.title || job.kind),
        el('div', { class: 'sub' },
          STATUS[job.status] || job.status,
          job.total ? ` · ${job.done} из ${job.total}` : '',
          job.error ? ` · ${job.error}` : '')),
      el('div', { class: 'row' },
        active ? el('div', { class: 'progress', title: `${percent}%` },
          el('i', { style: { width: `${percent}%` } })) : null,
        el('span', { class: `job-state ${job.status}` }, active ? `${percent}%` : STATUS[job.status]),
        active ? el('button', {
          onclick: guard(async () => {
            await post(`/api/jobs/${job.id}/cancel`);
            load();
          }),
        }, 'Отменить') : null));
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
