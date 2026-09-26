// Глава — читать вместе: текст идёт за звуком, текущая реплика подсвечена.
// Нажатие на фразу — слушать с этого места.

import { get, post } from '../api.js';
import { avatar, speakerName } from '../covers.js';
import { busyPill, synthJobForChapter, watchActiveJobs } from '../jobs-state.js';
import { ensureVoices } from '../voices.js';
import * as player from '../player.js';
import { el, emit, formatDuration, guard, icon, toast } from '../ui.js';

const NARRATOR = 'narrator';
const PREFS_KEY = 'booktts-reader';
const SIZES = [15, 17, 19, 21, 24, 28];

function loadPrefs() {
  try { return { size: 19, roles: true, follow: true, ...JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') }; } catch { return { size: 19, roles: true, follow: true }; }
}
function savePrefs(prefs) {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* удобство */ }
}

function tint(node, slot) {
  const n = Number(slot) || 0;
  node.style.setProperty('--seg', `var(--c-${n})`);
  node.style.setProperty('--seg-on', `var(--c-${n}-on)`);
  return node;
}

export function render(view, chapterId) {
  let alive = true;
  let data = null;
  let currentId = null;
  const prefs = loadPrefs();

  const backLink = el('a', { class: 'button surface', href: '#/library' }, icon('chevron-left'), '…');
  const meta = el('span', { class: 'hint' });
  const title = el('span', { class: 'markup-title' });
  const smaller = el('button', { class: 'round surface', 'aria-label': 'Мельче', title: 'Мельче' }, 'A−');
  const bigger = el('button', { class: 'round surface', 'aria-label': 'Крупнее', title: 'Крупнее' }, 'A+');
  const rolesToggle = el('button', { class: 'chip-filter' }, 'Кто говорит');
  const followToggle = el('button', { class: 'chip-filter', title: 'Прокручивать текст за звуком' }, 'Следить');
  const queueButton = el('button', { class: 'round surface', 'aria-label': 'Добавить в очередь', title: 'Добавить в очередь' }, icon('list-music'));
  const markupLink = el('a', { class: 'button surface', href: `#/chapter/${chapterId}/edit` }, icon('wand-sparkles'), 'Разметка');
  const listenButton = el('button', { class: 'primary' }, icon('play', { className: 'filled' }), 'Слушать');
  const textHost = el('article', { class: 'reader', 'aria-label': 'Текст главы' }, el('p', { class: 'hint' }, 'Загружаю главу…'));
  const chaptersHost = el('div', { class: 'reader-chapters' });
  const rolesHost = el('div', { class: 'markup-roles' });
  const sideNote = el('div');

  view.replaceChildren(el('div', { class: 'page markup-page' },
    el('div', { class: 'studio-head' },
      backLink,
      el('div', { class: 'markup-heading' }, meta, title),
      el('span', { class: 'grow' }),
      smaller, bigger, rolesToggle, followToggle, markupLink, queueButton, listenButton),
    el('div', { class: 'markup-layout reader-layout' },
      textHost,
      el('aside', { class: 'studio-side' },
        sideNote,
        el('section', { class: 'card-box side-card' }, el('h2', {}, 'Главы'), chaptersHost),
        el('section', { class: 'card-box side-card' }, el('h2', {}, 'Кто говорит'), rolesHost)))));

  function applyPrefs() {
    textHost.style.setProperty('--reader-size', `${prefs.size}px`);
    textHost.classList.toggle('show-roles', prefs.roles);
    rolesToggle.setAttribute('aria-pressed', String(prefs.roles));
    followToggle.setAttribute('aria-pressed', String(prefs.follow));
    smaller.disabled = prefs.size <= SIZES[0];
    bigger.disabled = prefs.size >= SIZES.at(-1);
  }
  const resize = (delta) => {
    const index = Math.max(0, Math.min(SIZES.length - 1, SIZES.indexOf(prefs.size) + delta));
    prefs.size = SIZES[index === -1 ? 2 : index];
    savePrefs(prefs);
    applyPrefs();
  };
  smaller.onclick = () => resize(-1);
  bigger.onclick = () => resize(1);
  rolesToggle.onclick = () => { prefs.roles = !prefs.roles; savePrefs(prefs); applyPrefs(); };
  followToggle.onclick = () => { prefs.follow = !prefs.follow; savePrefs(prefs); applyPrefs(); };

  const load = guard(async () => {
    data = await get(`/api/chapters/${chapterId}`);
    if (!alive) return;
    const index = data.chapters.findIndex((c) => c.id === data.chapter.id);
    backLink.replaceChildren(icon('chevron-left'), data.book.title);
    backLink.href = `#/book/${data.book.id}`;
    meta.textContent = `Глава ${index + 1} из ${data.chapters.length} · читать вместе`;
    title.textContent = data.chapter.title || `Глава ${data.chapter.number}`;
    const voiced = Boolean(data.chapter.audio_path);
    listenButton.disabled = !voiced;
    queueButton.disabled = !voiced;
    sideNote.replaceChildren(voiced ? '' : notVoiced());
    renderText();
    renderChapters();
    renderRoles();
    currentId = null;
  });

  function notVoiced() {
    if (!data.segments.length) {
      return el('section', { class: 'card-box side-card' },
        el('b', {}, 'Глава не разобрана по ролям'),
        el('span', { class: 'hint' }, 'Сначала отметьте, кто что говорит, — потом её можно озвучить.'),
        el('a', { class: 'button primary', href: `#/chapter/${chapterId}/edit` }, icon('wand-sparkles'), 'Открыть разметку'));
    }
    const button = el('button', { class: 'primary' }, icon('mic'), 'Озвучить главу');
    button.onclick = guard(async () => {
      if (!(await ensureVoices(data.book.id, { chapterId }))) return;
      const job = await post(`/api/chapters/${chapterId}/synthesize`, {});
      toast(`Задача добавлена: ${job.title}`, 'ok');
      emit('jobs-changed');
    });
    // Уже в очереди — вместо кнопки плашка с состоянием.
    const host = el('div', { class: 'voice-host' }, button);
    host.idle = button;
    voiceHost = host;
    drawVoiceHost();
    return el('section', { class: 'card-box side-card' },
      el('b', {}, 'Главу ещё не озвучили'), el('span', { class: 'hint' }, 'Читать можно уже сейчас, а слушать — после озвучки.'), host);
  }

  let voiceHost = null;
  function drawVoiceHost() {
    if (!voiceHost || !data) return;
    const job = synthJobForChapter(chapterId, data.book.id);
    const key = job ? `${job.id}:${job.status}:${Math.round((job.progress || 0) * 100)}` : '';
    if (voiceHost.dataset.key === key) return;
    voiceHost.dataset.key = key;
    voiceHost.replaceChildren(job ? busyPill(job) : voiceHost.idle);
  }
  const unwatchJobs = watchActiveJobs(drawVoiceHost);

  function renderText() {
    const { text } = data.chapter;
    const nodes = [];
    let cursor = 0;
    for (const segment of data.segments) {
      if (segment.char_start === null || segment.char_start === undefined) continue;
      if (segment.char_start > cursor) nodes.push(el('span', { class: 'gap' }, text.slice(cursor, segment.char_start)));
      if (segment.speaker !== NARRATOR) {
        nodes.push(tint(el('span', { class: 'seg-mark', 'aria-hidden': 'true' }, speakerName(segment.speaker).charAt(0)), segment.slot));
      }
      nodes.push(tint(el('span', {
        class: `seg${segment.speaker === NARRATOR ? ' narr' : ''}`,
        dataset: { id: segment.id },
        title: `${speakerName(segment.speaker)} — нажмите, чтобы слушать отсюда`,
        onclick: () => jumpTo(segment),
      }, text.slice(segment.char_start, segment.char_end)), segment.slot));
      cursor = segment.char_end;
    }
    if (cursor < text.length) nodes.push(el('span', { class: 'gap' }, text.slice(cursor)));
    textHost.replaceChildren(...nodes);
  }

  function renderChapters() {
    const state = player.snapshot();
    chaptersHost.replaceChildren(...data.chapters.map((chapter, index) => {
      const here = chapter.id === data.chapter.id;
      const playing = state.entry?.chapter_id === chapter.id;
      return el('a', { class: `reader-chapter${here ? ' here' : ''}`, href: `#/chapter/${chapter.id}`, 'aria-current': here ? 'true' : null },
        el('span', { class: `state-dot${playing ? ' ok' : ''}`, 'aria-hidden': 'true' }),
        el('span', { class: 'grow' },
          el('span', { class: 'reader-chapter-name' }, `${index + 1}. ${chapter.title || `Глава ${chapter.number}`}`),
          el('span', { class: 'hint' }, playing ? (state.playing ? 'играет' : 'на паузе')
            : chapter.duration_ms ? formatDuration(chapter.duration_ms) : 'ещё не озвучена')));
    }));
  }

  function renderRoles(speaking = null) {
    rolesHost.replaceChildren(...data.speakers.map((speaker) => el('div', { class: `role-key${speaker.name === speaking ? ' speaking' : ''}` },
      avatar(speaker.name, speaker.slot, 'md'),
      el('span', { class: 'grow' }, speakerName(speaker.name)),
      speaker.name === speaking ? el('span', { class: 'hint' }, 'сейчас') : null)));
  }

  const jumpTo = guard(async (segment) => {
    const state = player.snapshot();
    if (state.entry?.chapter_id === chapterId) {
      if (segment.audio_start_ms !== null) player.seekSegment(segment.id);
      if (!state.playing) player.toggle();
      return;
    }
    if (!data.chapter.audio_path) return toast('Глава ещё не озвучена', 'error');
    await player.playChapter(chapterId, { positionMs: segment.audio_start_ms ?? 0 });
  });

  listenButton.onclick = guard(async () => {
    const state = player.snapshot();
    if (state.entry?.chapter_id === chapterId) return player.toggle();
    await player.playBook(data.book.id, { fromChapter: chapterId });
  });

  queueButton.onclick = guard(async () => {
    await player.enqueue({
      chapter_id: chapterId, book_id: data.book.id, book_title: data.book.title,
      label: data.chapter.label, duration_ms: data.chapter.duration_ms,
    });
    toast('Добавлено в очередь', 'ok');
  });

  // ---------- подсветка по ходу воспроизведения ----------

  let lastPlaying = null;
  const unsubscribe = player.subscribe((state) => {
    if (!alive || !data) return;
    const playingHere = state.entry?.chapter_id === chapterId;
    const paused = !(playingHere && state.playing);
    if (listenButton.dataset.paused !== String(paused)) {
      listenButton.dataset.paused = String(paused);
      listenButton.replaceChildren(icon(paused ? 'play' : 'pause', { className: 'filled' }), paused ? 'Слушать' : 'Пауза');
    }
    if (lastPlaying !== `${state.entry?.chapter_id}:${state.playing}`) {
      lastPlaying = `${state.entry?.chapter_id}:${state.playing}`;
      renderChapters();
    }

    const id = playingHere ? state.segment?.id ?? null : null;
    if (id === currentId) return;
    currentId = id;
    for (const node of textHost.querySelectorAll('.seg.now')) node.classList.remove('now');
    renderRoles(playingHere ? state.segment?.speaker : null);
    if (id === null) return;
    const span = textHost.querySelector(`.seg[data-id="${id}"]`);
    span?.classList.add('now');
    if (prefs.follow) span?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  });

  const onJobs = () => load();
  window.addEventListener('jobs-changed', onJobs);
  applyPrefs();
  load();

  return () => {
    alive = false;
    unwatchJobs();
    unsubscribe();
    window.removeEventListener('jobs-changed', onJobs);
  };
}
