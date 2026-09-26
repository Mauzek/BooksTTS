// Книга: слева обложка, кнопки и сведения, справа — название, описание,
// главы и роли. Сюда же добавляются новые главы.

import { api, del, get, importChapters, patch, pickBookFiles, post } from '../api.js';
import { editBookInfo, suggestAndEdit } from '../bookinfo.js';
import { MOOD_PALETTES, avatar, cover, learnBooks, speakerName } from '../covers.js';
import { busyPill, synthJobForBook, synthJobForChapter, watchActiveJobs } from '../jobs-state.js';
import * as player from '../player.js';
import { ask, confirmAction, dropdown, el, emit, formatDuration, formatSpan, guard, icon, plural, popupMenu, toast } from '../ui.js';
import { ensureVoices } from '../voices.js';

const PREVIEW_CHARS = 320;
const DESCRIPTION_CHARS = 320;
const BOOK_EXTENSIONS = ['.txt', '.epub', '.fb2', '.docx', '.pdf'];

let preview = null;  // один голос за раз: новая проба глушит прошлую

function playPreview(url) {
  preview?.pause();
  preview = new Audio(url);
  preview.play().catch(() => toast('Не удалось проиграть пробу голоса', 'error'));
}

/**
 * Размер названия под самое длинное слово: «Реинкарнация» целиком влезает в
 * строку, а не рвётся посреди слова. Ширина буквы Unbounded — около 0,78 кегля.
 */
function titleSize(title, width = 640, largest = 44, smallest = 26) {
  const longest = Math.max(1, ...String(title).split(/\s+/).map((word) => word.length));
  return Math.max(smallest, Math.min(largest, Math.floor(width / (longest * 0.78))));
}

function when(stamp) {
  if (!stamp) return '';
  const date = new Date(`${stamp.replace(' ', 'T')}Z`);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('ru', { day: 'numeric', month: 'long' });
}

export function render(view, bookId) {
  let alive = true;
  const openBoundaries = new Set();
  let showFullDescription = false;
  let current = null;           // последние данные книги — для обновления по задачам
  const busyHosts = new Map();  // глава → место для кнопки или плашки «в очереди»
  let heroBusy = null;          // место под плашку озвучки книги
  const page = el('div', { class: 'page' });
  view.replaceChildren(page);

  const load = guard(async () => {
    const [data, cast, library] = await Promise.all([
      get(`/api/books/${bookId}`),
      get(`/api/books/${bookId}/cast`).catch(() => ({ speakers: [] })),
      get('/api/library').catch(() => ({ folders: [] })),
    ]);
    if (alive) draw(data, cast.speakers, library);
  });

  const changed = () => emit('library-changed');

  function folderPath(library, folderId) {
    const walk = (folders, trail) => {
      for (const folder of folders) {
        const path = [...trail, folder];
        if (folder.id === folderId) return path;
        const deeper = walk(folder.folders, path);
        if (deeper) return deeper;
      }
      return null;
    };
    return folderId ? walk(library.folders || [], []) || [] : [];
  }

  /**
   * Доля прослушанного по главам. Дослушанная глава отмечена в базе и
   * остаётся прослушанной, даже если книгу начали сначала; текущая — по позиции.
   */
  function listened(chapters, playback) {
    const out = new Map();
    for (const chapter of chapters) {
      if (chapter.listened_at) out.set(chapter.id, 1);
      else if (playback && chapter.id === playback.chapter_id && chapter.duration_ms) {
        out.set(chapter.id, Math.min(1, playback.position_ms / chapter.duration_ms));
      } else out.set(chapter.id, 0);
    }
    return out;
  }

  const markupBook = (book, unmarked) => guard(async () => {
    const ok = await confirmAction('Разметить книгу по ролям?',
      `${plural(unmarked, 'глава без разметки уйдёт', 'главы без разметки уйдут', 'глав без разметки уйдут')} ` +
      'в Claude по очереди. Уже размеченные главы и ручные правки не тронутся.', 'Разметить');
    if (!ok) return;
    const job = await post(`/api/books/${book.id}/markup`, {});
    toast(`Задача добавлена: ${job.title}`, 'ok');
    emit('jobs-changed');
  });

  const voiceAll = (book) => guard(async () => {
    const state = await get(`/api/books/${book.id}/readiness`);
    if (!state.marked) {
      const ok = await confirmAction('Книгу пока нечего озвучивать',
        'Сначала главы нужно разметить по ролям — Claude отметит, кто что говорит. Разметить сейчас?', 'Разметить');
      if (ok) {
        const job = await post(`/api/books/${book.id}/markup`, {});
        toast(`Задача добавлена: ${job.title}`, 'ok');
        emit('jobs-changed');
      }
      return;
    }
    // Голоса выбираются тут же, в окне, а не где-то в студии.
    if (!(await ensureVoices(book.id))) return;
    const ok = await confirmAction('Озвучить всю книгу?',
      `${plural(state.marked, 'размеченная глава встанет', 'размеченные главы встанут', 'размеченных глав встанут')} в очередь. ` +
      (state.unmarked.length ? `Неразмеченные (${state.unmarked.length}) пропустятся. ` : '') +
      'Уже озвученные реплики не переозвучиваются, очередь переживёт закрытие приложения.',
      'Озвучить');
    if (!ok) return;
    const job = await post(`/api/books/${book.id}/synthesize`, {});
    toast(`Задача добавлена: ${job.title}`, 'ok');
    emit('jobs-changed');
  });

  // ---------- обложка ----------

  const uploadCover = (book) => guard(async () => {
    const input = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp,image/gif' });
    input.onchange = guard(async () => {
      const file = input.files[0];
      if (!file) return;
      await api(`/api/books/${book.id}/cover`, { method: 'PUT', body: file });
      toast('Обложка обновлена', 'ok');
      changed();
    });
    input.click();
  });

  const removeCover = (book) => guard(async () => {
    const ok = await confirmAction('Убрать картинку обложки?',
      'Вместо неё нарисуется обложка по жанру и настроению книги. Файл картинки удалится из библиотеки.', 'Убрать');
    if (!ok) return;
    await del(`/api/books/${book.id}/cover`);
    toast('Картинка убрана — обложка нарисована заново', 'ok');
    changed();
  });

  function coverMenu(book, anchor) {
    popupMenu(anchor, [
      ['Загрузить свою картинку…', uploadCover(book)],
      ['Убрать картинку', removeCover(book), '', !book.cover_path],
      '-',
      ['Жанр и настроение — рисуют обложку…', guard(() => editBookInfo(book))],
    ]);
  }

  // ---------- главы: добавить ----------

  const addFromFile = (book, chapters) => guard(async () => {
    const items = await pickBookFiles(BOOK_EXTENSIONS, { title: `Добавить главы в «${book.title}»` });
    if (!items.length) return;
    let added = 0;
    for (const item of items) {
      const name = item.path ? item.path.split(/[\\/]/).pop() : item.file.name;
      try {
        const result = await importChapters(book.id, item);
        added += result.chapters.length;
      } catch (error) {
        toast(`${name}: ${error.message}`, 'error');
      }
    }
    if (added) {
      toast(`Добавлено: ${plural(added, 'глава', 'главы', 'глав')} — теперь в книге ${chapters.length + added}`, 'ok');
      changed();
    }
  });

  const addFromText = (book, chapters) => guard(async () => {
    const result = await pasteChapterDialog(book, chapters);
    if (!result) return;
    toast(`Глава «${result.label}» добавлена`, 'ok');
    changed();
  });

  function pasteChapterDialog(book, chapters) {
    return new Promise((resolve) => {
      const dialog = el('dialog', { class: 'paste-dialog' });
      const close = (value) => { dialog.close(); dialog.remove(); resolve(value); };
      const title = el('input', { placeholder: 'можно оставить пустым', 'aria-label': 'Название главы' });
      const text = el('textarea', { rows: '12', placeholder: 'Вставьте текст главы. Если первая строка — «Глава 5. Дорога», она станет названием.', 'aria-label': 'Текст главы' });
      const where = dropdown({
        className: 'wide', label: 'Куда вставить', value: '',
        options: [
          { value: '', label: 'в конец книги' },
          { value: '0', label: 'в начало книги' },
          ...chapters.slice(0, -1).map((c) => ({ value: String(c.number), label: `после главы ${c.number}${c.title ? `. ${c.title}` : ''}` })),
        ],
      });
      const error = el('p', { class: 'form-error', role: 'alert', hidden: true });
      const count = el('span', { class: 'hint' }, '0 знаков');
      text.addEventListener('input', () => { count.textContent = `${text.value.trim().length.toLocaleString('ru')} знаков`; });
      const save = el('button', { class: 'primary lg', type: 'submit' }, icon('plus'), 'Добавить главу');
      const form = el('form', {},
        el('div', { class: 'dialog-head' },
          el('div', { class: 'grow' }, el('h3', {}, 'Новая глава'), el('span', { class: 'hint' }, book.title)),
          el('button', { type: 'button', class: 'round', 'aria-label': 'Закрыть', onclick: () => close(null) }, icon('x'))),
        el('div', { class: 'field-grid' },
          el('label', { class: 'field wide' }, el('span', { class: 'field-label' }, 'Название'), title),
          el('label', { class: 'field wide' }, el('span', { class: 'field-label' }, 'Куда'), where)),
        el('label', { class: 'field' }, el('span', { class: 'field-label' }, 'Текст'), text),
        error,
        el('div', { class: 'buttons' }, count, el('span', { class: 'grow' }),
          el('button', { type: 'button', class: 'ghost lg', onclick: () => close(null) }, 'Отмена'), save));
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!text.value.trim()) { error.textContent = 'Вставьте текст главы.'; error.hidden = false; text.focus(); return; }
        save.disabled = true;
        try {
          const result = await post(`/api/books/${book.id}/chapters`, {
            text: text.value, title: title.value.trim(), after: where.value === '' ? null : Number(where.value),
          });
          close(result.chapters[0]);
        } catch (failure) {
          error.textContent = failure.message;
          error.hidden = false;
          save.disabled = false;
        }
      });
      dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(null); });
      dialog.append(form);
      document.body.append(dialog);
      dialog.showModal();
      text.focus();
    });
  }

  // ---------- отрисовка ----------

  function infoChips(book) {
    const chips = [
      ...(book.genres || []).map((name) => el('span', { class: 'info-chip' }, name)),
      ...(book.moods || []).map((name) => el('span', { class: 'info-chip' },
        el('span', { class: 'mood-dot', style: { background: MOOD_PALETTES[name]?.[0] || 'var(--track)' }, 'aria-hidden': 'true' }), name)),
    ];
    return chips.length ? el('div', { class: 'info-chips' }, chips) : null;
  }

  /** Описание, метки и откуда они. Пусто — приглашение заполнить. */
  function aboutSection(book) {
    const hasInfo = book.description || book.tags?.length || book.genres?.length || book.moods?.length || book.year || book.country;
    if (!hasInfo) {
      return el('section', { class: 'about-empty' },
        el('span', { class: 'about-icon', 'aria-hidden': 'true' }, icon('sparkles', { className: 'big' })),
        el('div', { class: 'grow' },
          el('b', {}, 'Расскажите о книге'),
          el('span', { class: 'hint' }, 'Жанр, настроение, описание и метки. По жанру и настроению нарисуется обложка, по меткам удобно искать. ИИ может заполнить всё сам по названию книги.')),
        el('div', { class: 'about-actions' },
          el('button', { class: 'surface', onclick: guard(() => editBookInfo(book)) }, 'Вручную'),
          el('button', { class: 'primary', onclick: guard(() => suggestAndEdit(book)) }, icon('sparkles'), 'Заполнить с помощью ИИ')));
    }
    const text = book.description || '';
    const long = text.length > DESCRIPTION_CHARS;
    const shown = long && !showFullDescription ? `${text.slice(0, DESCRIPTION_CHARS).replace(/\s+\S*$/, '')}…` : text;
    const source = book.info_source === 'ai'
      ? el('span', {
        class: 'ai-pill', title: `Описание, жанр и метки подобрал ИИ${book.info_at ? ` ${when(book.info_at)}` : ''}. Проверьте и поправьте, если что-то не так.`,
      }, icon('sparkles', { size: 13 }), 'подобрано ИИ')
      : null;
    return el('section', { class: 'section about' },
      el('div', { class: 'section-head' }, el('h2', {}, 'О книге'), source, el('span', { class: 'grow' }),
        book.info_source === 'ai' ? null
          : el('button', { class: 'ghost sm accent', onclick: guard(() => suggestAndEdit(book)), title: 'Жанр, настроение, описание и метки — по названию книги' },
            icon('sparkles', { size: 14 }), 'Дополнить с помощью ИИ'),
        el('button', { class: 'ghost sm', onclick: guard(() => editBookInfo(book)) }, icon('pencil', { size: 14 }), 'Изменить')),
      el('div', { class: 'card-box about-card' },
        text ? el('p', { class: 'about-text' }, shown,
          long ? el('button', {
            class: 'link-button', onclick: () => { showFullDescription = !showFullDescription; load(); },
          }, showFullDescription ? ' свернуть' : ' читать дальше') : null)
          : el('p', { class: 'hint' }, 'Описания пока нет — его можно добавить кнопкой «Изменить».'),
        book.tags?.length ? el('div', { class: 'tag-list' }, book.tags.map((tag) => el('a', {
          class: 'tag', href: `#/library?tag=${encodeURIComponent(tag)}`, title: 'Показать книги с этой меткой',
        }, tag))) : null));
  }

  function draw({ book, chapters, playback }, speakers, library) {
    current = { book, chapters };
    learnBooks([book]);
    busyHosts.clear();
    const voiced = chapters.filter((c) => c.duration_ms).length;
    const unmarked = chapters.filter((c) => !c.segments).length;
    const heard = chapters.filter((c) => c.listened_at).length;
    const duration = chapters.reduce((n, c) => n + (c.duration_ms || 0), 0);
    const progress = listened(chapters, playback);
    const folders = folderPath(library, book.folder_id);

    const title = el('h1', { class: 'book-title editable', title: 'Переименовать', tabindex: '0' }, book.title);
    title.style.fontSize = `${titleSize(book.title)}px`;
    title.onclick = guard(async () => {
      const value = await ask('Название книги', { value: book.title });
      if (value) { await patch(`/api/books/${book.id}`, { title: value }); changed(); }
    });
    const author = el('button', { class: 'book-author link-button', title: 'Изменить автора' },
      book.author || 'автор не указан');
    author.onclick = guard(async () => {
      const value = await ask('Автор', { value: book.author, allowEmpty: true });
      if (value !== null) { await patch(`/api/books/${book.id}`, { author: value }); changed(); }
    });

    const main = voiced
      ? el('button', { class: 'primary xl book-main-action', onclick: guard(() => player.playBook(book.id)) },
        icon('play', { className: 'filled' }), playback?.position_ms || heard ? 'Продолжить' : 'Слушать')
      : el('button', { class: 'primary xl book-main-action', onclick: voiceAll(book) }, icon('mic', { className: 'big' }), 'Озвучить книгу');
    const exportButton = el('button', { class: 'round xl surface', 'aria-label': 'Экспорт в m4b или mp3', title: 'Экспорт в m4b или mp3' },
      icon('download', { className: 'big' }));
    exportButton.onclick = () => exportDialog(book, voiced, chapters.length);
    const more = el('button', { class: 'round xl surface', 'aria-label': 'Ещё', title: 'Ещё' }, icon('ellipsis', { className: 'big' }));
    more.onclick = () => popupMenu(more, [
      ['Озвучить книгу', voiceAll(book), '', !chapters.some((c) => c.segments) || Boolean(synthJobForBook(book.id))],
      [`Разметить по ролям${unmarked ? ` (${unmarked})` : ''}`, markupBook(book, unmarked), '', !unmarked],
      ['Открыть в студии — по шагам', () => { location.hash = `#/studio/book/${book.id}`; }],
      ['Голоса ролей', () => { location.hash = `#/studio/book/${book.id}/voices`; }],
      ['Словарь произношений', () => { location.hash = `#/studio/pronunciation?book=${book.id}`; }],
      '-',
      ['О книге: жанр, настроение, метки…', guard(() => editBookInfo(book))],
      ['Добавить главы из файла…', addFromFile(book, chapters)],
      ['Вставить главу текстом…', addFromText(book, chapters)],
      ['Экспорт…', () => exportDialog(book, voiced, chapters.length)],
      ['Загрузить разметку из файла…', importMarkup(book)],
      '-',
      ['Удалить книгу', guard(async () => {
        const ok = await confirmAction(`Удалить книгу «${book.title}»?`,
          'Удалятся главы, разметка, озвучка и копия файла в библиотеке. Отменить это нельзя.');
        if (!ok) return;
        await del(`/api/books/${book.id}`);
        location.hash = '#/library';
        changed();
      }), 'danger'],
    ]);

    const coverButton = el('button', { class: 'cover-edit', 'aria-label': 'Сменить обложку', title: 'Сменить обложку' },
      icon('image'), el('span', {}, 'Обложка'));
    coverButton.onclick = (event) => { event.stopPropagation(); coverMenu(book, coverButton); };
    heroBusy = el('div', { class: 'book-busy' });

    const facts = [
      ['Автор', book.author || '—'],
      book.year ? ['Год', String(book.year)] : null,
      book.country ? ['Страна', book.country] : null,
      ['Главы', voiced === chapters.length ? `${chapters.length}, все озвучены` : `${chapters.length}, озвучено ${voiced}`],
      duration ? ['Звучит', formatSpan(duration)] : null,
      heard ? ['Прослушано', `${heard} из ${chapters.length}`] : null,
      speakers.length ? ['Роли', String(speakers.length)] : null,
    ].filter(Boolean);

    const side = el('aside', { class: 'book-side' },
      el('div', { class: 'book-cover' }, cover(book), coverButton),
      el('div', { class: 'book-actions' }, main, exportButton, more),
      heroBusy,
      el('dl', { class: 'book-facts' }, facts.flatMap(([label, value]) => [el('dt', {}, label), el('dd', {}, value)])));

    const crumbs = el('nav', { class: 'crumbs', 'aria-label': 'Где лежит книга' },
      el('a', { href: '#/library' }, 'Библиотека'),
      folders.flatMap((folder) => [el('span', { 'aria-hidden': 'true' }, '›'), el('a', { href: `#/library?folder=${folder.id}` }, folder.name)]));

    const roleCards = speakers.map((speaker) => {
      const voice = speaker.voice;
      return el('div', { class: 'role-card' },
        avatar(speaker.name, speaker.slot, 'lg'),
        el('div', { class: 'who' },
          el('b', {}, speakerName(speaker.name)),
          el('span', {}, plural(speaker.count, 'реплика', 'реплики', 'реплик'))),
        voice
          ? el('button', {
            title: speaker.sample ? `Прозвучит реплика из книги: «${speaker.sample}»` : 'Послушать голос',
            onclick: guard(async (event) => {
              const button = event.currentTarget;
              button.classList.add('busy');
              try {
                // Проба — реплика этого персонажа с его темпом и тоном; готовая
                // берётся из кеша, новая записывается за секунду-две.
                const result = await post(`/api/voices/${voice.id}/preview`, {
                  text: speaker.sample || '', rate: speaker.cast?.rate ?? 1,
                  pitch: speaker.cast?.pitch ?? 1, volume: speaker.cast?.volume ?? 1,
                });
                playPreview(result.url);
              } finally {
                button.classList.remove('busy');
              }
            }),
          }, icon('play', { className: 'filled', size: 12 }), voice.display_name || voice.voice_key)
          : el('button', {
            class: 'caution-button', onclick: guard(async () => { if (await ensureVoices(book.id)) load(); }),
          }, icon('mic', { size: 14 }), 'Выбрать голос'));
    });

    const addButton = el('button', { class: 'surface sm', 'aria-haspopup': 'menu' }, icon('plus', { size: 16 }), 'Добавить главы');
    addButton.onclick = () => popupMenu(addButton, [
      ['Из файла — txt, fb2, epub, docx, pdf…', addFromFile(book, chapters)],
      ['Вставить текстом…', addFromText(book, chapters)],
    ]);

    page.replaceChildren(el('div', { class: 'book-page' },
      side,
      el('div', { class: 'book-main' },
        el('header', { class: 'book-head' }, crumbs, title, author, infoChips(book)),
        aboutSection(book),
        el('section', { class: 'section' },
          el('div', { class: 'section-head' }, el('h2', {}, 'Главы'),
            el('span', { class: 'hint' }, plural(chapters.length, 'глава', 'главы', 'глав')),
            el('span', { class: 'grow' }), addButton),
          el('div', { class: 'chapter-rows' },
            chapters.map((chapter, index) => chapterRow(chapter, index === chapters.length - 1, progress.get(chapter.id), chapters.length)))),
        speakers.length
          ? el('section', { class: 'section' },
            el('div', { class: 'section-head' }, el('h2', {}, 'Кто читает'), el('a', { href: `#/studio/book/${book.id}/voices` }, 'настроить голоса')),
            el('div', { class: 'role-cards' }, roleCards))
          : null)));
    drawBusy();
  }

  /**
   * Состояние озвучки в строках глав и у кнопки книги. Обновляется на месте
   * по опросу очереди — страница не перерисовывается целиком.
   */
  function drawBusy() {
    if (!current) return;
    const { book, chapters } = current;
    const bookJob = synthJobForBook(book.id);
    const heroKey = bookJob ? `${bookJob.id}:${bookJob.status}:${Math.round((bookJob.progress || 0) * 100)}` : '';
    if (heroBusy && heroBusy.dataset.key !== heroKey) {
      heroBusy.dataset.key = heroKey;
      heroBusy.replaceChildren(bookJob ? busyPill(bookJob) : '');
    }
    // Задача всей книги идёт по главам подряд: первая неозвученная — сейчас, остальные ждут.
    const waiting = chapters.filter((c) => c.segments && !c.duration_ms);
    for (const chapter of chapters) {
      const host = busyHosts.get(chapter.id);
      if (!host) continue;
      let job = synthJobForChapter(chapter.id, book.id);
      if (job && job.kind === 'book') {
        if (chapter.duration_ms || !chapter.segments) job = null;
        else if (job.status === 'running' && waiting[0] !== chapter) job = { ...job, status: 'pending' };
      }
      const key = job ? `${job.id}:${job.status}:${Math.round((job.progress || 0) * 100)}` : '';
      if (host.dataset.key === key) continue;
      host.dataset.key = key;
      host.replaceChildren(job ? busyPill(job) : host.idle);
    }
  }

  function chapterRow(chapter, isLast, share = 0, total = 1) {
    const open = openBoundaries.has(chapter.id);
    const panel = el('div', { class: 'paragraphs chapter-extra', hidden: !open });
    const done = Boolean(chapter.listened_at);
    const bookId = current.book.id;

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
    const remove = guard(async () => {
      const ok = await confirmAction(`Удалить «${chapter.label}»?`,
        'Удалятся текст главы, её разметка и озвучка. Следующие главы сдвинутся на её место. Отменить это нельзя.');
      if (!ok) return;
      await del(`/api/chapters/${chapter.id}`);
      toast('Глава удалена', 'ok');
      changed();
    });
    const markListened = (value) => guard(async () => {
      await api(`/api/chapters/${chapter.id}/listened`, { method: 'PUT', body: { listened: value } });
      load();
    });
    const voice = guard(async () => {
      if (!(await ensureVoices(bookId, { chapterId: chapter.id }))) return;
      const job = await post(`/api/chapters/${chapter.id}/synthesize`, {});
      toast(`Задача добавлена: ${job.title}`, 'ok');
      emit('jobs-changed');
    });
    const toggle = () => {
      if (openBoundaries.has(chapter.id)) openBoundaries.delete(chapter.id);
      else openBoundaries.add(chapter.id);
      load();
    };

    let sub;
    let subClass = 'sub';
    if (chapter.errors) {
      sub = `не озвучено реплик: ${chapter.errors}`;
      subClass = 'sub warn';
    } else if (chapter.duration_ms) {
      sub = formatDuration(chapter.duration_ms) + (done ? ' · прослушана'
        : share > 0 ? ` · прослушано ${Math.round(share * 100)}%` : '');
    } else if (chapter.segments && chapter.voiced) {
      sub = `озвучено ${chapter.voiced} из ${chapter.segments} реплик`;
    } else if (chapter.segments) {
      sub = `разобрана по ролям · ${plural(chapter.segments, 'реплика', 'реплики', 'реплик')}`;
    } else {
      sub = `ещё не разобрана по ролям · ${chapter.n_chars.toLocaleString('ru')} знаков`;
    }

    // Прослушанная — галочка вместо номера; начатая — кольцо заполняется.
    const ring = el('span', {
      class: `ring${chapter.duration_ms ? '' : ' idle'}${done ? ' done' : ''}`,
      'aria-label': done ? `Глава ${chapter.number}, прослушана` : `Глава ${chapter.number}`,
    }, el('span', {}, chapter.number), done ? el('span', { class: 'ring-badge', 'aria-hidden': 'true' }, icon('check', { size: 11 })) : null);
    ring.style.setProperty('--p', String(done ? 100 : Math.round(share * 100)));

    const primary = chapter.duration_ms
      ? el('button', { class: 'round invert', 'aria-label': `Слушать: ${chapter.label}`, title: 'Слушать', onclick: guard(() => player.playChapter(chapter.id)) },
        icon('play', { className: 'filled', size: 14 }))
      : chapter.segments
        ? el('button', { onclick: voice, title: 'Поставить в очередь озвучки' }, icon('mic'), 'Озвучить')
        : el('a', { class: 'button', href: `#/chapter/${chapter.id}/edit` }, icon('wand-sparkles'), 'Разметить');
    // Место под кнопку: пока глава в очереди, там плашка с её состоянием.
    const host = el('span', { class: 'row-primary' }, primary);
    host.idle = primary;
    busyHosts.set(chapter.id, host);
    const busy = () => Boolean(synthJobForChapter(chapter.id, bookId));
    const moreButton = el('button', { class: 'round ghost', 'aria-label': `Действия: ${chapter.label}`, title: 'Действия' }, icon('ellipsis'));
    moreButton.onclick = () => popupMenu(moreButton, [
      ['Читать вместе', () => { location.hash = `#/chapter/${chapter.id}`; }],
      ['Разметка по ролям', () => { location.hash = `#/chapter/${chapter.id}/edit`; }],
      [busy() ? 'Озвучить главу — уже в очереди' : 'Озвучить главу', voice, '', !chapter.segments || busy()],
      done ? ['Снять отметку «прослушана»', markListened(false)] : ['Отметить прослушанной', markListened(true)],
      '-',
      ['Переименовать', rename],
      [open ? 'Скрыть границы глав' : 'Поправить границы глав', toggle],
      ['Склеить со следующей', merge, '', isLast],
      '-',
      ['Удалить главу', remove, 'danger', total <= 1],
    ]);

    const row = el('div', { class: `chapter-row${done ? ' listened' : ''}` },
      ring,
      el('div', { class: 'info' },
        el('span', { class: 'name' }, chapter.title || `Глава ${chapter.number}`),
        el('span', { class: subClass }, sub)),
      el('div', { class: 'row-actions' }, host, moreButton));
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


  const importMarkup = (book) => guard(async () => {
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

  /** Экспорт: формат карточками, куда ляжет файл, что уже выгружено. */
  function exportDialog(book, voiced, total) {
    const dialog = el('dialog', { class: 'export-dialog', 'aria-labelledby': 'export-title' });
    const close = () => { dialog.close(); dialog.remove(); };
    const option = (kind, iconName, title, text, disabled = false) => el('label', { class: `export-option${disabled ? ' off' : ''}` },
      el('input', { type: 'radio', name: 'export-kind', value: kind, disabled, checked: !disabled && kind === (voiced ? 'm4b' : 'json') }),
      el('span', { class: 'export-icon' }, icon(iconName, { className: 'big' })),
      el('span', {}, el('b', {}, title), el('span', { class: 'hint' }, text)));
    const noAudio = !voiced;
    const files = el('div', { class: 'export-files' }, el('span', { class: 'hint' }, 'Смотрю, что уже выгружено…'));
    const where = el('div', { class: 'export-where' });
    const run = el('button', { class: 'primary lg' }, icon('download', { className: 'big' }), 'Экспортировать');
    run.onclick = guard(async () => {
      const kind = dialog.querySelector('input[name="export-kind"]:checked')?.value;
      if (!kind) return;
      const job = await post(`/api/books/${book.id}/export`, { kind });
      toast(`Задача добавлена: ${job.title}. Файл появится в папке экспорта.`, 'ok');
      emit('jobs-changed');
      close();
    });

    // append у DOM печатает null текстом — пустые части отбрасываем.
    dialog.append(...[
      el('div', { class: 'dialog-head' },
        el('div', { class: 'grow' }, el('h3', { id: 'export-title' }, 'Экспорт книги'),
          el('span', { class: 'hint' }, `${book.title} · озвучено ${voiced} из ${total} ${total === 1 ? 'главы' : 'глав'}`)),
        el('button', { class: 'round', 'aria-label': 'Закрыть', onclick: close }, icon('x'))),
      el('div', { class: 'export-options' },
        option('m4b', 'file-audio', 'Аудиокнига m4b', 'Один файл с главами и обложкой — для плееров аудиокниг и телефона.', noAudio),
        option('mp3', 'folder', 'Папка mp3', 'Файл на каждую главу — откроется в любом плеере.', noAudio),
        option('json', 'file-json', 'Разметка JSON', 'Роли и голоса без звука — чтобы сохранить работу или перенести на другой компьютер.')),
      noAudio ? el('p', { class: 'hint' }, 'Звука пока нет — сначала озвучьте главы. Разметку сохранить можно уже сейчас.')
        : voiced < total ? el('p', { class: 'hint' }, `Неозвученные главы (${total - voiced}) в файл не попадут.`) : null,
      where, files,
      el('div', { class: 'buttons' }, el('button', { class: 'ghost lg', onclick: close }, 'Отмена'), run),
    ].filter(Boolean));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
    dialog.addEventListener('click', (event) => { if (event.target === dialog) close(); });
    document.body.append(dialog);
    dialog.showModal();

    get(`/api/books/${book.id}/exports`).then((data) => {
      const copy = el('button', { class: 'surface sm' }, 'Скопировать путь');
      copy.onclick = guard(async () => { await navigator.clipboard.writeText(data.dir); toast('Путь скопирован', 'ok'); });
      where.replaceChildren(el('code', { class: 'path grow' }, data.dir), copy);
      files.replaceChildren(...(data.files.length
        ? [el('span', { class: 'hint' }, 'Уже выгружено:'), ...data.files.map((file) => el('a', { class: 'export-file', href: file.url, download: file.name },
          icon(file.name.endsWith('.json') ? 'file-json' : 'file-audio', { size: 16 }), el('span', { class: 'grow' }, file.name),
          el('span', { class: 'hint' }, `${(file.size / 1048576).toFixed(1).replace('.', ',')} МБ`)))]
        : []));
    }).catch(() => files.replaceChildren());
  }

  const onChange = () => load();
  window.addEventListener('library-changed', onChange);
  const unwatchJobs = watchActiveJobs(drawBusy);
  load();
  return () => {
    alive = false;
    unwatchJobs();
    window.removeEventListener('library-changed', onChange);
  };
}
