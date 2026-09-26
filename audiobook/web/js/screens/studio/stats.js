// Студия — статистика: сколько звука готово и во что обошёлся разбор по ролям.

import { get } from '../../api.js';
import { el, formatSpan, guard, plural } from '../../ui.js';
import { setStudioJobs, studioFrame } from './common.js';

const PERIODS = [[0, 'Всё время'], [30, '30 дней'], [7, 'Неделя']];
const SERVICES = {
  silero: ['Silero', 'бесплатно'],
  qwen: ['Qwen3-TTS', 'бесплатно'],
  elevenlabs: ['ElevenLabs', 'по тарифу'],
  anthropic: ['Claude · разбор', 'по тарифу'],
};
const MAX_BARS = 8;

const num = (value) => (value || 0).toLocaleString('ru');
function compact(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace('.', ',')} млн`;
  if (value >= 10_000) return `${Math.round(value / 1000)} тыс.`;
  return num(value);
}

export function render(view) {
  let alive = true;
  let days = 0;
  const page = studioFrame(view, 'stats');

  const load = guard(async () => {
    const [data, jobs] = await Promise.all([get(`/api/stats?days=${days}`), get('/api/jobs?active=true').catch(() => ({ active: 0 }))]);
    if (alive) draw(data, jobs.active);
  });

  const tile = (label, value, note) => el('div', { class: 'stat-tile' },
    el('span', { class: 'hint' }, label), el('b', {}, value), el('span', { class: 'hint' }, note));

  function draw(data, activeJobs) {
    const { audio } = data;
    const byService = Object.fromEntries(data.usage.map((row) => [row.service, row]));
    const claude = byService.anthropic || {};
    const eleven = byService.elevenlabs || {};
    const tokens = (claude.input_tokens || 0) + (claude.output_tokens || 0);

    const books = data.books.filter((book) => book.duration_ms).sort((a, b) => b.duration_ms - a.duration_ms).slice(0, MAX_BARS);
    const longest = books[0]?.duration_ms || 1;

    const period = el('div', { class: 'switcher' }, PERIODS.map(([value, label]) => el('button', {
      'aria-pressed': String(value === days), onclick: () => { days = value; load(); },
    }, label)));

    const services = Object.keys(SERVICES).filter((key) => byService[key]);
    setStudioJobs(activeJobs);
    page.replaceChildren(
      el('div', { class: 'stat-tiles' },
        tile('Озвучено', formatSpan(audio.duration_ms),
          `${plural(audio.books, 'книга', 'книги', 'книг')}, ${plural(audio.chapters, 'глава', 'главы', 'глав')}`),
        tile('Реплик озвучено', `${num(audio.voiced_segments)} из ${num(audio.segments)}`,
          audio.errors ? `не вышло: ${audio.errors}` : 'без ошибок'),
        tile('Разбор по ролям', tokens ? `${compact(tokens)} токенов` : '—',
          claude.calls ? `${plural(claude.calls, 'обращение', 'обращения', 'обращений')} к Claude` : 'Claude ещё не звали'),
        tile('ElevenLabs', `${compact(eleven.chars || 0)} знаков`,
          eleven.chars ? 'платный облачный синтез' : 'платный синтез не использовался')),
      el('div', { class: 'stat-panels' },
        el('section', { class: 'card-box' },
          el('h2', {}, 'Часы звука по книгам'),
          books.length
            ? el('div', { class: 'bars', role: 'list' }, books.map((book) => el('a', {
              class: 'bar-row', role: 'listitem', href: `#/book/${book.id}`,
              title: `${book.title}: ${formatSpan(book.duration_ms)}, озвучено ${book.voiced} из ${book.chapters} глав`,
            },
              el('span', { class: 'bar-label' }, book.title),
              el('span', { class: 'bar-track' },
                el('i', { style: { width: `${Math.max(1.5, (book.duration_ms / longest) * 100)}%` } }),
                el('span', { class: 'bar-value' }, formatSpan(book.duration_ms))))))
            : el('p', { class: 'hint' }, 'Готового звука пока нет.')),
        el('section', { class: 'card-box' },
          el('div', { class: 'section-head' }, el('h2', {}, 'Расход'), el('span', { class: 'grow' }), period),
          services.length
            ? el('table', { class: 'usage-table' },
              el('thead', {}, el('tr', {}, ['Сервис', 'Обращений', 'Знаков', 'Токенов', 'Цена'].map((h) => el('th', {}, h)))),
              el('tbody', {}, services.map((key) => {
                const row = byService[key];
                return el('tr', {},
                  el('td', {}, SERVICES[key][0]),
                  el('td', {}, num(row.calls)),
                  el('td', {}, compact(row.chars || 0)),
                  el('td', {}, row.input_tokens || row.output_tokens ? compact((row.input_tokens || 0) + (row.output_tokens || 0)) : '—'),
                  el('td', { class: 'muted' }, SERVICES[key][1]));
              })))
            : el('p', { class: 'hint' }, 'За этот период ничего не тратилось.'))));
  }

  load();
  return () => { alive = false; };
}
