// Голоса ролей там, где они понадобились: в разметке, перед озвучкой.
// Раньше за голосом нужно было идти в студию — теперь его выбирают на месте.

import { get, post, put } from './api.js';
import { avatar, speakerName } from './covers.js';
import { dropdown, el, emit, humanError, icon, plural } from './ui.js';

const CATALOG_TTL_MS = 30_000;
let catalog = null;
let catalogAt = 0;

/** Каталог голосов и движков. Кешируется ненадолго: он меняется редко. */
export async function voiceCatalog() {
  if (!catalog || Date.now() - catalogAt > CATALOG_TTL_MS) {
    const [voices, engines] = await Promise.all([
      get('/api/voices'),
      get('/api/engines').catch(() => ({ engines: [] })),
    ]);
    catalog = { voices: voices.voices, engines: engines.engines };
    catalogAt = Date.now();
  }
  return catalog;
}

/** Назначить роли голос, не трогая её темп, тон и громкость. */
export function setVoice(bookId, speaker, voiceId) {
  return put(`/api/books/${bookId}/cast`, {
    speaker: speaker.name,
    voice_id: voiceId,
    rate: speaker.cast?.rate ?? 1, pitch: speaker.cast?.pitch ?? 1, volume: speaker.cast?.volume ?? 1,
  });
}

/** Выпадающий список голосов, сгруппированный по движкам. */
export function voiceSelect(speaker, { voices, engines }, onChange, { className = 'voice-select' } = {}) {
  const current = speaker.voice?.id ?? speaker.cast?.voice_id ?? null;
  const options = current ? [{ value: '', label: '— снять голос —' }] : [];
  for (const engine of engines) {
    for (const voice of voices.filter((v) => v.engine === engine.name && (v.available || v.id === current))) {
      options.push({
        value: voice.id, label: voice.label, group: engine.title || engine.name,
        hint: voice.gender ? ` · ${voice.gender === 'м' ? 'мужской' : voice.gender === 'ж' ? 'женский' : voice.gender}` : '',
      });
    }
  }
  return dropdown({
    options, value: current, placeholder: 'выберите голос…', className,
    label: `Голос для роли ${speakerName(speaker.name)}`,
    onChange: (value) => onChange(value === '' || value === null ? null : Number(value)),
  });
}

/** Движок, которым уже читают книгу: им и подбирать недостающие голоса. */
function mainEngine(speakers) {
  const counts = new Map();
  for (const speaker of speakers) {
    if (speaker.voice?.engine) counts.set(speaker.voice.engine, (counts.get(speaker.voice.engine) || 0) + (speaker.count || 1));
  }
  return [...counts].sort((a, b) => b[1] - a[1])[0]?.[0] || 'silero';
}

/**
 * Перед озвучкой: у всех ли ролей есть голос. Нет — окно, где голос выбирают
 * тут же или подбирают автоматически. true — можно ставить в очередь.
 * chapterId — проверить только роли этой главы.
 */
export async function ensureVoices(bookId, { chapterId = null } = {}) {
  const [cast, missing] = await Promise.all([
    get(`/api/books/${bookId}/cast`),
    chapterId
      ? get(`/api/chapters/${chapterId}/checks`).then((data) => data.findings
        .filter((f) => f.kind === 'no_voice').map((f) => ({ speaker: f.speaker, lines: f.segment_ids.length })))
      : get(`/api/books/${bookId}/readiness`).then((data) => [
        ...data.missing_voice,
        ...data.unavailable_voice.map((u) => ({ speaker: u.speaker, lines: 0, gone: u.voice })),
      ]),
  ]);
  if (!missing.length) return true;
  const known = new Map(cast.speakers.map((s) => [s.name, s]));
  return fixVoicesDialog(bookId, missing.map((m) => ({ ...m, info: known.get(m.speaker) || { name: m.speaker } })), cast.speakers);
}

function fixVoicesDialog(bookId, missing, speakers) {
  return new Promise((resolve) => {
    const dialog = el('dialog', { class: 'voices-dialog' });
    const close = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    const chosen = new Map();
    const error = el('p', { class: 'form-error', role: 'alert', hidden: true });
    const go = el('button', { class: 'primary lg', disabled: true }, icon('mic'), 'Озвучить');
    const rows = el('div', { class: 'voices-rows' }, el('p', { class: 'hint' }, 'Загружаю голоса…'));
    const auto = el('button', { class: 'surface lg' }, icon('wand-sparkles'), 'Подобрать сами');

    const refresh = () => {
      go.disabled = missing.some((m) => !chosen.get(m.speaker));
    };

    voiceCatalog().then((voices) => {
      rows.replaceChildren(...missing.map((m) => el('div', { class: 'voices-row' },
        avatar(m.speaker, m.info.slot ?? 0, 'md'),
        el('div', { class: 'grow' },
          el('b', {}, speakerName(m.speaker)),
          el('span', { class: 'hint' }, m.gone ? `голос ${m.gone} пропал из каталога`
            : m.lines ? plural(m.lines, 'реплика', 'реплики', 'реплик') : 'нет голоса')),
        voiceSelect(m.info, voices, async (voiceId) => {
          error.hidden = true;
          try {
            await setVoice(bookId, m.info, voiceId);
            chosen.set(m.speaker, voiceId);
          } catch (failure) {
            error.textContent = humanError(failure);
            error.hidden = false;
          }
          refresh();
        }))));
    }).catch((failure) => { rows.replaceChildren(el('p', { class: 'form-error' }, humanError(failure))); });

    auto.onclick = async () => {
      auto.disabled = true;
      try {
        await post(`/api/books/${bookId}/cast/auto`, { engine: mainEngine(speakers) });
        emit('library-changed');
        close(true);
      } catch (failure) {
        error.textContent = humanError(failure);
        error.hidden = false;
        auto.disabled = false;
      }
    };
    go.onclick = () => { emit('library-changed'); close(true); };

    dialog.append(
      el('div', { class: 'dialog-head' },
        el('div', { class: 'grow' },
          el('h3', {}, missing.length === 1 ? `У роли «${speakerName(missing[0].speaker)}» нет голоса` : 'Не у всех ролей есть голос'),
          el('span', { class: 'hint' }, 'Без голоса реплики не озвучатся. Выберите голос здесь же — или подберём сами по полу персонажа.')),
        el('button', { class: 'round', 'aria-label': 'Закрыть', onclick: () => close(false) }, icon('x'))),
      rows, error,
      el('div', { class: 'buttons' },
        el('button', { class: 'ghost lg', onclick: () => close(false) }, 'Отмена'),
        auto, go));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(false); });
    document.body.append(dialog);
    dialog.showModal();
  });
}
