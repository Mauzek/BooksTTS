// Что сейчас в очереди задач — для всех экранов сразу.
//
// Каркас приложения опрашивает очередь (app.js) и складывает активные задачи
// сюда. Экраны по ним решают, показывать ли «Озвучить»: главу, которая уже
// озвучивается или ждёт очереди, второй раз ставить незачем.

import { el, icon } from './ui.js';

let jobs = [];
const listeners = new Set();

export function setActiveJobs(list) {
  jobs = Array.isArray(list) ? list : [];
  for (const listener of listeners) listener(jobs);
}

export const activeJobs = () => jobs;

/** Подписаться; вернёт отписку. Сразу вызывается с текущим состоянием. */
export function watchActiveJobs(listener) {
  listeners.add(listener);
  listener(jobs);
  return () => listeners.delete(listener);
}

const SYNTH = new Set(['synthesis', 'book']);

/** Озвучка главы в очереди: своя задача главы или задача всей книги. */
export function synthJobForChapter(chapterId, bookId) {
  return jobs.find((job) => job.kind === 'synthesis' && job.target_id === chapterId)
    || jobs.find((job) => job.kind === 'book' && job.target_id === bookId)
    || null;
}

/** Любая озвучка книги: всей книги или отдельной её главы. */
export function synthJobForBook(bookId) {
  return jobs.find((job) => job.kind === 'book' && job.target_id === bookId)
    || jobs.find((job) => SYNTH.has(job.kind) && job.book_id === bookId)
    || null;
}

export function markupJobForChapter(chapterId, bookId) {
  return jobs.find((job) => job.kind === 'markup' && job.target_id === chapterId)
    || jobs.find((job) => job.kind === 'markup_book' && job.target_id === bookId)
    || null;
}

/** Подпись состояния: «В очереди», «Озвучивается 40%», «Останавливается». */
export function jobLabel(job, verb = 'Озвучивается') {
  if (!job) return '';
  if (job.status === 'pending') return 'В очереди';
  if (job.status === 'cancelling') return 'Останавливается';
  const percent = Math.round((job.progress || 0) * 100);
  return percent ? `${verb} ${percent}%` : `${verb}…`;
}

/** Плашка «идёт работа» вместо кнопки — ведёт к задачам. */
export function busyPill(job, verb) {
  return el('a', {
    class: `busy-pill${job.status === 'pending' ? ' waiting' : ''}`, href: '#/studio/jobs',
    title: `${job.title} — открыть задачи`,
  }, job.status === 'pending' ? icon('list-music', { size: 16 }) : el('span', { class: 'spinner', 'aria-hidden': 'true' }),
  jobLabel(job, verb));
}
