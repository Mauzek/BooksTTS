// Статистика: сколько готового аудио и во сколько обошлись платные API.

import { get } from '../api.js';
import { el, formatDuration, formatSpan, guard, icon, plural } from '../ui.js';

const PERIODS = [[0, 'за всё время'], [30, 'за 30 дней'], [7, 'за неделю']];
// Silero считает символы, но денег не стоит: он локальный.
const PAID = { anthropic: 'Anthropic — разметка по ролям', elevenlabs: 'ElevenLabs — синтез' };
const LOCAL = { silero: 'Silero — локально, бесплатно', qwen: 'Qwen3-TTS — локально, бесплатно' };

export function render(view) {
  let alive = true;
  let days = 0;
  const page = el('div', { class: 'page' });
  view.replaceChildren(page);

  const load = guard(async () => {
    const data = await get(`/api/stats?days=${days}`);
    if (alive) draw(data);
  });

  function periodSwitch() {
    return el('div', { class: 'row' }, PERIODS.map(([value, label]) => el('button', {
      class: value === days ? 'primary' : '',
      onclick: () => { days = value; load(); },
    }, label)));
  }

  function tile(title, value, note = '') {
    return el('div', { class: 'tile' },
      el('div', { class: 'tile-value' }, value),
      el('div', { class: 'tile-title' }, title),
      note ? el('div', { class: 'hint' }, note) : null);
  }

  function draw(data) {
    const { audio } = data;
    const usage = data.usage.filter((row) => row.chars || row.input_tokens || row.output_tokens);
    const paid = usage.filter((row) => PAID[row.service]);
    const local = usage.filter((row) => !PAID[row.service]);

    page.replaceChildren(
      el('h1', {}, 'Статистика'),
      el('div', { class: 'tiles' },
        tile('готового аудио', formatSpan(audio.duration_ms),
          `${plural(audio.chapters, 'глава', 'главы', 'глав')} в ${plural(audio.books, 'книге', 'книгах', 'книгах')}`),
        tile('озвучено реплик', `${audio.voiced_segments} из ${audio.segments}`,
          audio.errors ? `с ошибкой: ${audio.errors}` : 'ошибок нет'),
        tile('точная длительность', formatDuration(audio.duration_ms))),

      el('h2', {}, 'Расход платных API'),
      periodSwitch(),
      paid.length
        ? el('div', { class: 'list' }, paid.map(usageRow))
        : el('div', { class: 'placeholder' }, 'Платные API пока не использовались.'),

      local.length ? el('h2', {}, 'Локальный синтез') : null,
      local.length ? el('div', { class: 'list' }, local.map(usageRow)) : null,

      data.usage_by_book.length ? el('h2', {}, 'По книгам') : null,
      data.usage_by_book.length
        ? el('div', { class: 'list' }, data.usage_by_book.map((row) => el('div', { class: 'list-row' },
          el('span', { class: 'num' }, icon(PAID[row.service] ? 'circle-alert' : 'check')),
          el('div', { class: 'title' },
            el('div', {}, row.title === '—' ? 'вне книги (прослушивание голосов)' : row.title),
            el('div', { class: 'sub' }, PAID[row.service] || LOCAL[row.service] || row.service)),
          el('span', { class: 'hint' },
            `${(row.chars || 0).toLocaleString('ru')} симв.`
            + (row.tokens ? ` · ${row.tokens.toLocaleString('ru')} токенов` : '')))))
        : null,

      el('h2', {}, 'Аудио по книгам'),
      el('div', { class: 'list' }, data.books.filter((b) => b.chapters).map((book) =>
        el('div', { class: 'list-row' },
          el('span', { class: 'num' }, book.voiced ? icon('check') : ''),
          el('div', { class: 'title' },
            el('a', { href: `#/book/${book.id}` }, book.title),
            el('div', { class: 'sub' }, `озвучено ${book.voiced} из ${book.chapters} глав`)),
          el('span', { class: 'hint' }, formatDuration(book.duration_ms))))),
    );
  }

  function usageRow(row) {
    // Своя сетка: в .list-row первый столбец узкий, под номер главы.
    return el('div', { class: 'list-row usage-row' },
      el('div', { class: 'title' },
        el('div', {}, PAID[row.service] || LOCAL[row.service] || row.service),
        el('div', { class: 'sub' }, plural(row.calls, 'вызов', 'вызова', 'вызовов'))),
      el('div', { class: 'row' },
        el('span', { class: 'hint' }, `${(row.chars || 0).toLocaleString('ru')} симв.`),
        (row.input_tokens || row.output_tokens)
          ? el('span', { class: 'hint' },
            `${(row.input_tokens || 0).toLocaleString('ru')} + ${(row.output_tokens || 0).toLocaleString('ru')} токенов`)
          : null));
  }

  load();
  return () => { alive = false; };
}
