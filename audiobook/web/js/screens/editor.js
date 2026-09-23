// Редактор разметки: текст главы слева, сегменты справа.

import { get, post } from '../api.js';
import { ask, el, emit, guard, icon } from '../ui.js';

const NARRATOR = 'narrator';

function hint() {
  const k = (text) => el('kbd', {}, text);
  return el('p', { class: 'hint' },
    'Выделите текст мышью — он станет отдельным сегментом с выбранным говорящим. ',
    k('1'), '–', k('9'), ' — назначить роль из палитры, ',
    k('↑'), k('↓'), ' — переход между сегментами, ',
    k('Enter'), ' — разделить по курсору, ', k('M'), ' — склеить, ',
    k('Ctrl'), '+клик — выбрать несколько. ',
    'Пунктир — правлено вручную: повторная разметка это не тронет.');
}

function askRename(current) {
  return new Promise((resolve) => {
    const input = el('input', { value: current });
    const wholeBook = el('input', { type: 'checkbox' });
    const dialog = el('dialog');
    const done = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    const form = el('form', { method: 'dialog' },
      el('h3', {}, `Переименовать «${current}»`),
      el('label', { class: 'field' }, input),
      el('label', { class: 'row hint' }, wholeBook, 'во всей книге'),
      el('div', { class: 'buttons' },
        el('button', { type: 'button', onclick: () => done(null) }, 'Отмена'),
        el('button', { class: 'primary', type: 'submit' }, 'Переименовать')));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const name = input.value.trim();
      done(name ? { name, wholeBook: wholeBook.checked } : null);
    });
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); done(null); });
    dialog.append(form);
    document.body.append(dialog);
    dialog.showModal();
    input.select();
  });
}

export function render(view, chapterId) {
  const state = { data: null, picked: new Set(), alive: true };

  const status = el('span', { class: 'editor-status' });
  const bookLink = el('a', { class: 'crumb', href: '#/' }, '…');
  const chapterSelect = el('select', { title: 'Глава' });
  const markupButton = el('button', { class: 'primary' }, icon('wand-sparkles'), 'Разметить');
  const undoButton = el('button', { class: 'icon-button', title: 'Отменить (Ctrl+Z)', disabled: true },
    icon('undo-2'));
  const redoButton = el('button', { class: 'icon-button', title: 'Повторить (Ctrl+Shift+Z)', disabled: true },
    icon('redo-2'));
  const textHost = el('div', { class: 'editor-text' }, el('p', { class: 'hint' }, 'Загружаю главу…'));
  const castHost = el('div', { class: 'cast' });
  const splitButton = el('button', { disabled: true, title: 'Разделить по курсору (Enter)' },
    icon('scissors'), 'Разделить');
  const mergeButton = el('button', { disabled: true, title: 'Склеить выбранные (M)' },
    icon('merge'), 'Склеить');
  const renameButton = el('button', { disabled: true }, icon('pencil'), 'Переименовать…');
  const newSpeakerButton = el('button', {}, icon('user-round'), 'Новый персонаж…');
  const segmentsHost = el('div');
  const checksHost = el('div', { class: 'checks' });

  view.replaceChildren(el('div', { class: 'editor' },
    el('div', { class: 'editor-bar' },
      bookLink, el('span', { class: 'muted' }, '›'), chapterSelect,
      markupButton, undoButton, redoButton, el('span', { class: 'grow' }), status),
    el('div', { class: 'editor-panes' },
      el('section', { class: 'pane' }, el('h2', {}, 'Текст главы'), textHost),
      el('section', { class: 'pane' },
        el('h2', {}, 'Сегменты'),
        castHost,
        el('div', { class: 'cast' }, splitButton, mergeButton, renameButton, newSpeakerButton),
        segmentsHost, checksHost, hint())),
  ));

  function say(text, kind = '') {
    status.textContent = text;
    status.className = `editor-status ${kind}`;
  }

  // ---------- загрузка ----------

  async function load() {
    const data = await get(`/api/chapters/${chapterId}`);
    if (!state.alive) return;
    apply(data);
    emit('active-book', data.book.id);
    loadChecks();
  }

  function apply(data) {
    state.data = data;
    const ids = new Set(data.segments.map((s) => s.id));
    state.picked = new Set([...state.picked].filter((id) => ids.has(id)));
    bookLink.textContent = data.book.title;
    bookLink.href = `#/book/${data.book.id}`;
    chapterSelect.replaceChildren(...data.chapters.map((c) =>
      el('option', { value: c.id, selected: c.id === data.chapter.id }, c.label)));
    renderText();
    renderSegments();
    renderCast();
    const history = data.history || {};
    undoButton.disabled = !history.can_undo;
    redoButton.disabled = !history.can_redo;
    if (history.description) say(history.description, 'ok');
    updateToolbar();
  }

  function redraw() {
    renderText();
    renderSegments();
    updateToolbar();
  }

  // ---------- текст главы ----------

  function renderText() {
    const { text } = state.data.chapter;
    const nodes = [];
    let cursor = 0;
    for (const segment of state.data.segments) {
      if (segment.char_start === null || segment.char_start === undefined) continue;
      if (segment.char_start > cursor) nodes.push(el('span', { class: 'gap' }, text.slice(cursor, segment.char_start)));
      const classes = ['seg'];
      if (segment.speaker === NARRATOR) classes.push('narr');
      if (segment.is_manual) classes.push('manual');
      if (state.picked.has(segment.id)) classes.push('pick');
      nodes.push(el('span', {
        class: classes.join(' '),
        style: segment.speaker === NARRATOR ? null : { color: segment.color },
        dataset: { id: segment.id, start: segment.char_start },
        title: `${segment.speaker} · ${segment.emotion}`,
        onclick: (event) => pick(segment.id, event),
      }, text.slice(segment.char_start, segment.char_end)));
      cursor = segment.char_end;
    }
    if (cursor < text.length) nodes.push(el('span', { class: 'gap' }, text.slice(cursor)));
    textHost.replaceChildren(...nodes);
  }

  /** Смещения выделения в координатах текста главы. */
  function selectionRange() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
    const range = selection.getRangeAt(0);
    if (!textHost.contains(range.commonAncestorContainer)) return null;
    const point = (node, offset) => {
      const host = node.nodeType === 3 ? node.parentElement : node;
      const span = host?.closest?.('[data-start]');
      return span ? Number(span.dataset.start) + offset : null;
    };
    const from = point(range.startContainer, range.startOffset);
    const to = point(range.endContainer, range.endOffset);
    if (from === null || to === null) return null;
    return { start: Math.min(from, to), end: Math.max(from, to) };
  }

  /** Позиция курсора для разделения сегмента. */
  function caretOffset() {
    const selection = window.getSelection();
    if (!selection || !selection.rangeCount) return null;
    const node = selection.anchorNode;
    if (!node || !textHost.contains(node)) return null;
    const host = node.nodeType === 3 ? node.parentElement : node;
    const span = host?.closest?.('.seg[data-id]');
    if (!span) return null;
    return { segmentId: Number(span.dataset.id), offset: selection.anchorOffset };
  }

  // ---------- сегменты ----------

  function renderSegments() {
    segmentsHost.replaceChildren(...state.data.segments.map((segment) => {
      const who = el('div', {
        class: 'who',
        style: { color: segment.speaker === NARRATOR ? 'var(--muted)' : segment.color },
      }, segment.speaker);
      if (segment.emotion && segment.emotion !== 'нейтрально') who.append(el('span', { class: 'tag' }, segment.emotion));
      if (segment.is_manual) who.append(el('span', { class: 'tag' }, '✎ вручную'));
      if (segment.error) who.append(el('span', { class: 'tag err' }, `⚠ ${segment.error}`));
      const classes = ['seg-row'];
      if (segment.speaker === NARRATOR) classes.push('narr');
      if (state.picked.has(segment.id)) classes.push('pick');
      return el('div', { class: classes.join(' '), dataset: { id: segment.id }, onclick: (event) => pick(segment.id, event) },
        el('span', { class: 'n' }, segment.order),
        el('div', {}, who, el('div', { class: 'say' }, segment.text)));
    }));
  }

  function renderCast() {
    castHost.replaceChildren(...state.data.speakers.map((speaker, index) => el('button', {
      class: 'chip', title: `Назначить «${speaker.name}» выбранному`, onclick: () => assign(speaker.name),
    },
      el('span', { class: 'dot', style: { background: speaker.color } }),
      el('span', {}, speaker.name),
      el('span', { class: 'cnt' }, speaker.count),
      index < 9 ? el('span', { class: 'key' }, index + 1) : null)));
  }

  const loadChecks = guard(async () => {
    const data = await get(`/api/chapters/${chapterId}/checks`);
    if (!state.alive) return;
    if (!data.findings.length) {
      checksHost.replaceChildren(el('p', { class: 'hint' }, 'Проверки пройдены.'));
      return;
    }
    checksHost.replaceChildren(...data.findings.map((finding) => el('div', {
      class: `check ${finding.severity}`,
      onclick: () => {
        state.picked = new Set(finding.segment_ids);
        redraw();
        segmentsHost.querySelector('.seg-row.pick')?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      },
    }, icon(finding.severity === 'blocker' ? 'circle-alert' : 'triangle-alert', { className: 'mark' }),
      finding.message)));
  });

  // ---------- выбор ----------

  function pick(id, event) {
    if (event && (event.ctrlKey || event.metaKey || event.shiftKey)) {
      if (state.picked.has(id)) state.picked.delete(id); else state.picked.add(id);
    } else {
      if (selectionRange()) return; // выделение мышью важнее клика
      state.picked = new Set([id]);
    }
    redraw();
  }

  function step(delta) {
    const { segments } = state.data;
    if (!segments.length) return;
    const current = [...state.picked].at(-1);
    const index = segments.findIndex((s) => s.id === current);
    const next = segments[Math.min(segments.length - 1, Math.max(0, index + delta))];
    state.picked = new Set([next.id]);
    redraw();
    segmentsHost.querySelector('.seg-row.pick')?.scrollIntoView({ block: 'nearest' });
  }

  function updateToolbar() {
    const count = state.picked.size;
    splitButton.disabled = count !== 1;
    mergeButton.disabled = count < 2;
    renameButton.disabled = count !== 1;
  }

  // ---------- операции ----------

  async function edit(path, payload) {
    try {
      apply(await post(`/api/chapters/${chapterId}/${path}`, payload || {}));
      loadChecks();
    } catch (error) {
      say(error.message, 'error');
    }
  }

  function assign(speaker) {
    const range = selectionRange();
    if (range) {
      window.getSelection().removeAllRanges();
      return edit('assign-range', { char_start: range.start, char_end: range.end, speaker });
    }
    if (!state.picked.size) return say('нечего назначать: выберите сегмент или выделите текст', 'error');
    return edit('assign', { segment_ids: [...state.picked], speaker });
  }

  function splitAtCaret() {
    const caret = caretOffset();
    if (!caret || !caret.offset) return say('поставьте курсор внутри сегмента', 'error');
    return edit('split', { segment_id: caret.segmentId, offset: caret.offset });
  }

  markupButton.onclick = async () => {
    markupButton.disabled = true;
    say('размечаю, это может занять с полминуты…');
    try {
      const data = await post(`/api/chapters/${chapterId}/markup`, {});
      apply(data);
      loadChecks();
      emit('library-changed');
      const issues = (data.markup?.issues || []).length;
      say(issues ? `размечено, замечаний: ${issues}` : 'размечено', issues ? 'error' : 'ok');
    } catch (error) {
      say(error.message, 'error');
    } finally {
      markupButton.disabled = false;
    }
  };

  chapterSelect.onchange = () => { location.hash = `#/chapter/${chapterSelect.value}/edit`; };
  undoButton.onclick = () => edit('undo');
  redoButton.onclick = () => edit('redo');
  splitButton.onclick = splitAtCaret;
  mergeButton.onclick = () => edit('merge', { segment_ids: [...state.picked] });
  newSpeakerButton.onclick = async () => {
    const name = await ask('Новый персонаж', { placeholder: 'Имя' });
    if (name) assign(name);
  };
  renameButton.onclick = async () => {
    const current = state.data.segments.find((s) => state.picked.has(s.id));
    if (!current) return;
    const answer = await askRename(current.speaker);
    if (answer) edit('rename', { old: current.speaker, new: answer.name, whole_book: answer.wholeBook });
  };

  function onKey(event) {
    if (!state.data || document.querySelector('dialog[open]')) return;
    if (event.target.matches?.('input, select, textarea')) return;

    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
      event.preventDefault();
      edit(event.shiftKey ? 'redo' : 'undo');
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return;

    if (event.key >= '1' && event.key <= '9') {
      const speaker = state.data.speakers[Number(event.key) - 1];
      if (speaker) { event.preventDefault(); assign(speaker.name); }
      return;
    }
    if (event.key === 'ArrowDown') { event.preventDefault(); step(1); }
    if (event.key === 'ArrowUp') { event.preventDefault(); step(-1); }
    if (event.key === 'Enter') { event.preventDefault(); splitAtCaret(); }
    if (event.key.toLowerCase() === 'm' && state.picked.size > 1) {
      event.preventDefault();
      edit('merge', { segment_ids: [...state.picked] });
    }
  }

  document.addEventListener('keydown', onKey);
  load().catch((error) => say(error.message, 'error'));

  return () => {
    state.alive = false;
    document.removeEventListener('keydown', onKey);
  };
}
