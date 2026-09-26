// Студия — каталог голосов: все голоса движков, проба, состояние движков, профили.

import { del, get, post } from '../../api.js';
import { watchActiveJobs } from '../../jobs-state.js';
import { confirmAction, el, emit, guard, icon, plural, toast } from '../../ui.js';
import { shortEngine, setStudioJobs, studioFrame } from './common.js';

const player = new Audio();
const GENDERS = [['', 'Любые'], ['м', 'Мужские'], ['ж', 'Женские']];
const GENDER_WORD = { м: 'мужской', ж: 'женский' };

export function render(view) {
  let alive = true;
  let engines = [];
  let voices = [];     // что показано — с фильтрами
  let allVoices = [];  // весь каталог — для счётчиков у движков
  const filter = { engine: '', gender: '', q: '' };
  const page = studioFrame(view, 'voices');

  // Образцы готовы заранее — ▶ играет сразу. Своя фраза озвучивается по нажатию.
  const phrase = el('input', { placeholder: 'Своя фраза — впишите и нажмите ▶ у любого голоса', 'aria-label': 'Своя фраза для пробы' });
  const phraseNote = el('p', { class: 'hint phrase-note' });
  const samples = el('div', { class: 'samples-note', hidden: true });
  let playing = null;  // какой голос звучит сейчас: {id, button}
  const search = el('input', { type: 'search', placeholder: 'Имя или метка', 'aria-label': 'Найти голос' });
  const engineSeg = el('div', { class: 'switcher' });
  const genderSeg = el('div', { class: 'switcher' });
  const grid = el('div', { class: 'voice-list' });
  const side = el('aside', { class: 'studio-side' });

  const drawSegs = () => {
    const counts = {};
    for (const voice of allVoices) counts[voice.engine] = (counts[voice.engine] || 0) + 1;
    engineSeg.replaceChildren(
      el('button', { 'aria-pressed': String(!filter.engine), onclick: () => { filter.engine = ''; loadVoices(); } }, `Все · ${allVoices.length}`),
      ...engines.map((engine) => el('button', {
        'aria-pressed': String(filter.engine === engine.name),
        onclick: () => { filter.engine = engine.name; loadVoices(); },
        title: engine.ready ? '' : engine.detail,
      }, counts[engine.name] ? `${shortEngine(engine)} · ${counts[engine.name]}` : shortEngine(engine))));
    genderSeg.replaceChildren(...GENDERS.map(([value, label]) => el('button', {
      'aria-pressed': String(filter.gender === value), onclick: () => { filter.gender = value; drawVoices(); drawSegs(); },
    }, label)));
  };

  function setPlaying(next) {
    if (playing?.button) playing.button.replaceChildren(icon('play', { className: 'filled', size: 14 }));
    playing = next;
    if (playing?.button) playing.button.replaceChildren(icon('pause', { className: 'filled', size: 14 }));
  }
  player.addEventListener('ended', () => setPlaying(null));

  function voiceRow(voice) {
    const ready = Boolean(voice.preview_url);
    const play = el('button', {
      class: 'round invert', 'aria-label': `Послушать ${voice.label}`,
      title: ready ? 'Послушать образец' : 'Послушать — образец запишется за секунду-две',
    }, icon('play', { className: 'filled', size: 14 }));
    play.onclick = guard(async () => {
      // Второе нажатие по звучащему голосу — стоп.
      if (playing?.id === voice.id && !player.paused) { player.pause(); setPlaying(null); return; }
      const custom = phrase.value.trim();
      let url = !custom && voice.preview_url;
      if (!url) {
        play.classList.add('busy');
        try {
          url = (await post(`/api/voices/${voice.id}/preview`, { text: custom })).url;
        } finally {
          play.classList.remove('busy');
        }
      }
      player.src = url;
      await player.play();
      setPlaying({ id: voice.id, button: play });
    });
    const engine = engines.find((e) => e.name === voice.engine);
    // Метки без служебных («v5_5_ru», «родной: en») и без повтора пола.
    const tags = voice.tags.map((tag) => tag.trim())
      .filter((tag) => tag && !/^v\d/.test(tag) && !/^родной:/.test(tag) && tag !== GENDER_WORD[voice.gender]);
    return el('div', { class: `voice-row${voice.available ? '' : ' off'}${ready ? ' has-sample' : ''}` },
      el('span', { class: 'voice-letter', 'aria-hidden': 'true' }, voice.label.charAt(0).toUpperCase()),
      el('div', { class: 'grow' },
        el('div', { class: 'voice-name' }, voice.label, el('span', { class: 'hint' }, ` · ${shortEngine(engine || voice.engine)}`)),
        el('div', { class: 'hint ellipsis' }, [GENDER_WORD[voice.gender], ...tags].filter(Boolean).join(' · '))),
      voice.books ? el('span', { class: 'used-badge' }, `в ${plural(voice.books, 'книге', 'книгах', 'книгах')}`) : null,
      play);
  }

  function drawVoices() {
    const shown = voices.filter((voice) => !filter.gender || voice.gender === filter.gender);
    grid.replaceChildren(...(shown.length ? shown.map(voiceRow) : [el('div', { class: 'placeholder' },
      voices.length ? 'Таких голосов нет.' : 'Каталог пуст. Нажмите «Обновить каталог» у нужного движка справа.')]));
  }

  const loadVoices = guard(async () => {
    const params = new URLSearchParams();
    if (filter.engine) params.set('engine', filter.engine);
    if (filter.q) params.set('q', filter.q);
    const shown = await get(`/api/voices?${params}`);
    if (!alive) return;
    voices = shown.voices;
    if (!filter.engine && !filter.q) allVoices = voices;
    drawVoices();
    drawSegs();
  });

  const loadAll = guard(async () => {
    allVoices = (await get('/api/voices')).voices;
    if (alive) drawSegs();
  });

  const refresh = (engine) => guard(async () => {
    toast(`Обновляю каталог ${engine.title || engine.name}…`);
    const { report } = await post('/api/engines/refresh', { engine: engine.name });
    for (const entry of report) {
      if (entry.error) toast(`${entry.engine}: ${entry.error}`, 'error');
      else toast(`${engine.title || entry.engine}: добавлено ${entry.added}, обновлено ${entry.updated}` +
        (entry.gone ? `, пропало ${entry.gone}` : ''), 'ok');
    }
    await loadSide();
    await loadAll();
    loadVoices();
  });

  const loadSide = guard(async () => {
    const [engineData, profiles] = await Promise.all([get('/api/engines'), get('/api/profiles').catch(() => ({ profiles: [] }))]);
    if (!alive) return;
    engines = engineData.engines;
    const sample = engineData.settings['preview.text'] || '';
    phraseNote.textContent = sample ? `Образцы звучат фразой «${sample}». Её можно поменять в «Настройках».` : '';
    side.replaceChildren(
      el('section', { class: 'card-box side-card' },
        el('h2', {}, 'Движки'),
        engines.map((engine) => el('div', { class: 'engine-line' },
          el('span', { class: `state-dot ${engine.ready ? 'ok' : ''}`, 'aria-hidden': 'true' }),
          el('div', { class: 'grow' },
            el('b', {}, engine.title || engine.name),
            el('div', { class: 'hint' }, engine.detail || (engine.ready ? 'готов' : 'недоступен'))),
          engine.needs_key && !engine.ready
            ? el('a', { class: 'button sm', href: '#/settings' }, 'Ключ')
            : el('button', { class: 'round ghost sm', 'aria-label': `Обновить каталог ${engine.title || engine.name}`, title: 'Обновить каталог', onclick: refresh(engine) }, icon('refresh-cw', { size: 14 }))))),
      el('section', { class: 'card-box side-card' },
        el('h2', {}, 'Профили голосов'),
        el('p', { class: 'hint' }, 'Профиль — набор голосов для ролей. Сохраните его на экране голосов книги и применяйте к новым книгам одним нажатием: похожие имена сведутся сами.'),
        profiles.profiles.length
          ? profiles.profiles.map((profile) => el('div', { class: 'engine-line' },
            el('div', { class: 'grow' }, el('b', {}, profile.name), el('div', { class: 'hint' }, plural(profile.entries, 'роль', 'роли', 'ролей'))),
            el('button', {
              class: 'round ghost sm', 'aria-label': `Удалить профиль ${profile.name}`, title: 'Удалить профиль',
              onclick: guard(async () => {
                if (!await confirmAction(`Удалить профиль «${profile.name}»?`, 'Назначенные голоса книг останутся.')) return;
                await del(`/api/profiles/${profile.id}`);
                toast('Профиль удалён', 'ok');
                loadSide();
              }),
            }, icon('trash-2', { size: 14 }))))
          : el('span', { class: 'hint' }, 'Пока нет ни одного профиля.')),
      el('section', { class: 'dashed-card' },
        el('b', {}, 'Свой голос'),
        el('span', { class: 'hint' }, 'Голос по описанию и клонирование по записи в Qwen3-TTS — в версии 0.5.')));
    drawSegs();
  });

  let timer = null;
  search.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { filter.q = search.value.trim(); loadVoices(); }, 250);
  };

  get('/api/jobs?active=true').catch(() => ({ active: 0 })).then((jobs) => {
    if (!alive) return;
    setStudioJobs(jobs.active);
    page.replaceChildren(
      el('div', { class: 'lib-filters' }, engineSeg, genderSeg,
        el('label', { class: 'search-field small' }, icon('search'), search)),
      el('div', { class: 'studio-grid' },
        el('div', { class: 'section' },
          samples,
          el('label', { class: 'search-field wide' }, icon('volume-2'), phrase),
          phraseNote,
          grid),
        side));
  });
  /** Образцов не хватает — записать их в фоне; пока идёт, показать, сколько осталось. */
  const prepareSamples = guard(async () => {
    const result = await post('/api/voices/previews');
    if (result.job) emit('jobs-changed');
  });
  let samplesJob = null;
  const unwatchJobs = watchActiveJobs((jobs) => {
    const job = jobs.find((item) => item.kind === 'previews');
    if (job) {
      samples.hidden = false;
      samples.replaceChildren(el('span', { class: 'spinner', 'aria-hidden': 'true' }),
        `Записываю образцы голосов${job.total ? ` · ${job.done} из ${job.total}` : ''} — ▶ заиграет сразу, как только образец готов.`);
    } else {
      samples.hidden = true;
      if (samplesJob) loadVoices();  // образцы готовы — перечитать каталог
    }
    samplesJob = job || null;
  });
  loadSide().then(loadAll).then(loadVoices).then(prepareSamples);

  return () => {
    alive = false;
    clearTimeout(timer);
    unwatchJobs();
    player.pause();
  };
}
