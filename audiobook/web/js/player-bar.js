// Нижний плеер: живёт поверх экранов, поэтому переход в другой раздел не
// прерывает воспроизведение. Раскладка как у музыкальных плееров: слева что
// играет, в центре кнопки (сетка 1fr / auto / 1fr держит их строго по центру
// окна), справа дополнительные действия. Прогресс — полоса по верхнему краю.

import { cover, roleDot, speakerName } from './covers.js';
import * as player from './player.js';
import { el, formatDuration, guard, icon, toast } from './ui.js';

const RATES = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];
const SLEEP_OPTIONS = [
  [15, '15 минут'], [30, '30 минут'], [45, '45 минут'], [60, '1 час'],
  ['chapter', 'До конца главы'], [0, 'Выключить'],
];
const SEEK_KEY_MS = 15_000;

const rateText = (rate) => `${String(rate).replace('.', ',')}×`;

export function mount(host) {
  let popup = null;

  const button = (name, label, extra = {}) =>
    el('button', { class: 'round', 'aria-label': label, title: label, ...extra }, icon(name, { className: 'big' }));

  const progressFill = el('i');
  const progress = el('div', {
    class: 'player-progress', role: 'slider', tabindex: '0', 'aria-label': 'Позиция в главе',
    'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0',
  }, progressFill);

  const art = el('div', { class: 'player-art' });
  const title = el('span', { class: 'player-title' });
  const sub = el('span', { class: 'player-sub' });
  const now = el('a', { class: 'player-now', href: '#/', title: 'Открыть плеер' },
    art, el('span', { class: 'player-text' }, title, sub));

  const rateButton = el('button', { class: 'rate', title: 'Скорость' }, '1×');
  const playButton = el('button', { class: 'play', 'aria-label': 'Пуск', title: 'Пуск/пауза (пробел)' },
    icon('play', { className: 'big filled' }));
  const prevButton = button('skip-back', 'В начало главы, дважды — предыдущая');
  const nextButton = button('skip-forward', 'Следующая глава');
  const backButton = button('rotate-ccw', 'Назад 15 секунд');
  const forwardButton = button('rotate-cw', 'Вперёд 30 секунд');
  const sleepButton = el('button', { class: 'round sleep', title: 'Таймер сна', 'aria-label': 'Таймер сна' },
    icon('moon', { className: 'big' }));

  const time = el('span', { class: 'player-time' }, '0:00 / 0:00');
  const textLink = el('a', { class: 'button round', 'aria-label': 'Текст главы', title: 'Текст главы' },
    icon('type', { className: 'big' }));
  const queueButton = button('list-music', 'Очередь');
  const volumeButton = button('volume-2', 'Громкость');
  const expandLink = el('a', { class: 'button round', href: '#/', 'aria-label': 'Развернуть плеер', title: 'Развернуть плеер' },
    icon('chevron-up', { className: 'big' }));

  host.replaceChildren(
    progress,
    now,
    el('div', { class: 'player-controls' },
      rateButton, backButton, prevButton, playButton, nextButton, forwardButton, sleepButton),
    el('div', { class: 'player-extra' }, time, textLink, queueButton, volumeButton, expandLink),
  );

  // ---------- всплывающие панели: одна за раз ----------

  function closePopup() {
    popup?.node.remove();
    popup?.anchor.setAttribute('aria-expanded', 'false');
    popup = null;
  }

  function openPopup(anchor, build, className) {
    if (popup?.anchor === anchor) { closePopup(); return; }
    closePopup();
    const node = el('div', { class: `player-pop ${className}` });
    build(node);
    document.body.append(node);
    const rect = anchor.getBoundingClientRect();
    if (!className.includes('queue-pop')) {
      const left = Math.min(innerWidth - node.offsetWidth - 16, Math.max(16, rect.left + rect.width / 2 - node.offsetWidth / 2));
      node.style.left = `${left}px`;
    }
    anchor.setAttribute('aria-expanded', 'true');
    popup = { node, anchor, build };
    node.querySelector('button, input')?.focus();
  }

  const menu = (items) => el('div', { class: 'menu-list', role: 'menu' }, items.map(([label, action, pressed]) =>
    el('button', { role: 'menuitemradio', 'aria-pressed': pressed ? 'true' : 'false', onclick: () => { action(); closePopup(); } }, label)));

  rateButton.onclick = () => openPopup(rateButton, (node) => {
    const current = player.snapshot().rate;
    node.append(menu(RATES.map((value) => [rateText(value), () => player.setRate(value), value === current])));
  }, 'rate-pop');

  sleepButton.onclick = () => openPopup(sleepButton, (node) => {
    node.append(menu(SLEEP_OPTIONS.map(([value, label]) => [label, () => {
      player.setSleepTimer(value);
      toast(value ? `Таймер сна: ${label.toLowerCase()}` : 'Таймер сна выключен', 'ok');
    }, false])));
  }, 'sleep-pop');

  volumeButton.onclick = () => openPopup(volumeButton, (node) => {
    const slider = el('input', {
      type: 'range', min: '0', max: '100', value: String(Math.round(player.snapshot().volume * 100)),
      'aria-label': 'Громкость',
    });
    slider.oninput = () => player.setVolume(Number(slider.value) / 100);
    node.append(icon('volume-2'), slider);
  }, 'volume-pop');

  queueButton.onclick = () => openPopup(queueButton, (node) => drawQueue(node, player.snapshot()), 'queue-pop');

  function drawQueue(node, state) {
    node.replaceChildren(
      el('div', { class: 'queue-head' }, 'Очередь'),
      state.queue.length
        ? el('div', {}, state.queue.map((entry, index) => el('div', {
          class: `queue-row${entry.chapter_id === state.entry?.chapter_id ? ' current' : ''}`,
        },
          el('button', {
            class: 'queue-name',
            onclick: guard(() => player.playChapter(entry.chapter_id, { index })),
          }, `${entry.book_title} — ${entry.label}`),
          el('span', { class: 'hint' }, entry.duration_ms ? formatDuration(entry.duration_ms) : ''),
          el('button', {
            class: 'round sm', 'aria-label': 'Выше', title: 'Выше', disabled: index === 0,
            onclick: guard(() => player.moveInQueue(index, index - 1)),
          }, icon('arrow-up')),
          el('button', {
            class: 'round sm', 'aria-label': 'Убрать из очереди', title: 'Убрать из очереди',
            onclick: guard(() => player.removeFromQueue(entry.chapter_id)),
          }, icon('x')))))
        : el('div', { class: 'hint', style: { padding: '8px 10px' } }, 'Очередь пуста.'));
  }

  document.addEventListener('mousedown', (event) => {
    if (popup && !popup.node.contains(event.target) && !popup.anchor.contains(event.target)) closePopup();
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closePopup(); });
  window.addEventListener('hashchange', closePopup);

  // ---------- кнопки ----------

  playButton.onclick = () => player.toggle();
  prevButton.onclick = guard(() => player.previous());
  nextButton.onclick = guard(() => player.next());
  backButton.onclick = () => player.skip(-15000);
  forwardButton.onclick = () => player.skip(30000);

  const seekTo = (event) => {
    const state = player.snapshot();
    if (!state.durationMs) return;
    const rect = progress.getBoundingClientRect();
    player.seek(Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)) * state.durationMs);
  };
  progress.onclick = seekTo;
  progress.onkeydown = (event) => {
    if (event.key === 'ArrowLeft') { event.preventDefault(); player.skip(-SEEK_KEY_MS); }
    if (event.key === 'ArrowRight') { event.preventDefault(); player.skip(SEEK_KEY_MS); }
  };

  // ---------- отрисовка состояния ----------

  let drawnBook = null;
  let lastSpeaker = null;

  player.subscribe((state) => {
    const active = Boolean(state.entry);
    host.hidden = !active;
    document.body.classList.toggle('has-player', active);
    if (!active) { closePopup(); return; }

    if (drawnBook !== state.entry.book_id) {
      drawnBook = state.entry.book_id;
      art.replaceChildren(cover({
        id: state.entry.book_id, title: state.entry.book_title, has_cover: state.entry.has_cover,
        ...('cover_path' in state.entry ? { cover_path: state.entry.cover_path } : {}),
        genres: state.entry.genres, moods: state.entry.moods,
      }, { text: false }));
    }

    // Перерисовываем только то, что поменялось: плеер сообщает о себе
    // несколько раз в секунду, и пересозданная иконка на кадр пропадает.
    const playIcon = state.playing ? 'pause' : 'play';
    if (playButton.dataset.icon !== playIcon) {
      playButton.dataset.icon = playIcon;
      playButton.replaceChildren(icon(playIcon, { className: 'big filled' }));
      playButton.setAttribute('aria-label', state.playing ? 'Пауза' : 'Пуск');
    }
    if (title.textContent !== state.entry.label) title.textContent = state.entry.label;
    textLink.href = `#/chapter/${state.entry.chapter_id}`;

    if (state.segment) lastSpeaker = state.segment;
    sub.classList.toggle('warn', Boolean(state.error));
    const subKey = state.error ? `e:${state.error}` : state.notice ? `n:${state.notice}` : `${state.entry.book_title}|${lastSpeaker?.speaker ?? ''}|${lastSpeaker?.slot ?? ''}`;
    if (sub.dataset.key !== subKey) {
      sub.dataset.key = subKey;
      if (state.error) {
        sub.replaceChildren(state.error);
      } else if (state.notice) {
        sub.replaceChildren(state.entry.book_title, el('span', { 'aria-hidden': 'true' }, '·'), state.notice);
      } else if (lastSpeaker) {
        sub.replaceChildren(state.entry.book_title, el('span', { 'aria-hidden': 'true' }, '·'),
          roleDot(lastSpeaker.slot), lastSpeaker.speaker === 'narrator' ? 'рассказчик' : `говорит ${speakerName(lastSpeaker.speaker)}`);
      } else {
        sub.replaceChildren(state.entry.book_title);
      }
    }

    const share = state.durationMs ? state.positionMs / state.durationMs : 0;
    progressFill.style.width = `${Math.min(100, share * 100)}%`;
    progress.setAttribute('aria-valuenow', String(Math.round(share * 100)));
    progress.setAttribute('aria-valuetext', `${formatDuration(state.positionMs)} из ${formatDuration(state.durationMs)}`);
    time.textContent = `${formatDuration(state.positionMs)} / ${formatDuration(state.durationMs)}`;
    if (rateButton.textContent !== rateText(state.rate)) rateButton.textContent = rateText(state.rate);
    nextButton.disabled = state.index + 1 >= state.queue.length;

    // Таймер сна: кнопка горит, рядом — сколько осталось.
    const sleeping = Boolean(state.sleep);
    sleepButton.classList.toggle('active', sleeping);
    const left = state.sleep?.mode === 'chapter'
      ? 'глава'
      : state.sleepLeftMs !== null ? formatDuration(state.sleepLeftMs) : '';
    const sleepKey = sleeping ? left : '';
    if (sleepButton.dataset.key !== sleepKey || !sleepButton.firstChild) {
      sleepButton.dataset.key = sleepKey;
      sleepButton.replaceChildren(icon('moon', { className: 'big' }), sleeping ? el('span', { class: 'hint' }, left) : '');
      sleepButton.title = sleeping ? `Таймер сна: ${left}` : 'Таймер сна';
    }

    if (popup?.anchor === queueButton) drawQueue(popup.node, state);
  });

  // Пробел — пуск и пауза, ← → — перемотка. Только когда не набираем текст.
  document.addEventListener('keydown', (event) => {
    if (!player.snapshot().entry) return;
    if (event.target.matches?.('input, select, textarea, button, [role="slider"]')) return;
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

  player.loadQueue().catch(() => toast('Не удалось прочитать очередь', 'error'));
}

