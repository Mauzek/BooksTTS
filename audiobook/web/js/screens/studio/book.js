// Студия — шаг «Голоса» книги: кто какую роль читает. Проба звучит репликой
// самого персонажа; приложение само подсказывает, где голос не подходит.
// Встраивается в рабочее место книги (workspace.js).

import { get, post, put } from '../../api.js';
import { avatar, speakerName } from '../../covers.js';
import { ask, confirmAction, dropdown, el, emit, guard, icon, plural, popupMenu, toast } from '../../ui.js';
import { voiceSelect } from '../../voices.js';

const player = new Audio();
const GENDER_WORD = { м: 'мужской', ж: 'женский' };
const PERSON = { м: 'мужского', ж: 'женского' };
const PITCH_STEP = 0.2;

/** Нарисовать шаг «Голоса» в host. Вернёт уборку. */
export function mountVoices(host, bookId) {
  let alive = true;
  let data = null;
  let voices = [];
  let engines = [];
  const open = new Set();  // у каких ролей раскрыты ползунки
  const page = host;

  const load = guard(async () => {
    const [cast, catalog, engineData, book] = await Promise.all([
      get(`/api/books/${bookId}/cast`),
      get('/api/voices'),
      get('/api/engines').catch(() => ({ engines: [] })),
      get(`/api/books/${bookId}`),
    ]);
    if (!alive) return;
    data = { ...cast, chapters: book.chapters };
    voices = catalog.voices;
    engines = engineData.engines;
    draw();
  });

  const setCast = (speaker, changes) => put(`/api/books/${bookId}/cast`, {
    speaker: speaker.name,
    voice_id: speaker.cast?.voice_id ?? null,
    rate: speaker.cast?.rate ?? 1, pitch: speaker.cast?.pitch ?? 1, volume: speaker.cast?.volume ?? 1,
    ...changes,
  });

  const preview = (speaker, button) => guard(async () => {
    if (!speaker.voice) return;
    button.disabled = true;
    try {
      const result = await post(`/api/voices/${speaker.voice.id}/preview`, {
        text: speaker.sample || '',
        rate: speaker.cast.rate, pitch: speaker.cast.pitch, volume: speaker.cast.volume,
      });
      player.src = result.url;
      await player.play();
    } finally {
      button.disabled = false;
    }
  });

  /** Что с ролью не так — и как это исправить одним нажатием. */
  function warnings(speaker) {
    const out = [];
    if (!speaker.voice) return out;
    if (!speaker.voice.available) {
      out.push({ text: `Голос ${speaker.voice.label} пропал из каталога ${speaker.voice.engine} — выберите другой.` });
    }
    const gender = speaker.gender;
    if (gender && speaker.voice.gender && gender !== speaker.voice.gender && !speaker.is_narrator) {
      const fitting = voices.filter((v) => v.available && v.engine === speaker.voice.engine && v.gender === gender);
      const used = new Set(data.speakers.filter((s) => s.voice).map((s) => s.voice.id));
      const free = fitting.find((v) => !used.has(v.id));
      const pick = free || fitting[0];
      out.push({
        text: `${GENDER_WORD[speaker.voice.gender].replace(/^./, (c) => c.toUpperCase())} голос у ${PERSON[gender]} персонажа.` +
          (free ? '' : pick ? ` Свободных ${gender === 'м' ? 'мужских' : 'женских'} голосов у ${speaker.voice.engine} не осталось.` : ''),
        fix: pick ? [free ? `Взять ${pick.label}` : `Взять ${pick.label}, тон выше`,
          () => setCast(speaker, { voice_id: pick.id, pitch: free ? 1 : 1 + PITCH_STEP })] : null,
      });
    }
    const twins = data.speakers.filter((other) => other.name !== speaker.name && other.voice?.id === speaker.voice.id
      && Math.abs((other.cast?.pitch ?? 1) - speaker.cast.pitch) < 0.03);
    if (twins.length) {
      const highest = Math.max(speaker.cast.pitch, ...twins.map((t) => t.cast.pitch));
      out.push({
        text: `Звучит так же, как ${twins.map((t) => (t.is_narrator ? 'рассказчик' : `«${t.name}»`)).join(', ')}.`,
        fix: ['Поднять тон', () => setCast(speaker, { pitch: Math.min(2, +(highest + PITCH_STEP).toFixed(2)) })],
      });
    }
    return out;
  }

  function voicePicker(speaker) {
    return voiceSelect(speaker, { voices, engines }, guard(async (voiceId) => {
      await setCast(speaker, { voice_id: voiceId });
      load();
    }));
  }

  function sliders(speaker) {
    const make = (label, field, min, max) => {
      const value = speaker.cast[field];
      const output = el('output', {}, value.toFixed(2).replace('.', ','));
      const input = el('input', { type: 'range', min: String(min), max: String(max), step: '0.05', value: String(value), 'aria-label': label });
      input.oninput = () => { output.textContent = Number(input.value).toFixed(2).replace('.', ','); };
      input.onchange = guard(async () => { await setCast(speaker, { [field]: Number(input.value) }); load(); });
      return el('label', { class: 'slider' }, el('span', { class: 'hint' }, label), input, output);
    };
    return el('div', { class: 'role-sliders' },
      make('Темп', 'rate', 0.5, 2), make('Тон', 'pitch', 0.5, 2), make('Громкость', 'volume', 0.1, 2));
  }

  function roleRow(speaker) {
    const play = el('button', {
      class: 'round invert', 'aria-label': `Послушать: ${speakerName(speaker.name)}`,
      title: speaker.sample ? `Прозвучит: «${speaker.sample}»` : 'Послушать', disabled: !speaker.voice,
    }, icon('play', { className: 'filled', size: 14 }));
    play.onclick = preview(speaker, play);
    const tune = el('button', {
      class: `round ghost${open.has(speaker.name) ? ' active' : ''}`, 'aria-expanded': String(open.has(speaker.name)),
      'aria-label': 'Темп, тон и громкость', title: 'Темп, тон и громкость', disabled: !speaker.cast,
      onclick: () => { if (open.has(speaker.name)) open.delete(speaker.name); else open.add(speaker.name); draw(); },
    }, icon('settings'));
    const meta = [plural(speaker.count, 'реплика', 'реплики', 'реплик')];
    if (speaker.gender) meta.push(speaker.gender_source === 'text' ? speaker.gender : `${speaker.gender}?`);
    return el('div', { class: 'role-row' },
      el('div', { class: 'role-main' },
        avatar(speaker.name, speaker.slot),
        el('div', { class: 'role-who' },
          el('b', {}, speakerName(speaker.name)),
          el('span', { class: 'hint', title: speaker.gender_source === 'text' ? 'Пол — по глаголам рядом с именем' : 'Пол угадан по окончанию имени' }, meta.join(' · '))),
        el('span', { class: 'role-sample' }, speaker.sample ? `«${speaker.sample}»` : ''),
        voicePicker(speaker), play, tune),
      !speaker.voice ? el('div', { class: 'role-warn' }, icon('circle-alert', { size: 16 }), 'Голос не назначен — без него роль не озвучится.') : null,
      ...warnings(speaker).map((warning) => el('div', { class: 'role-warn' },
        icon('triangle-alert', { size: 16 }), el('span', { class: 'grow' }, warning.text),
        warning.fix ? el('button', { class: 'surface sm', onclick: guard(async () => { await warning.fix[1](); load(); }) }, warning.fix[0]) : null)),
      open.has(speaker.name) && speaker.cast ? sliders(speaker) : null);
  }

  const autoAssign = (engine, overwrite) => guard(async () => {
    if (overwrite && !await confirmAction('Заменить все голоса?',
      `Все роли получат голоса ${engine.title || engine.name}. Настройки темпа и тона сбросятся.`, 'Заменить')) return;
    const result = await post(`/api/books/${bookId}/cast/auto`, { engine: engine.name, overwrite });
    toast(result.assigned.length ? `Назначено ролей: ${result.assigned.length}` : 'У всех ролей уже есть голос', 'ok');
    load();
  });

  const saveProfile = guard(async () => {
    const name = await ask('Название профиля', { placeholder: 'Например, «Цикл про Рудеуса»' });
    if (!name) return;
    await post(`/api/books/${bookId}/profiles`, { name });
    toast(`Профиль «${name}» сохранён`, 'ok');
  });

  const applyProfile = guard(async () => {
    const { profiles } = await get('/api/profiles');
    if (!profiles.length) return toast('Профилей пока нет — сохраните первый', 'error');
    const choice = await new Promise((resolve) => {
      const select = dropdown({
        className: 'wide', label: 'Профиль голосов', value: profiles[0].id,
        options: profiles.map((p) => ({ value: p.id, label: p.name, hint: ` — ${plural(p.entries, 'роль', 'роли', 'ролей')}` })),
      });
      const dialog = el('dialog');
      const done = (value) => { dialog.close(); dialog.remove(); resolve(value); };
      dialog.append(el('h3', {}, 'Применить профиль'), el('div', { class: 'field' }, select),
        el('p', { class: 'hint' }, 'Похожие имена сведутся сами: «Велимир» и «Велемир» получат один голос.'),
        el('div', { class: 'buttons' },
          el('button', { class: 'ghost', onclick: () => done(null) }, 'Отмена'),
          el('button', { class: 'primary', onclick: () => done(Number(select.value)) }, 'Применить')));
      dialog.addEventListener('cancel', (event) => { event.preventDefault(); done(null); });
      document.body.append(dialog);
      dialog.showModal();
    });
    if (!choice) return;
    const result = await post(`/api/books/${bookId}/apply-profile`, { profile_id: choice });
    const lines = [`назначено ${result.applied.length}`];
    if (result.missing_voice.length) lines.push(`голос пропал у ${result.missing_voice.length}`);
    if (result.without_voice.length) lines.push(`без голоса ${result.without_voice.length}`);
    toast(lines.join(', '), result.missing_voice.length ? 'error' : 'ok');
    load();
  });

  function draw() {
    const { book, speakers } = data;
    // Движок выбирается у каждой роли — через её голос; в книге их можно
    // смешивать. Здесь — сводка, чем читают сейчас, и подбор голосов разом.
    const usage = engines.map((engine) => ({
      engine, roles: speakers.filter((s) => s.voice?.engine === engine.name).length,
    })).filter((item) => item.roles);
    const without = speakers.filter((s) => !s.voice).length;
    const ready = engines.filter((engine) => engine.ready);
    const engineMenu = (anchor, overwrite) => popupMenu(anchor, ready.length
      ? ready.map((engine) => [engine.title || engine.name, () => autoAssign(engine, overwrite)()])
      : [['Нет готовых движков — проверьте «Голоса» студии', () => {}, '', true]]);
    const fillButton = el('button', { class: 'surface', disabled: !without, 'aria-haspopup': 'menu' },
      icon('wand-sparkles'), without ? `Подобрать голоса ролям без голоса · ${without}` : 'У всех ролей есть голос', icon('chevron-down', { size: 16 }));
    fillButton.onclick = () => engineMenu(fillButton, false);
    const replaceButton = el('button', { class: 'ghost sm', 'aria-haspopup': 'menu' }, 'Перевести все роли на другой движок…');
    replaceButton.onclick = () => engineMenu(replaceButton, true);

    page.replaceChildren(
      el('div', { class: 'studio-grid' },
        el('div', { class: 'section' },
          speakers.length
            ? speakers.map(roleRow)
            : el('div', { class: 'placeholder' }, 'В книге пока нет ролей — сначала разберите главы по ролям.',
              el('div', { class: 'row', style: { marginTop: '12px', justifyContent: 'center' } },
                el('a', { class: 'button primary', href: `#/studio/book/${book.id}/roles` }, 'К шагу «Роли»')))),
        el('aside', { class: 'studio-side' },
          el('section', { class: 'card-box side-card' },
            el('h2', {}, 'Чем озвучивается книга'),
            usage.length
              ? el('div', { class: 'engine-usage' }, usage.map(({ engine, roles }) => el('div', { class: 'engine-line' },
                el('span', { class: `state-dot ${engine.ready ? 'ok' : ''}`, 'aria-hidden': 'true' }),
                el('div', { class: 'grow' }, el('b', {}, engine.title || engine.name),
                  el('div', { class: 'hint' }, plural(roles, 'роль', 'роли', 'ролей'))))))
              : el('p', { class: 'hint' }, 'Голоса ещё не назначены.'),
            el('p', { class: 'hint' }, 'Движок выбирается у каждой роли — через её голос. В одной книге можно смешивать: главным героям — Qwen3-TTS, остальным — Silero.'),
            fillButton,
            replaceButton),
          el('section', { class: 'card-box side-card' },
            el('h2', {}, 'Профиль'),
            el('button', { class: 'surface', onclick: saveProfile }, icon('save'), 'Сохранить как профиль'),
            el('button', { class: 'surface', onclick: applyProfile }, icon('users-round'), 'Применить профиль…')))));
    emit('book-voices-changed', { bookId, missing: speakers.filter((s) => !s.voice).length });
  }

  load();
  return () => {
    alive = false;
    player.pause();
  };
}
