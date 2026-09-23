// Карточка книги: название, главы, границы глав, озвучка, словарь произношений.

import { api, del, get, patch, post, put } from '../api.js';
import * as player from '../player.js';
import { ask, confirmAction, el, emit, formatDuration, guard, icon, plural, toast } from '../ui.js';

const PREVIEW_CHARS = 320;

export function render(view, bookId) {
  let alive = true;
  const openBoundaries = new Set();
  const page = el('div', { class: 'page' });
  view.replaceChildren(page);

  const load = guard(async () => {
    const [data, rules] = await Promise.all([
      get(`/api/books/${bookId}`),
      get(`/api/books/${bookId}/pronunciations`).catch(() => ({ rules: [] })),
    ]);
    if (alive) draw(data, rules.rules);
  });

  const changed = () => emit('library-changed');

  function draw({ book, chapters, speakers }, rules) {
    const marked = chapters.filter((c) => c.segments).length;
    const voiced = chapters.filter((c) => c.segments && c.voiced >= c.segments).length;
    const chars = chapters.reduce((n, c) => n + c.n_chars, 0);

    const title = el('h1', { class: 'editable', title: 'Переименовать' }, book.title);
    title.onclick = guard(async () => {
      const value = await ask('Название книги', { value: book.title });
      if (value) { await patch(`/api/books/${book.id}`, { title: value }); changed(); }
    });
    const author = el('div', { class: 'editable muted', title: 'Изменить автора' },
      book.author || 'автор не указан');
    author.onclick = guard(async () => {
      const value = await ask('Автор', { value: book.author, allowEmpty: true });
      if (value !== null) { await patch(`/api/books/${book.id}`, { author: value }); changed(); }
    });

    const voiceAll = el('button', { class: 'primary' }, icon('mic'), 'Озвучить книгу');
    voiceAll.onclick = guard(async () => {
      const ok = await confirmAction('Озвучить всю книгу?',
        `${plural(chapters.length, 'глава', 'главы', 'глав')} встанут в очередь. ` +
        'Уже озвученные реплики пропустятся, очередь переживёт закрытие приложения.',
        'Озвучить');
      if (!ok) return;
      const job = await post(`/api/books/${book.id}/synthesize`, {});
      toast(`Задача добавлена: ${job.title}`, 'ok');
      emit('jobs-changed');
    });

    const remove = el('button', { class: 'danger' }, icon('trash-2'), 'Удалить книгу');
    remove.onclick = guard(async () => {
      const ok = await confirmAction(`Удалить книгу «${book.title}»?`,
        'Удалятся главы, разметка, озвучка и копия файла в библиотеке. Отменить это нельзя.');
      if (!ok) return;
      await del(`/api/books/${book.id}`);
      location.hash = '#/';
      changed();
    });

    const firstUnmarked = chapters.find((c) => !c.segments) || chapters[0];

    page.replaceChildren(
      el('div', { class: 'book-head' },
        book.cover_path
          ? el('img', { class: 'cover', src: `/api/books/${book.id}/cover`, alt: '' })
          : el('div', { class: 'cover' }, icon('book-open', { className: 'huge' })),
        el('div', { class: 'grow' },
          title, author,
          el('div', { class: 'stats' },
            el('span', {}, el('b', {}, plural(chapters.length, 'глава', 'главы', 'глав'))),
            el('span', {}, el('b', {}, chars.toLocaleString('ru')), ' симв.'),
            el('span', {}, 'размечено ', el('b', {}, `${marked} из ${chapters.length}`)),
            el('span', {}, 'озвучено ', el('b', {}, `${voiced} из ${chapters.length}`)),
            speakers.length
              ? el('span', {}, el('b', {}, plural(speakers.length, 'роль', 'роли', 'ролей')))
              : null),
          el('div', { class: 'row' },
            firstUnmarked
              ? el('a', { class: 'button', href: `#/chapter/${firstUnmarked.id}/edit` },
                firstUnmarked.segments ? 'Открыть разметку' : 'Разметить по ролям')
              : null,
            el('a', { class: 'button', href: '#/voices' }, 'Голоса ролей'),
            voiced
              ? el('button', {
                title: 'Играть озвученные главы подряд',
                onclick: guard(() => player.playBook(book.id)),
              }, icon('play'), 'Слушать книгу')
              : null,
            voiceAll,
            el('span', { class: 'grow' }),
            remove))),
      el('h2', {}, 'Главы'),
      el('p', { class: 'hint' },
        'Границы глав можно поправить: раскройте «Границы» и начните новую главу ',
        'с нужного абзаца или склейте главу со следующей.'),
      el('div', { class: 'list' },
        chapters.map((chapter, index) => chapterRow(chapter, index === chapters.length - 1))),
      el('h2', {}, 'Экспорт'),
      exportSection(book, voiced > 0),
      el('h2', {}, 'Словарь произношений'),
      el('p', { class: 'hint' },
        'Имена и термины, которые синтез читает неправильно. Замена подставляется ',
        'только в озвучку — в тексте главы остаётся исходное написание. ',
        'Правка словаря переозвучит лишь те реплики, где слово встречается.'),
      pronunciations(book, rules),
    );
  }

  function chapterRow(chapter, isLast) {
    const open = openBoundaries.has(chapter.id);
    const progress = chapter.segments ? chapter.voiced / chapter.segments : 0;
    const panel = el('div', { class: 'paragraphs', hidden: !open });

    const rename = guard(async () => {
      const value = await ask('Название главы',
        { value: chapter.title, allowEmpty: true, placeholder: 'без названия' });
      if (value === null) return;
      await patch(`/api/chapters/${chapter.id}`, { title: value });
      load();
    });
    const merge = guard(async () => {
      const ok = await confirmAction('Склеить со следующей главой?',
        `«${chapter.label}» и следующая глава станут одной. Разметка и озвучка реплик сохранятся.`,
        'Склеить');
      if (!ok) return;
      await post(`/api/chapters/${chapter.id}/merge-next`);
      toast('Главы склеены', 'ok');
      changed();
    });
    const voice = guard(async () => {
      const job = await post(`/api/chapters/${chapter.id}/synthesize`, {});
      toast(`Задача добавлена: ${job.title}`, 'ok');
      emit('jobs-changed');
    });
    const toggle = () => {
      if (openBoundaries.has(chapter.id)) openBoundaries.delete(chapter.id);
      else openBoundaries.add(chapter.id);
      load();
    };

    const row = el('div', { class: 'list-row' },
      el('span', { class: 'num' }, chapter.number),
      el('div', { class: 'title' },
        el('div', {}, chapter.title || el('span', { class: 'muted' }, `Глава ${chapter.number}`)),
        el('div', { class: 'sub' },
          `${chapter.n_chars.toLocaleString('ru')} симв. · `,
          chapter.segments ? plural(chapter.segments, 'сегмент', 'сегмента', 'сегментов') : 'не размечена',
          chapter.duration_ms ? ` · ${formatDuration(chapter.duration_ms)}` : '',
          chapter.errors ? ` · ошибок: ${chapter.errors}` : '')),
      el('div', { class: 'row' },
        chapter.voiced
          ? el('div', { class: 'progress', title: `озвучено ${Math.round(progress * 100)}%` },
            el('i', { style: { width: `${progress * 100}%` } }))
          : null,
        el('div', { class: 'actions' },
          chapter.duration_ms
            ? el('button', {
              class: 'icon-button', title: 'Слушать эту главу',
              onclick: guard(() => player.playChapter(chapter.id)),
            }, icon('play'))
            : null,
          el('a', { class: 'button', href: `#/chapter/${chapter.id}` }, icon('headphones'), 'Читать'),
          el('a', { class: 'button', href: `#/chapter/${chapter.id}/edit` }, icon('type'), 'Разметка'),
          chapter.segments
            ? el('button', { onclick: voice, title: 'Поставить в очередь озвучки' }, icon('mic'), 'Озвучить')
            : null,
          el('button', { class: 'icon-button', onclick: rename, title: 'Переименовать главу' },
            icon('pencil')),
          el('button', { onclick: toggle, 'aria-expanded': String(open) },
            icon('scissors'), open ? 'Скрыть границы' : 'Границы'),
          isLast
            ? null
            : el('button', { class: 'icon-button', onclick: merge, title: 'Склеить со следующей' },
              icon('merge')))),
    );
    if (open) loadParagraphs(chapter, panel);
    return el('div', {}, row, panel);
  }

  const loadParagraphs = guard(async (chapter, host) => {
    host.replaceChildren(el('p', { class: 'hint', style: { padding: '8px 14px' } }, 'Загружаю текст…'));
    const data = await get(`/api/chapters/${chapter.id}`);
    const text = data.chapter.text;
    const rows = [];
    let position = 0;
    for (const block of text.split('\n\n')) {
      const start = position;
      position += block.length + 2;
      if (!block.trim()) continue;
      const cut = el('button', { class: 'cut', title: 'Новая глава начнётся с этого абзаца' },
        icon('scissors', { size: 13 }), 'новая глава отсюда');
      cut.onclick = guard(async () => {
        const title = await ask('Новая глава с этого абзаца', {
          label: 'Название новой главы — можно оставить пустым',
          value: block.length <= 80 ? block : '',
          okText: 'Разделить',
          allowEmpty: true,
        });
        if (title === null) return;
        await post(`/api/chapters/${chapter.id}/split-chapter`, { offset: start, title });
        toast('Глава разделена', 'ok');
        changed();
      });
      rows.push(el('div', { class: 'para' }, cut,
        block.length > PREVIEW_CHARS ? `${block.slice(0, PREVIEW_CHARS)}…` : block));
    }
    host.replaceChildren(...rows);
  });

  function exportSection(book, hasAudio) {
    const files = el('div', { class: 'list' });
    const run = (kind, label) => guard(async () => {
      const job = await post(`/api/books/${book.id}/export`, { kind });
      toast(`${label}: задача добавлена`, 'ok');
      emit('jobs-changed');
      return job;
    });

    const folder = el('p', { class: 'hint' });

    const loadFiles = guard(async () => {
      const data = await get(`/api/books/${book.id}/exports`);
      if (!alive) return;
      // Папка одна на все файлы — показываем её сверху, а не в каждой строке.
      folder.replaceChildren(data.files.length ? `Папка: ${data.dir}` : '');
      files.replaceChildren(...(data.files.length
        ? data.files.map((file) => el('div', { class: 'list-row' },
          el('span', { class: 'num' }, icon(file.name.endsWith('.json') ? 'file-json' : 'file-audio')),
          el('div', { class: 'title' }, el('a', { href: file.url, download: file.name }, file.name)),
          el('span', { class: 'hint' },
            `${(file.size / 1048576).toFixed(1).replace('.', ',')} МБ`)))
        : [el('div', { class: 'list-row' },
          el('span', { class: 'num' }),
          el('div', { class: 'title muted' }, 'Пока ничего не выгружено.'),
          el('span'))]));
    });

    const importMarkup = guard(async () => {
      const input = el('input', { type: 'file', accept: '.json' });
      input.onchange = guard(async () => {
        const file = input.files[0];
        if (!file) return;
        const result = await api(`/api/books/${book.id}/import-markup`, {
          method: 'POST', body: await file.text(),
          headers: { 'content-type': 'application/json' },
        });
        const chapters = result.chapters.length;
        toast(`Разметка наложена на ${plural(chapters, 'главу', 'главы', 'глав')}` +
          (result.lost_segments ? `, не нашлось реплик: ${result.lost_segments}` : ''),
          result.lost_segments ? 'error' : 'ok');
        changed();
      });
      input.click();
    });

    loadFiles();
    return el('div', {},
      el('p', { class: 'hint' },
        'm4b — один файл с оглавлением и обложкой, его понимают плееры аудиокниг. ',
        'Разметку можно передать другому человеку: она привязана к тексту, а не к номерам строк.'),
      el('div', { class: 'row' },
        el('button', {
          class: 'primary', disabled: !hasAudio, title: hasAudio ? '' : 'Сначала озвучьте главы',
          onclick: run('m4b', 'm4b'),
        }, icon('download'), 'Собрать m4b'),
        el('button', {
          disabled: !hasAudio, onclick: run('mp3', 'mp3'),
        }, icon('file-audio'), 'Разложить mp3'),
        el('button', { onclick: run('json', 'Разметка') }, icon('file-json'), 'Сохранить разметку'),
        el('button', { onclick: importMarkup }, icon('wand-sparkles'), 'Загрузить разметку…')),
      folder, files);
  }

  function pronunciations(book, rules) {
    const term = el('input', { placeholder: 'Как написано' });
    const replacement = el('input', { placeholder: 'Как читать' });
    const wholeWord = el('input', { type: 'checkbox', checked: true });
    const add = el('button', { class: 'primary' }, 'Добавить');
    add.onclick = guard(async () => {
      if (!term.value.trim()) return toast('Впишите слово', 'error');
      await put(`/api/books/${book.id}/pronunciations`, {
        term: term.value.trim(),
        replacement: replacement.value.trim(),
        whole_word: wholeWord.checked,
      });
      term.value = replacement.value = '';
      toast('Добавлено в словарь', 'ok');
      load();
    });

    return el('div', {},
      rules.length
        ? el('div', { class: 'list' }, rules.map((rule) => el('div', { class: 'list-row rules-row' },
          el('div', { class: 'term' }, rule.term),
          el('div', {}, rule.replacement || el('span', { class: 'muted' }, '— без замены —'),
            rule.whole_word ? null : el('span', { class: 'sub' }, ' (и внутри слов)')),
          el('button', {
            class: 'danger', onclick: guard(async () => {
              await del(`/api/books/${book.id}/pronunciations/${rule.id}`);
              load();
            }),
          }, 'Удалить'))))
        : null,
      el('div', { class: 'rules-form' },
        term, replacement,
        el('label', { class: 'row hint' }, wholeWord, 'слово целиком'),
        add));
  }

  const onChange = () => load();
  window.addEventListener('library-changed', onChange);
  load();
  return () => {
    alive = false;
    window.removeEventListener('library-changed', onChange);
  };
}
