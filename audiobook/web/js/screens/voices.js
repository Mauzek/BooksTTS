// Голоса: каталог движков слева, роли книги справа. Голос переносится на роль
// перетаскиванием или кнопкой «Назначить».

import { get, post, put } from '../api.js';
import { ask, confirmAction, el, guard, icon, plural, toast } from '../ui.js';

const BOOK_KEY = 'booktts-voices-book';
const player = new Audio();

function remember(bookId) {
  try { localStorage.setItem(BOOK_KEY, String(bookId)); } catch { /* не важно */ }
}

function remembered() {
  try { return Number(localStorage.getItem(BOOK_KEY)) || null; } catch { return null; }
}

function flattenBooks(tree) {
  const books = [...tree.books];
  const walk = (folders) => folders.forEach((folder) => {
    books.push(...folder.books);
    walk(folder.folders);
  });
  walk(tree.folders);
  return books;
}

export function render(view, bookId = null) {
  let alive = true;
  // Пришли из карточки книги — открываем её роли, иначе ту, что смотрели последней.
  const state = { voices: [], engines: [], bookId: bookId || remembered(), cast: null, phrase: '' };

  const engineHost = el('div', { class: 'engine-cards' });
  const filters = {
    engine: el('select', { title: 'Движок' }),
    gender: el('select', { title: 'Пол' }),
    search: el('input', { type: 'search', placeholder: 'Имя или метка' }),
  };
  const phrase = el('input', {
    class: 'grow', placeholder: 'Фраза для прослушивания — можно свою',
  });
  const fromBook = el('button', { title: 'Взять реплику из выбранной книги' }, 'Фраза из книги');
  const voiceHost = el('div', { class: 'voice-grid' });
  const bookSelect = el('select', { title: 'Книга' });
  const castHost = el('div', { class: 'cast-list' });
  const castTools = el('div', { class: 'row' });

  const page = el('div', { class: 'voices' },
    el('div', { class: 'voices-main' },
      el('div', { class: 'page' },
        el('h1', {}, 'Голоса'),
        el('h2', {}, 'Движки'),
        engineHost,
        el('h2', {}, 'Каталог'),
        el('div', { class: 'row' }, filters.engine, filters.gender, filters.search),
        el('div', { class: 'row', style: { marginTop: '8px' } }, phrase, fromBook),
        voiceHost)),
    el('aside', { class: 'cast-panel' },
      el('div', { class: 'cast-head' }, el('span', { class: 'grow' }, 'Роли книги'), bookSelect),
      castTools,
      castHost,
      el('p', { class: 'hint' },
        'Перетащите голос из каталога на роль или выберите роль и нажмите «Назначить». ',
        'Темп, высота и громкость — у каждой роли свои.')));
  view.replaceChildren(page);

  // ---------- движки ----------

  const loadEngines = guard(async () => {
    const data = await get('/api/engines');
    if (!alive) return;
    state.engines = data.engines;
    engineHost.replaceChildren(...data.engines.map((engine) => el('div', {
      class: `engine-card ${engine.ready ? 'ok' : 'off'}`,
    },
      el('div', { class: 'row' },
        el('b', { class: 'grow' }, engine.title || engine.name),
        el('span', { class: 'dot-state', title: engine.ready ? 'готов' : 'недоступен' })),
      el('div', { class: 'hint' }, engine.detail || (engine.ready ? 'готов' : 'недоступен')),
      el('div', { class: 'row' },
        el('button', { onclick: () => refresh(engine.name) }, icon('refresh-cw'), 'Обновить каталог'),
        engine.needs_key && !engine.ready
          ? el('a', { class: 'button', href: '#/settings' }, 'Добавить ключ')
          : null))));
    const engines = ['', ...data.engines.map((e) => e.name)];
    filters.engine.replaceChildren(...engines.map((name) =>
      el('option', { value: name, selected: name === filters.engine.value },
        name || 'все движки')));
    if (!state.phrase) {
      state.phrase = data.settings['preview.text'] || '';
      phrase.value = state.phrase;
    }
  });

  const refresh = guard(async (engine) => {
    toast(`Обновляю каталог: ${engine}…`);
    const { report } = await post('/api/engines/refresh', { engine });
    for (const entry of report) {
      if (entry.error) toast(`${entry.engine}: ${entry.error}`, 'error');
      else toast(`${entry.engine}: добавлено ${entry.added}, обновлено ${entry.updated}` +
        (entry.gone ? `, пропало ${entry.gone}` : ''), 'ok');
    }
    await Promise.all([loadVoices(), loadEngines()]);
  });

  // ---------- каталог ----------

  const loadVoices = guard(async () => {
    const params = new URLSearchParams();
    if (filters.engine.value) params.set('engine', filters.engine.value);
    if (filters.gender.value) params.set('gender', filters.gender.value);
    if (filters.search.value.trim()) params.set('q', filters.search.value.trim());
    const { voices } = await get(`/api/voices?${params}`);
    if (!alive) return;
    state.voices = voices;
    if (!voices.length) {
      voiceHost.replaceChildren(el('div', { class: 'placeholder' },
        'Каталог пуст. Нажмите «Обновить каталог» у нужного движка.'));
      return;
    }
    voiceHost.replaceChildren(...voices.map(voiceCard));
  });

  function voiceCard(voice) {
    const play = el('button', { class: 'icon-button', title: 'Прослушать' }, icon('play'));
    play.onclick = guard(async () => {
      play.disabled = true;
      try {
        const result = await post(`/api/voices/${voice.id}/preview`, { text: phrase.value.trim() });
        player.src = result.url;
        await player.play().catch(() => toast('Браузер не дал воспроизвести звук', 'error'));
      } finally {
        play.disabled = false;
      }
    });
    const assign = el('button', { title: 'Назначить выбранной роли' }, 'Назначить');
    assign.onclick = () => assignVoice(voice.id);

    const card = el('div', {
      class: 'voice-card', draggable: 'true', dataset: { voice: voice.id },
      title: voice.tags.join(', '),
    },
      el('div', { class: 'row' },
        el('b', { class: 'grow' }, voice.label),
        voice.gender ? el('span', { class: 'badge' }, voice.gender) : null),
      el('div', { class: 'hint' }, [voice.engine, voice.language, ...voice.tags].filter(Boolean).join(' · ')),
      el('div', { class: 'row' }, play, assign));
    card.addEventListener('dragstart', (event) => {
      event.dataTransfer.setData('application/x-booktts-voice', String(voice.id));
      event.dataTransfer.effectAllowed = 'copy';
    });
    return card;
  }

  // ---------- роли книги ----------

  const loadBooks = guard(async () => {
    const tree = await get('/api/library');
    if (!alive) return;
    const books = flattenBooks(tree);
    bookSelect.replaceChildren(...books.map((book) =>
      el('option', { value: book.id, selected: book.id === state.bookId }, book.title)));
    if (!books.length) {
      castHost.replaceChildren(el('div', { class: 'placeholder' }, 'Сначала добавьте книгу.'));
      return;
    }
    if (!books.some((b) => b.id === state.bookId)) state.bookId = books[0].id;
    bookSelect.value = String(state.bookId);
    await loadCast();
  });

  const loadCast = guard(async () => {
    if (!state.bookId) return;
    remember(state.bookId);
    const data = await get(`/api/books/${state.bookId}/cast`);
    if (!alive) return;
    state.cast = data;
    drawCastTools();
    if (!data.speakers.length) {
      castHost.replaceChildren(el('div', { class: 'placeholder' },
        'В книге пока нет ролей — сначала разметьте главы.'));
      return;
    }
    castHost.replaceChildren(...data.speakers.map(castRow));
  });

  function drawCastTools() {
    castTools.replaceChildren(
      el('button', { class: 'primary', onclick: autoAssign }, icon('wand-sparkles'), 'Автоподбор'),
      el('button', { onclick: saveProfile }, icon('save'), 'Сохранить профиль'),
      el('button', { onclick: applyProfile }, icon('users-round'), 'Применить профиль…'));
  }

  function castRow(speaker) {
    const selected = state.selected === speaker.name;
    const row = el('div', {
      class: `cast-row${selected ? ' selected' : ''}`, dataset: { speaker: speaker.name },
      onclick: () => { state.selected = speaker.name; loadCast(); },
    },
      el('div', { class: 'row' },
        el('b', { class: 'grow' }, speaker.name === 'narrator' ? 'рассказчик' : speaker.name),
        speaker.gender
          ? el('span', {
            class: 'badge',
            title: speaker.gender_source === 'text'
              ? 'Пол определён по глаголам рядом с именем в тексте'
              : 'Пол угадан по окончанию имени',
          }, speaker.gender + (speaker.gender_source === 'text' ? '' : '?'))
          : null,
        el('span', { class: 'hint' }, plural(speaker.count, 'реплика', 'реплики', 'реплик'))),
      shareNote(speaker),
      el('div', { class: 'row' },
        speaker.voice
          ? el('span', { class: 'voice-chip' }, `${speaker.voice.label} · ${speaker.voice.engine}`)
          : el('span', { class: 'hint warn' }, 'голос не назначен'),
        speaker.voice ? el('button', {
          class: 'icon-button', title: 'Прослушать роль',
          onclick: guard(async (event) => {
            event.stopPropagation();
            const result = await post(`/api/voices/${speaker.voice.id}/preview`, {
              text: phrase.value.trim(),
              rate: speaker.cast.rate, pitch: speaker.cast.pitch, volume: speaker.cast.volume,
            });
            player.src = result.url;
            player.play().catch(() => {});
          }),
        }, icon('play')) : null,
        speaker.voice ? el('button', {
          class: 'icon-button ghost', title: 'Снять голос',
          onclick: guard(async (event) => {
            event.stopPropagation();
            await put(`/api/books/${state.bookId}/cast`, { speaker: speaker.name, voice_id: null });
            loadCast();
          }),
        }, icon('x')) : null),
      speaker.cast ? sliders(speaker) : null);

    row.addEventListener('dragover', (event) => {
      if (![...event.dataTransfer.types].includes('application/x-booktts-voice')) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
      row.classList.add('drop-into');
    });
    row.addEventListener('dragleave', () => row.classList.remove('drop-into'));
    row.addEventListener('drop', (event) => {
      event.preventDefault();
      row.classList.remove('drop-into');
      const voiceId = Number(event.dataTransfer.getData('application/x-booktts-voice'));
      if (voiceId) assignVoice(voiceId, speaker.name);
    });
    return row;
  }

  /** С кем роль делит голос. Одинаковая высота — звучат неразличимо. */
  function shareNote(speaker) {
    if (!speaker.cast?.voice_id) return null;
    const twins = state.cast.speakers.filter((other) => other.name !== speaker.name
      && other.cast?.voice_id === speaker.cast.voice_id);
    if (!twins.length) return null;
    const same = twins.filter((other) => Math.abs(other.cast.pitch - speaker.cast.pitch) < 0.03);
    // Имя в кавычках после «роль» не нужно склонять: «у роли «Велимир»», а не «у Велимир».
    const names = (list) => list
      .map((o) => (o.name === 'narrator' ? 'рассказчик' : `«${o.name}»`)).join(', ');
    return same.length
      ? el('div', { class: 'hint warn' }, icon('triangle-alert', { size: 12 }),
        ` звучит одинаково с ${list(same)} — измените высоту или голос`)
      : el('div', { class: 'hint' }, `общий голос с ${list(twins)}, но другая высота`);

    // «с ролью «Велимир»», «с ролями «Велимир», «Терех»» — творительный падеж после «с».
    function list(items) {
      return `${items.length > 1 ? 'ролями' : 'ролью'} ${names(items)}`;
    }
  }

  function sliders(speaker) {
    const make = (label, field, min, max) => {
      const value = speaker.cast[field];
      const output = el('span', { class: 'hint' }, value.toFixed(2));
      const input = el('input', {
        type: 'range', min: String(min), max: String(max), step: '0.05', value: String(value),
      });
      input.oninput = () => { output.textContent = Number(input.value).toFixed(2); };
      input.onchange = guard(async () => {
        await put(`/api/books/${state.bookId}/cast`, {
          speaker: speaker.name, voice_id: speaker.cast.voice_id,
          rate: speaker.cast.rate, pitch: speaker.cast.pitch, volume: speaker.cast.volume,
          [field]: Number(input.value),
        });
        loadCast();
      });
      input.onclick = (event) => event.stopPropagation();
      return el('label', { class: 'slider' }, el('span', { class: 'hint' }, label), input, output);
    };
    return el('div', { class: 'sliders' },
      make('темп', 'rate', 0.5, 2), make('высота', 'pitch', 0.5, 2), make('громкость', 'volume', 0.1, 2));
  }

  const assignVoice = guard(async (voiceId, speakerName = state.selected) => {
    if (!speakerName) return toast('Сначала выберите роль справа', 'error');
    const speaker = state.cast?.speakers.find((s) => s.name === speakerName);
    await put(`/api/books/${state.bookId}/cast`, {
      speaker: speakerName, voice_id: voiceId,
      rate: speaker?.cast?.rate ?? 1, pitch: speaker?.cast?.pitch ?? 1,
      volume: speaker?.cast?.volume ?? 1,
    });
    toast(`Роль «${speakerName}» озвучена`, 'ok');
    loadCast();
  });

  const autoAssign = guard(async () => {
    const result = await post(`/api/books/${state.bookId}/cast/auto`, {
      engine: filters.engine.value || 'silero', overwrite: false,
    });
    toast(result.assigned.length
      ? `Назначено ролей: ${result.assigned.length}`
      : 'Все роли уже озвучены', 'ok');
    loadCast();
  });

  const saveProfile = guard(async () => {
    const name = await ask('Название профиля', { placeholder: 'Например, «Цикл про Рудеуса»' });
    if (!name) return;
    await post(`/api/books/${state.bookId}/profiles`, { name });
    toast(`Профиль «${name}» сохранён`, 'ok');
  });

  const applyProfile = guard(async () => {
    const { profiles } = await get('/api/profiles');
    if (!profiles.length) return toast('Профилей пока нет — сохраните первый', 'error');
    const choice = await pickProfile(profiles);
    if (!choice) return;
    const result = await post(`/api/books/${state.bookId}/apply-profile`, { profile_id: choice });
    const lines = [`назначено ${result.applied.length}`];
    if (result.missing_voice.length) lines.push(`голос пропал у ${result.missing_voice.length}`);
    if (result.without_voice.length) lines.push(`без голоса ${result.without_voice.length}`);
    toast(lines.join(', '), result.missing_voice.length ? 'error' : 'ok');
    loadCast();
  });

  function pickProfile(profiles) {
    return new Promise((resolve) => {
      const select = el('select', { class: 'grow' }, profiles.map((p) =>
        el('option', { value: p.id }, `${p.name} — ${plural(p.entries, 'роль', 'роли', 'ролей')}`)));
      const dialog = el('dialog');
      const done = (value) => { dialog.close(); dialog.remove(); resolve(value); };
      dialog.append(
        el('h3', {}, 'Применить профиль'),
        el('div', { class: 'field' }, select),
        el('p', { class: 'hint' }, 'Похожие имена персонажей сведутся: «Велимир» и «Велемир» — один голос.'),
        el('div', { class: 'buttons' },
          el('button', { onclick: () => done(null) }, 'Отмена'),
          el('button', {
            class: 'primary danger', onclick: async () => {
              const id = Number(select.value);
              const profile = profiles.find((p) => p.id === id);
              done(null);
              if (await confirmAction(`Удалить профиль «${profile.name}»?`,
                'Назначенные голоса книг останутся.')) {
                await fetch(`/api/profiles/${id}`, {
                  method: 'DELETE',
                  headers: { 'x-requested-with': 'booktts' },
                });
                toast('Профиль удалён', 'ok');
              }
            },
          }, 'Удалить'),
          el('button', { class: 'primary', onclick: () => done(Number(select.value)) }, 'Применить')));
      dialog.addEventListener('cancel', (event) => { event.preventDefault(); done(null); });
      document.body.append(dialog);
      dialog.showModal();
    });
  }

  // ---------- события ----------

  filters.gender.replaceChildren(
    el('option', { value: '' }, 'любой пол'),
    el('option', { value: 'м' }, 'мужские'),
    el('option', { value: 'ж' }, 'женские'));
  filters.engine.onchange = loadVoices;
  filters.gender.onchange = loadVoices;
  let searchTimer = null;
  filters.search.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadVoices, 250); };
  bookSelect.onchange = () => { state.bookId = Number(bookSelect.value); state.selected = null; loadCast(); };
  fromBook.onclick = guard(async () => {
    if (!state.bookId) return;
    const chapters = await get(`/api/books/${state.bookId}`);
    const first = chapters.chapters.find((c) => c.segments);
    if (!first) return toast('В книге нет размеченных глав', 'error');
    const chapter = await get(`/api/chapters/${first.id}`);
    const line = chapter.segments.find((s) => s.text.length > 40) || chapter.segments[0];
    if (line) phrase.value = line.text.slice(0, 200);
  });

  loadEngines();
  loadVoices();
  loadBooks();

  return () => {
    alive = false;
    player.pause();
  };
}
