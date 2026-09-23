// Глава: текст и звук рядом. Подсветка идёт за воспроизведением, клик по
// абзацу перематывает, клик по строке прокручивает текст.

import { get, post } from '../api.js';
import * as player from '../player.js';
import { el, emit, formatDuration, guard, icon, toast } from '../ui.js';

const NARRATOR = 'narrator';

export function render(view, chapterId) {
  let alive = true;
  let unsubscribe = null;
  let follow = true;
  let data = null;
  let currentId = null;

  const status = el('span', { class: 'editor-status' });
  const bookLink = el('a', { class: 'crumb', href: '#/' }, '…');
  const chapterSelect = el('select', { title: 'Глава' });
  const listenButton = el('button', { class: 'primary' }, icon('play'), 'Слушать');
  const bookButton = el('button', { title: 'Все озвученные главы книги подряд' },
    icon('book-open'), 'Слушать книгу');
  const queueButton = el('button', { title: 'Добавить в очередь' }, icon('list-music'), 'В очередь');
  const followLabel = el('input', { type: 'checkbox', checked: true });
  const textHost = el('div', { class: 'reader-text' }, el('p', { class: 'hint' }, 'Загружаю главу…'));
  const linesHost = el('div', { class: 'reader-lines' });

  view.replaceChildren(el('div', { class: 'editor' },
    el('div', { class: 'editor-bar' },
      bookLink, el('span', { class: 'muted' }, '›'), chapterSelect,
      listenButton, bookButton, queueButton,
      el('label', { class: 'row hint' }, followLabel, 'следовать за чтением'),
      el('a', { class: 'button', href: `#/chapter/${chapterId}/edit` }, icon('type'), 'Разметка'),
      el('span', { class: 'grow' }), status),
    el('div', { class: 'editor-panes' },
      el('section', { class: 'pane' }, el('h2', {}, 'Текст главы'), textHost),
      el('section', { class: 'pane' }, el('h2', {}, 'Реплики'), linesHost))));

  const say = (text, kind = '') => {
    status.textContent = text;
    status.className = `editor-status ${kind}`;
  };

  const load = guard(async () => {
    data = await get(`/api/chapters/${chapterId}`);
    if (!alive) return;
    bookLink.textContent = data.book.title;
    bookLink.href = `#/book/${data.book.id}`;
    chapterSelect.replaceChildren(...data.chapters.map((c) =>
      el('option', { value: c.id, selected: c.id === data.chapter.id }, c.label)));
    emit('active-book', data.book.id);

    const voiced = Boolean(data.chapter.audio_path);
    listenButton.disabled = !voiced;
    bookButton.disabled = !voiced;
    queueButton.disabled = !voiced;
    if (!voiced) {
      say(data.segments.length ? 'глава не озвучена' : 'глава не размечена по ролям', 'error');
    } else {
      say(`${formatDuration(data.chapter.duration_ms)} звука`);
    }
    renderText();
    renderLines(voiced);
  });

  function renderText() {
    const { text } = data.chapter;
    const nodes = [];
    let cursor = 0;
    for (const segment of data.segments) {
      if (segment.char_start === null || segment.char_start === undefined) continue;
      if (segment.char_start > cursor) {
        nodes.push(el('span', { class: 'gap' }, text.slice(cursor, segment.char_start)));
      }
      nodes.push(el('span', {
        class: `seg${segment.speaker === NARRATOR ? ' narr' : ''}`,
        style: segment.speaker === NARRATOR ? null : { color: segment.color },
        dataset: { id: segment.id },
        title: `${segment.speaker} — нажмите, чтобы слушать отсюда`,
        onclick: () => jumpTo(segment),
      }, text.slice(segment.char_start, segment.char_end)));
      cursor = segment.char_end;
    }
    if (cursor < text.length) nodes.push(el('span', { class: 'gap' }, text.slice(cursor)));
    textHost.replaceChildren(...nodes);
  }

  function renderLines(voiced) {
    if (!data.segments.length) {
      linesHost.replaceChildren(el('div', { class: 'placeholder' },
        'Глава не размечена по ролям. Откройте «Разметка» и нажмите «Разметить».'));
      return;
    }
    if (!voiced) {
      const button = el('button', { class: 'primary' }, icon('mic'), 'Озвучить главу');
      button.onclick = guard(async () => {
        const job = await post(`/api/chapters/${chapterId}/synthesize`, {});
        toast(`Задача добавлена: ${job.title}`, 'ok');
        emit('jobs-changed');
      });
      linesHost.replaceChildren(el('div', { class: 'placeholder' },
        el('p', {}, 'Главу ещё не озвучили — слушать нечего.'), button));
      return;
    }
    linesHost.replaceChildren(...data.segments.map((segment) => el('div', {
      class: `line-row${segment.speaker === NARRATOR ? ' narr' : ''}`,
      dataset: { id: segment.id },
      onclick: () => jumpTo(segment),
    },
      el('span', { class: 'line-time' },
        segment.audio_start_ms === null ? '' : formatDuration(segment.audio_start_ms)),
      el('div', {},
        el('div', { class: 'who', style: { color: segment.speaker === NARRATOR ? 'var(--muted)' : segment.color } },
          segment.speaker),
        el('div', { class: 'say' }, segment.text)))));
  }

  const jumpTo = guard(async (segment) => {
    const state = player.snapshot();
    if (state.entry?.chapter_id === chapterId) {
      if (segment.audio_start_ms !== null) player.seekSegment(segment.id);
      return;
    }
    if (!data.chapter.audio_path) return say('глава не озвучена', 'error');
    await player.playChapter(chapterId, { positionMs: segment.audio_start_ms ?? 0 });
  });

  listenButton.onclick = guard(async () => {
    const state = player.snapshot();
    if (state.entry?.chapter_id === chapterId) return player.toggle();
    await player.playChapter(chapterId);
  });

  bookButton.onclick = guard(async () => {
    await player.playBook(data.book.id, { fromChapter: chapterId });
    toast('Играет книга подряд', 'ok');
  });

  queueButton.onclick = guard(async () => {
    await player.enqueue({
      chapter_id: chapterId, book_id: data.book.id, book_title: data.book.title,
      label: data.chapter.label, duration_ms: data.chapter.duration_ms,
    });
    toast('Добавлено в очередь', 'ok');
  });

  followLabel.onchange = () => { follow = followLabel.checked; };
  chapterSelect.onchange = () => { location.hash = `#/chapter/${chapterSelect.value}`; };

  // ---------- подсветка по ходу воспроизведения ----------

  unsubscribe = player.subscribe((state) => {
    if (!alive || !data) return;
    const playingHere = state.entry?.chapter_id === chapterId;
    const paused = !(playingHere && state.playing);
    listenButton.replaceChildren(icon(paused ? 'play' : 'pause'), paused ? 'Слушать' : 'Пауза');

    const id = playingHere ? state.segment?.id ?? null : null;
    if (id === currentId) return;
    currentId = id;
    for (const node of view.querySelectorAll('.seg.now, .line-row.now')) node.classList.remove('now');
    if (id === null) return;
    const span = textHost.querySelector(`.seg[data-id="${id}"]`);
    const row = linesHost.querySelector(`.line-row[data-id="${id}"]`);
    span?.classList.add('now');
    row?.classList.add('now');
    if (follow) {
      span?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      row?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  });

  const onJobs = () => load();
  window.addEventListener('jobs-changed', onJobs);
  load();

  return () => {
    alive = false;
    unsubscribe?.();
    window.removeEventListener('jobs-changed', onJobs);
  };
}
