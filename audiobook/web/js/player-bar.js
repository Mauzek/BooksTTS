// Панель плеера внизу окна: живёт поверх экранов, поэтому переход в другой
// раздел не прерывает воспроизведение.

import * as player from './player.js';
import { el, formatDuration, guard, icon, toast } from './ui.js';

const RATES = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];
const SLEEP_OPTIONS = [
  [15, '15 минут'], [30, '30 минут'], [45, '45 минут'], [60, '1 час'],
  ['chapter', 'До конца главы'], [0, 'Выключить'],
];
const SEEK_KEY_MS = 15_000;

export function mount(host) {
  let seeking = false;

  const playButton = el('button', { class: 'primary icon-button', title: 'Пуск/пауза (пробел)' },
    icon('play', { className: 'big' }));
  const prevButton = el('button', { class: 'icon-button', title: 'В начало главы, дважды — предыдущая' },
    icon('skip-back'));
  const nextButton = el('button', { class: 'icon-button', title: 'Следующая глава' }, icon('skip-forward'));
  const backButton = el('button', { class: 'icon-button', title: 'Назад 15 секунд' }, icon('rotate-ccw'));
  const forwardButton = el('button', { class: 'icon-button', title: 'Вперёд 30 секунд' }, icon('rotate-cw'));
  const title = el('a', { class: 'player-title', href: '#/' });
  const subtitle = el('div', { class: 'hint' });
  const seek = el('input', { type: 'range', min: '0', max: '1000', value: '0', class: 'player-seek' });
  const time = el('span', { class: 'hint player-time' }, '0:00 / 0:00');
  const rate = el('select', { title: 'Скорость' },
    RATES.map((value) => el('option', { value: String(value) }, `${value}×`.replace('.', ','))));
  const silence = el('input', { type: 'checkbox', title: 'Пропускать паузы между репликами' });
  const queueButton = el('button', { class: 'icon-button', title: 'Очередь' }, icon('list-music'));
  const queuePanel = el('div', { class: 'queue-panel', hidden: true });
  const sleepButton = el('button', { class: 'icon-button sleep-button', title: 'Таймер сна' }, icon('moon'));
  const sleepMenu = el('div', { class: 'menu-pop sleep-menu', hidden: true },
    SLEEP_OPTIONS.map(([value, label]) => el('button', {
      class: 'ghost',
      onclick: () => {
        player.setSleepTimer(value);
        sleepMenu.hidden = true;
        toast(value ? `Таймер сна: ${label.toLowerCase()}` : 'Таймер сна выключен', 'ok');
      },
    }, label)));

  host.replaceChildren(
    el('div', { class: 'player-controls' }, prevButton, backButton, playButton, forwardButton, nextButton),
    el('div', { class: 'player-info' }, title, subtitle),
    el('div', { class: 'player-seekbar' }, seek, time),
    el('label', { class: 'row hint' }, silence, 'без пауз'),
    rate, sleepButton, queueButton, queuePanel, sleepMenu,
  );

  sleepButton.onclick = () => {
    sleepMenu.hidden = !sleepMenu.hidden;
    queuePanel.hidden = true;
  };

  playButton.onclick = () => player.toggle();
  prevButton.onclick = guard(() => player.previous());
  nextButton.onclick = guard(() => player.next());
  backButton.onclick = () => player.skip(-15000);
  forwardButton.onclick = () => player.skip(30000);
  rate.onchange = () => player.setRate(Number(rate.value));
  silence.onchange = () => player.setSkipSilence(silence.checked);
  seek.oninput = () => { seeking = true; };
  seek.onchange = () => {
    const state = player.snapshot();
    player.seek((Number(seek.value) / 1000) * state.durationMs);
    seeking = false;
  };
  queueButton.onclick = () => {
    queuePanel.hidden = !queuePanel.hidden;
    if (!queuePanel.hidden) drawQueue(player.snapshot());
  };

  function drawQueue(state) {
    if (queuePanel.hidden) return;
    queuePanel.replaceChildren(
      el('div', { class: 'queue-head' }, 'Очередь воспроизведения'),
      state.queue.length
        ? el('div', {}, state.queue.map((entry, index) => el('div', {
          class: `queue-row${entry.chapter_id === state.entry?.chapter_id ? ' current' : ''}`,
        },
          el('button', {
            class: 'ghost grow queue-name',
            onclick: guard(() => player.playChapter(entry.chapter_id, { index })),
          }, `${entry.book_title} — ${entry.label}`),
          el('span', { class: 'hint' }, entry.duration_ms ? formatDuration(entry.duration_ms) : ''),
          el('button', {
            class: 'ghost icon-button', title: 'Выше',
            disabled: index === 0,
            onclick: guard(() => player.moveInQueue(index, index - 1)),
          }, icon('arrow-up')),
          el('button', {
            class: 'ghost icon-button', title: 'Убрать из очереди',
            onclick: guard(() => player.removeFromQueue(entry.chapter_id)),
          }, icon('x')))))
        : el('div', { class: 'hint', style: { padding: '10px' } }, 'Очередь пуста.'));
  }

  player.subscribe((state) => {
    const active = Boolean(state.entry);
    host.hidden = !active;
    document.body.classList.toggle('has-player', active);
    if (!active) return;

    playButton.replaceChildren(icon(state.playing ? 'pause' : 'play', { className: 'big' }));
    title.textContent = state.entry.label;
    title.href = `#/chapter/${state.entry.chapter_id}`;
    subtitle.textContent = state.error || state.entry.book_title;
    subtitle.classList.toggle('warn', Boolean(state.error));
    if (!seeking && state.durationMs) {
      seek.value = String(Math.round((state.positionMs / state.durationMs) * 1000));
    }
    time.textContent = `${formatDuration(state.positionMs)} / ${formatDuration(state.durationMs)}`;
    rate.value = String(state.rate);
    silence.checked = state.skipSilence;
    nextButton.disabled = state.index + 1 >= state.queue.length;

    // Таймер сна: иконка горит, рядом — сколько осталось.
    const sleeping = Boolean(state.sleep);
    sleepButton.classList.toggle('active', sleeping);
    const label = state.sleep?.mode === 'chapter'
      ? 'до конца главы'
      : state.sleepLeftMs !== null ? formatDuration(state.sleepLeftMs) : '';
    sleepButton.replaceChildren(icon('moon'), sleeping ? el('span', { class: 'sleep-left' }, label) : '');
    sleepButton.title = sleeping ? `Таймер сна: ${label}` : 'Таймер сна';
    drawQueue(state);
  });

  // Пробел — пуск и пауза, ← → — перемотка. Только когда не набираем текст.
  document.addEventListener('keydown', (event) => {
    if (!player.snapshot().entry) return;
    if (event.target.matches?.('input, select, textarea, button')) return;
    if (document.querySelector('dialog[open]')) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.code === 'Space') {
      event.preventDefault();
      player.toggle();
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      player.skip(-SEEK_KEY_MS);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      player.skip(SEEK_KEY_MS);
    }
  });

  document.addEventListener('mousedown', (event) => {
    if (!sleepMenu.hidden && !sleepMenu.contains(event.target) && !sleepButton.contains(event.target)) {
      sleepMenu.hidden = true;
    }
  });

  player.loadQueue().catch(() => toast('Не удалось прочитать очередь', 'error'));
}
