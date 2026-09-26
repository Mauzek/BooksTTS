// Плеер: одна дорожка на всё приложение.
//
// Что где живёт: очередь и позиция по книгам — в базе (переживают перезапуск),
// скорость и пропуск тишины — в localStorage (это привычка, а не данные).

import { get, put } from './api.js';

const SETTINGS_KEY = 'booktts-player';
const SAVE_EVERY_MS = 5000;
const SILENCE_EPSILON_MS = 120;  // короче — не пауза, а дыхание внутри реплики

const audio = new Audio();
audio.preload = 'metadata';

const listeners = new Set();
const state = {
  entry: null,       // что играет: {chapter_id, book_id, book_title, label, duration_ms}
  segments: [],      // реплики с раскладкой по времени
  queue: [],         // очередь глав
  index: -1,         // место в очереди
  rate: 1,
  volume: 1,
  skipSilence: false,
  error: '',
  notice: '',        // «Книга дослушана» — плеер остановился в конце книги
  sleep: null,       // таймер сна: {mode: 'time', endsAt} или {mode: 'chapter'}
};

const FADE_MS = 10_000;  // последние секунды перед сном громкость плавно уходит
let sleepTimer = null;

function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    state.rate = Number(saved.rate) || 1;
    state.volume = saved.volume === undefined ? 1 : Math.min(1, Math.max(0, Number(saved.volume) || 0));
    state.skipSilence = Boolean(saved.skipSilence);
  } catch { /* настройки плеера — не данные, можно и без них */ }
}

function saveSettings() {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({
      rate: state.rate, volume: state.volume, skipSilence: state.skipSilence,
    }));
  } catch { /* приватный режим */ }
}

loadSettings();
audio.playbackRate = state.rate;
audio.volume = state.volume;

// ---------- подписка ----------

export function subscribe(listener) {
  listeners.add(listener);
  listener(snapshot());
  return () => listeners.delete(listener);
}

function publish() {
  const view = snapshot();
  listeners.forEach((listener) => listener(view));
}

export function snapshot() {
  return {
    ...state,
    playing: Boolean(state.entry) && !audio.paused,
    positionMs: audio.currentTime * 1000,
    durationMs: (audio.duration || 0) * 1000 || state.entry?.duration_ms || 0,
    segment: currentSegment(),
    sleepLeftMs: state.sleep?.mode === 'time' ? Math.max(0, state.sleep.endsAt - Date.now()) : null,
  };
}

// ---------- таймер сна ----------

function clearSleep() {
  clearTimeout(sleepTimer);
  sleepTimer = null;
  state.sleep = null;
  audio.volume = state.volume;
}

/** Уснуть через ``minutes`` минут; ``'chapter'`` — в конце главы; 0 — выключить. */
export function setSleepTimer(minutes) {
  clearSleep();
  if (minutes === 'chapter') {
    state.sleep = { mode: 'chapter' };
  } else if (Number(minutes) > 0) {
    const duration = Number(minutes) * 60_000;
    state.sleep = { mode: 'time', endsAt: Date.now() + duration };
    sleepTimer = setTimeout(() => {
      audio.pause();
      clearSleep();
      publish();
    }, duration);
  }
  publish();
}

function fadeBeforeSleep() {
  if (state.sleep?.mode !== 'time') return;
  const left = state.sleep.endsAt - Date.now();
  // Затухание — доля от выбранной громкости, а не замена ей.
  audio.volume = state.volume * (left < FADE_MS ? Math.max(0, left / FADE_MS) : 1);
}

export function currentSegment() {
  const position = audio.currentTime * 1000;
  return state.segments.find((s) => position >= s.audio_start_ms && position < s.audio_end_ms) || null;
}

// ---------- воспроизведение ----------

export async function playChapter(chapterId, { positionMs = null, queue = null, index = null } = {}) {
  if (queue) {
    state.queue = queue;
    state.index = index ?? queue.findIndex((item) => item.chapter_id === chapterId);
  }
  const data = await get(`/api/chapters/${chapterId}`);
  // Глава не из текущей очереди — очередь становится её книгой. Иначе после
  // неё заиграла бы глава совсем другой книги, оставшейся в очереди.
  if (!queue) {
    const at = state.queue.findIndex((item) => item.chapter_id === chapterId);
    if (at >= 0) {
      state.index = at;
    } else {
      const book = await get(`/api/books/${data.book.id}/playback`).catch(() => null);
      const ready = book ? book.chapters.filter((c) => c.ready) : [];
      if (ready.length) {
        await setQueue(ready);
        state.index = ready.findIndex((item) => item.chapter_id === chapterId);
      }
    }
  }
  state.notice = '';
  if (!data.chapter.audio_path) {
    state.error = 'глава ещё не озвучена';
    publish();
    throw new Error(state.error);
  }
  state.error = '';
  state.entry = {
    chapter_id: chapterId,
    book_id: data.book.id,
    book_title: data.book.title,
    book_author: data.book.author,
    has_cover: Boolean(data.book.cover_path),
    cover_path: data.book.cover_path ?? null,
    genres: data.book.genres || [],
    moods: data.book.moods || [],
    label: data.chapter.label,
    duration_ms: data.chapter.duration_ms,
  };
  state.segments = data.segments
    .filter((s) => s.audio_start_ms !== null && s.audio_end_ms !== null)
    .map((s) => ({ id: s.id, speaker: s.speaker, text: s.text, color: s.color, slot: s.slot, emotion: s.emotion,
                   audio_start_ms: s.audio_start_ms, audio_end_ms: s.audio_end_ms }));

  let start = positionMs;
  if (start === null) {
    const saved = await get(`/api/books/${data.book.id}/playback`).catch(() => null);
    const mark = saved?.playback;
    start = mark && mark.chapter_id === chapterId ? mark.position_ms : 0;
  }

  audio.src = `/api/chapters/${chapterId}/audio`;
  audio.playbackRate = state.rate;
  audio.currentTime = Math.max(0, start) / 1000;
  await audio.play();
  updateMediaSession();
  publish();
  return state.entry;
}

/** Слушать книгу подряд: очередь — её озвученные главы. */
export async function playBook(bookId, { fromChapter = null } = {}) {
  const data = await get(`/api/books/${bookId}/playback`);
  const ready = data.chapters.filter((c) => c.ready);
  if (!ready.length) throw new Error('в книге пока нет озвученных глав');
  await setQueue(ready);
  const target = fromChapter || data.playback?.chapter_id || ready[0].chapter_id;
  const index = Math.max(0, ready.findIndex((c) => c.chapter_id === target));
  return playChapter(ready[index].chapter_id, { queue: ready, index });
}

export function toggle() {
  if (!state.entry) return;
  if (audio.paused) audio.play(); else audio.pause();
  publish();
}

export function pause() {
  audio.pause();
  publish();
}

export function seek(positionMs) {
  if (!state.entry) return;
  audio.currentTime = Math.max(0, positionMs) / 1000;
  publish();
}

export function seekSegment(segmentId) {
  const segment = state.segments.find((s) => s.id === segmentId);
  if (segment) seek(segment.audio_start_ms);
}

export function skip(deltaMs) {
  seek(audio.currentTime * 1000 + deltaMs);
}

export async function next() {
  if (state.index + 1 >= state.queue.length) {
    pause();
    return false;
  }
  state.index += 1;
  await playChapter(state.queue[state.index].chapter_id);
  return true;
}

export async function previous() {
  // Как в плеерах: сначала к началу главы, и только потом к предыдущей.
  if (audio.currentTime > 3 || state.index <= 0) {
    seek(0);
    return true;
  }
  state.index -= 1;
  await playChapter(state.queue[state.index].chapter_id);
  return true;
}

export function setRate(rate) {
  state.rate = Math.min(3, Math.max(0.5, Number(rate) || 1));
  audio.playbackRate = state.rate;
  saveSettings();
  publish();
}

export function setVolume(volume) {
  state.volume = Math.min(1, Math.max(0, Number(volume) || 0));
  audio.volume = state.volume;
  saveSettings();
  publish();
}

export function setSkipSilence(enabled) {
  state.skipSilence = Boolean(enabled);
  saveSettings();
  publish();
}

// ---------- очередь ----------

export async function loadQueue() {
  const data = await get('/api/queue').catch(() => ({ queue: [] }));
  state.queue = data.queue;
  if (state.entry) {
    state.index = state.queue.findIndex((item) => item.chapter_id === state.entry.chapter_id);
  }
  publish();
  return state.queue;
}

export async function setQueue(entries) {
  state.queue = entries;
  await put('/api/queue', { chapter_ids: entries.map((item) => item.chapter_id) });
  publish();
  return state.queue;
}

export async function removeFromQueue(chapterId) {
  await setQueue(state.queue.filter((item) => item.chapter_id !== chapterId));
  if (state.entry) {
    state.index = state.queue.findIndex((item) => item.chapter_id === state.entry.chapter_id);
  }
}

export async function moveInQueue(from, to) {
  const entries = [...state.queue];
  const [moved] = entries.splice(from, 1);
  entries.splice(Math.max(0, Math.min(to, entries.length)), 0, moved);
  await setQueue(entries);
}

export async function enqueue(entry) {
  if (state.queue.some((item) => item.chapter_id === entry.chapter_id)) return state.queue;
  return setQueue([...state.queue, entry]);
}

// ---------- события дорожки ----------

let lastSaved = 0;

function savePosition(force = false) {
  if (!state.entry) return;
  const now = Date.now();
  if (!force && now - lastSaved < SAVE_EVERY_MS) return;
  lastSaved = now;
  put(`/api/books/${state.entry.book_id}/playback`, {
    chapter_id: state.entry.chapter_id,
    position_ms: Math.round(audio.currentTime * 1000),
  }).catch(() => { /* позиция — не повод шуметь */ });
}

/** Пропуск пауз: молчание между репликами нам известно точно, искать его не нужно. */
function skipSilenceIfNeeded() {
  if (!state.skipSilence || !state.segments.length) return;
  const position = audio.currentTime * 1000;
  const inside = state.segments.some(
    (s) => position >= s.audio_start_ms - SILENCE_EPSILON_MS && position < s.audio_end_ms,
  );
  if (inside) return;
  const upcoming = state.segments.find((s) => s.audio_start_ms > position);
  if (upcoming && upcoming.audio_start_ms - position > SILENCE_EPSILON_MS) {
    audio.currentTime = upcoming.audio_start_ms / 1000;
  }
}

audio.addEventListener('timeupdate', () => {
  skipSilenceIfNeeded();
  fadeBeforeSleep();
  savePosition();
  publish();
});
audio.addEventListener('play', () => { state.notice = ''; updateMediaSession(); publish(); });
audio.addEventListener('pause', () => { savePosition(true); publish(); });
audio.addEventListener('ended', () => {
  savePosition(true);
  if (state.sleep?.mode === 'chapter') {
    // «До конца главы»: следующую не начинаем.
    clearSleep();
    publish();
    return;
  }
  // Сами переходим только к главе той же книги: закончилась книга — тишина,
  // а не чужая книга из очереди. Дальше — кнопкой «Следующая», если хочется.
  const upcoming = state.queue[state.index + 1];
  if (!upcoming || upcoming.book_id !== state.entry?.book_id) {
    state.notice = upcoming ? 'Книга дослушана' : 'Книга дослушана до конца';
    publish();
    return;
  }
  next().catch(() => { /* очередь кончилась */ });
});
audio.addEventListener('error', () => {
  state.error = 'не удалось воспроизвести файл главы';
  publish();
});
window.addEventListener('beforeunload', () => savePosition(true));

// ---------- системные медиа-клавиши ----------

function updateMediaSession() {
  if (!('mediaSession' in navigator) || !state.entry) return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: state.entry.label,
      artist: state.entry.book_title,
      album: 'BookTTS',
    });
    const handlers = {
      play: () => audio.play(),
      pause: () => audio.pause(),
      nexttrack: () => next(),
      previoustrack: () => previous(),
      seekbackward: () => skip(-15000),
      seekforward: () => skip(30000),
    };
    for (const [action, handler] of Object.entries(handlers)) {
      try {
        navigator.mediaSession.setActionHandler(action, handler);
      } catch { /* не все действия поддерживаются */ }
    }
  } catch { /* MediaSession может отсутствовать */ }
}
