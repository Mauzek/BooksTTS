// Разметка главы: текст как в книге, реплики подсвечены цветом говорящего.
// Нажали на фразу — под абзацем открывается панель: кто говорит, интонация,
// прослушать, разделить. Справа — роли с клавишами, проверки, подсказки.

import { get, post, put, waitForJob } from '../api.js';
import { avatar, pickRoleColor, speakerName } from '../covers.js';
import { busyPill, synthJobForChapter, watchActiveJobs } from '../jobs-state.js';
import * as player from '../player.js';
import { ask, confirmAction, dropdown, el, emit, guard, icon, plural, popupMenu, toast } from '../ui.js';
import { ensureVoices, setVoice, voiceCatalog, voiceSelect } from '../voices.js';

const NARRATOR = 'narrator';
const DEFAULT_EMOTIONS = ['нейтрально', 'радостно', 'зло', 'грустно'];

function askRename(current) {
  return new Promise((resolve) => {
    const input = el('input', { value: current });
    const wholeBook = el('input', { type: 'checkbox' });
    const dialog = el('dialog');
    const done = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    const form = el('form', { method: 'dialog' },
      el('h3', {}, `Переименовать «${speakerName(current)}»`),
      el('label', { class: 'field' }, input),
      el('label', { class: 'row hint' }, wholeBook, 'во всей книге'),
      el('div', { class: 'buttons' },
        el('button', { type: 'button', class: 'ghost', onclick: () => done(null) }, 'Отмена'),
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

/** Объединить роль с другой: «Руди» — это «Рудеус». */
function askMerge(current, others) {
  return new Promise((resolve) => {
    const select = dropdown({
      className: 'wide', label: 'С какой ролью объединить', value: others[0],
      options: others.map((name) => ({ value: name, label: speakerName(name) })),
    });
    const wholeBook = el('input', { type: 'checkbox', checked: true });
    const dialog = el('dialog');
    const done = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    const form = el('form', { method: 'dialog' },
      el('h3', {}, `Объединить «${speakerName(current)}» с другой ролью`),
      el('p', { class: 'muted' }, 'Реплики перейдут выбранной роли и зазвучат её голосом.'),
      el('label', { class: 'field' }, select),
      el('label', { class: 'row hint' }, wholeBook, 'во всей книге'),
      el('div', { class: 'buttons' },
        el('button', { type: 'button', class: 'ghost', onclick: () => done(null) }, 'Отмена'),
        el('button', { class: 'primary', type: 'submit' }, 'Объединить')));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      done({ name: select.value, wholeBook: wholeBook.checked });
    });
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); done(null); });
    dialog.append(form);
    document.body.append(dialog);
    dialog.showModal();
  });
}

function tint(node, slot) {
  const n = Number(slot) || 0;
  node.style.setProperty('--seg', `var(--c-${n})`);
  node.style.setProperty('--seg-on', `var(--c-${n}-on)`);
  return node;
}

export function render(view, chapterId) {
  const state = {
    data: null, picked: new Set(), alive: true, emotions: DEFAULT_EMOTIONS,
    cast: new Map(),   // роль → назначение голоса (из голосов книги)
    catalog: null,     // каталог голосов для выбора прямо в списке ролей
  };

  const status = el('span', { class: 'editor-status', role: 'status' });
  // Путь наверху: откуда эта страница и куда с неё вернуться.
  const bookLink = el('a', { href: '#/studio' }, '…');
  const crumbs = el('nav', { class: 'crumbs', 'aria-label': 'Путь' },
    el('a', { href: '#/studio' }, 'Студия'), el('span', { 'aria-hidden': 'true' }, '›'),
    bookLink, el('span', { 'aria-hidden': 'true' }, '›'), el('span', { class: 'here' }, 'Разметка по ролям'));
  const chapterTitle = el('h1', { class: 'editor-title' });
  const chapterPick = dropdown({ label: 'Другая глава', className: 'chapter-pick' });
  const undoButton = el('button', { class: 'round surface', 'aria-label': 'Отменить', title: 'Отменить (Ctrl+Z)', disabled: true }, icon('undo-2'));
  const redoButton = el('button', { class: 'round surface', 'aria-label': 'Повторить', title: 'Повторить (Ctrl+Shift+Z)', disabled: true }, icon('redo-2'));
  const markupButton = el('button', { class: 'surface' }, icon('wand-sparkles'), 'Разобрать заново');
  const voiceButton = el('button', { class: 'primary' }, icon('mic'), 'Озвучить главу');
  // Кнопка «Озвучить» или, если глава уже в очереди, плашка с её состоянием.
  const voiceHost = el('div', { class: 'voice-host' }, voiceButton);
  // Панель над выделением: кому отдать выделенный текст.
  const selPop = el('div', { class: 'sel-pop', role: 'toolbar', 'aria-label': 'Кто говорит выделенное', hidden: true });
  const textHost = el('article', { class: 'markup-text', 'aria-label': 'Текст главы' }, el('p', { class: 'hint' }, 'Загружаю главу…'));
  const rolesHost = el('div', { class: 'markup-roles' });
  const checksHost = el('div', { class: 'markup-checks' });

  view.replaceChildren(el('div', { class: 'page markup-page' },
    el('header', { class: 'editor-head' },
      crumbs,
      el('div', { class: 'editor-heading' }, chapterTitle, chapterPick)),
    // Панель инструментов: слева правка, справа — что делать с главой дальше.
    el('div', { class: 'editor-toolbar' },
      el('div', { class: 'history-pair' }, undoButton, redoButton),
      status,
      el('span', { class: 'grow' }),
      markupButton, voiceHost),
    el('div', { class: 'markup-layout' },
      textHost,
      el('aside', { class: 'studio-side' },
        el('section', { class: 'card-box side-card' }, el('h2', {}, 'Роли в главе'), rolesHost),
        el('section', { class: 'card-box side-card' }, el('h2', {}, 'Перед озвучкой'), checksHost),
        el('section', { class: 'dashed-card hint' },
          el('p', { class: 'tips' },
            'Нажмите на фразу, чтобы сменить говорящего, или клавишу 1–9. ',
            'Выделите любой кусок текста мышью — над ним появятся роли: выделенное станет отдельной репликой.'),
          el('p', { class: 'tips' }, 'Серым подчёркнут текст, который не попал ни в одну реплику: нажмите на него, чтобы назначить роль.'),
          el('p', { class: 'tips' },
            el('kbd', {}, '↑'), ' ', el('kbd', {}, '↓'), ' — по репликам, ', el('kbd', {}, 'Enter'), ' — разделить по курсору, ',
            el('kbd', {}, 'M'), ' — склеить выбранные, ', el('kbd', {}, 'Ctrl'), '+клик — выбрать несколько.'),
          el('p', { class: 'tips' }, 'Пунктир — правлено вручную: повторная разметка это не тронет.'))))));

  // Сообщение в панели: что сделано или что пошло не так. Само гаснет.
  let sayTimer = null;
  function say(text, kind = '') {
    clearTimeout(sayTimer);
    status.textContent = text;
    status.className = `editor-status ${kind}${text ? ' shown' : ''}`;
    if (text && kind !== 'busy') sayTimer = setTimeout(() => say(''), kind === 'error' ? 7000 : 4000);
  }

  // ---------- загрузка ----------

  async function load() {
    const [data, config] = await Promise.all([
      get(`/api/chapters/${chapterId}`),
      state.emotions === DEFAULT_EMOTIONS ? get('/api/config').catch(() => ({})) : Promise.resolve({}),
    ]);
    if (!state.alive) return;
    if (config.emotions?.length) state.emotions = config.emotions;
    apply(data);
    loadChecks();
    loadCast();
  }

  /** Голоса ролей книги и каталог — чтобы выбрать голос прямо в списке ролей. */
  const loadCast = guard(async () => {
    const [cast, catalog] = await Promise.all([
      get(`/api/books/${state.data.book.id}/cast`),
      voiceCatalog().catch(() => null),
    ]);
    if (!state.alive) return;
    state.cast = new Map(cast.speakers.map((speaker) => [speaker.name, speaker]));
    state.catalog = catalog;
    renderRoles();
  });

  function apply(data) {
    state.data = data;
    const ids = new Set(data.segments.map((s) => s.id));
    state.picked = new Set([...state.picked].filter((id) => ids.has(id)));
    bookLink.textContent = data.book.title;
    bookLink.href = `#/studio/book/${data.book.id}/roles`;
    chapterTitle.textContent = data.chapter.label;
    chapterPick.setOptions(data.chapters.map((c) => ({ value: c.id, label: c.label })), data.chapter.id);
    renderText();
    renderRoles();
    drawVoiceHost();
    // Пока разметки нет — «Разобрать по ролям»; «заново» — только когда есть что переделывать.
    const fresh = !data.segments.length;
    markupButton.className = fresh ? 'primary' : 'surface';
    markupButton.replaceChildren(icon('wand-sparkles'), fresh ? 'Разобрать по ролям' : 'Разобрать заново');
    const history = data.history || {};
    undoButton.disabled = !history.can_undo;
    redoButton.disabled = !history.can_redo;
    if (history.description) say(history.description, 'ok');
  }

  // ---------- текст главы ----------

  function picked() {
    return state.data.segments.filter((s) => state.picked.has(s.id));
  }

  /** Панель под абзацем выбранной реплики. */
  function panel() {
    const chosen = picked();
    if (!chosen.length) return null;
    const one = chosen.length === 1 ? chosen[0] : null;
    const speakers = state.data.speakers;
    const who = el('div', { class: 'panel-row' },
      el('span', { class: 'panel-label' }, chosen.length > 1 ? `Кто говорит (${chosen.length})` : 'Кто говорит'),
      ...speakers.map((speaker, index) => tint(el('button', {
        class: 'choice', 'aria-pressed': String(chosen.every((s) => s.speaker === speaker.name)),
        onclick: () => assign(speaker.name), title: index < 9 ? `Клавиша ${index + 1}` : '',
      }, el('span', { class: 'dot', 'aria-hidden': 'true' }), speakerName(speaker.name)), speaker.slot)),
      el('button', { class: 'choice', onclick: newSpeaker }, icon('plus', { size: 14 }), 'Новый'));
    const mood = el('div', { class: 'panel-row' },
      el('span', { class: 'panel-label' }, 'Интонация'),
      ...state.emotions.map((emotion) => el('button', {
        class: 'choice', 'aria-pressed': String(chosen.every((s) => s.emotion === emotion)),
        onclick: () => edit('emotion', { segment_ids: chosen.map((s) => s.id), emotion }),
      }, emotion)));
    const actions = el('div', { class: 'panel-actions' },
      one ? el('button', { class: 'surface sm', onclick: () => listen(one) }, icon('play', { className: 'filled', size: 12 }), 'Прослушать') : null,
      one ? el('button', { class: 'surface sm', onclick: splitAtCaret, title: 'Поставьте курсор в фразу и нажмите (Enter)' }, icon('scissors', { size: 14 }), 'Разделить') : null,
      chosen.length > 1 ? el('button', { class: 'surface sm', onclick: () => edit('merge', { segment_ids: chosen.map((s) => s.id) }) }, icon('merge', { size: 14 }), 'Склеить') : null,
      one ? el('button', { class: 'surface sm', onclick: () => rename(one) }, icon('pencil', { size: 14 }), 'Переименовать роль…') : null,
      el('span', { class: 'grow' }),
      one?.error ? el('span', { class: 'panel-error' }, icon('circle-alert', { size: 14 }), one.error) : null);
    return el('div', { class: 'seg-panel', contenteditable: 'false' }, who, mood, actions);
  }

  function renderText() {
    const { text } = state.data.chapter;
    const nodes = [];
    let cursor = 0;
    let panelDue = false;
    const lastPicked = [...state.picked].at(-1);
    const placePanel = () => { const node = panel(); if (node) nodes.push(node); panelDue = false; };

    // Текст между репликами. Если в нём есть слова, разметка его пропустила:
    // такой кусок подчёркнут, и по нажатию ему можно назначить роль.
    const gapSpan = (gap, start) => {
      const loose = /[\p{L}\p{N}]/u.test(gap);
      return el('span', {
        class: `gap${loose ? ' loose' : ''}`, dataset: { start: String(start) },
        title: loose ? 'Этот текст не попал ни в одну реплику — нажмите, чтобы назначить роль' : null,
        onclick: loose ? (event) => pickGap(event.currentTarget) : null,
      }, gap);
    };
    const pushGap = (gap, start) => {
      if (panelDue) {
        // Панель встаёт в конец абзаца — там, где в тексте пустая строка.
        const at = gap.indexOf('\n');
        if (at >= 0) {
          if (at > 0) nodes.push(gapSpan(gap.slice(0, at), start));
          placePanel();
          const lead = gap.slice(at).match(/^\n+/)[0].length;
          const rest = gap.slice(at + lead);
          if (rest) nodes.push(gapSpan(rest, start + at + lead));
          return;
        }
      }
      nodes.push(gapSpan(gap, start));
    };

    for (const segment of state.data.segments) {
      if (segment.char_start === null || segment.char_start === undefined) continue;
      if (segment.char_start > cursor) pushGap(text.slice(cursor, segment.char_start), cursor);
      const classes = ['seg'];
      if (segment.speaker === NARRATOR) classes.push('narr');
      if (segment.is_manual) classes.push('manual');
      if (segment.error) classes.push('err');
      if (state.picked.has(segment.id)) classes.push('pick');
      if (segment.speaker !== NARRATOR) {
        nodes.push(tint(el('span', { class: 'seg-mark', 'aria-hidden': 'true' }, speakerName(segment.speaker).charAt(0)), segment.slot));
      }
      nodes.push(tint(el('span', {
        class: classes.join(' '),
        dataset: { id: segment.id, start: segment.char_start },
        title: `${speakerName(segment.speaker)} · ${segment.emotion}`,
        onclick: (event) => pick(segment.id, event),
      }, text.slice(segment.char_start, segment.char_end)), segment.slot));
      if (segment.emotion && segment.emotion !== state.emotions[0] && segment.speaker !== NARRATOR) {
        nodes.push(el('span', { class: 'seg-mood', 'aria-hidden': 'true' }, segment.emotion));
      }
      if (segment.id === lastPicked) panelDue = true;
      cursor = segment.char_end;
    }
    if (cursor < text.length) pushGap(text.slice(cursor), cursor);
    if (panelDue) placePanel();
    if (!state.data.segments.length) {
      nodes.unshift(el('div', { class: 'seg-panel' }, el('b', {}, 'Глава ещё не разобрана по ролям.'),
        el('span', { class: 'hint' }, 'Нажмите «Разобрать по ролям» — Claude отметит, кто что говорит. Или выделите текст мышью — над ним появятся роли.')));
    }
    textHost.replaceChildren(...nodes);
  }

  /** Смещения выделения в координатах текста главы. */
  function selectionRange() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
    const range = selection.getRangeAt(0);
    if (!textHost.contains(range.commonAncestorContainer)) return null;
    const from = textPoint(range.startContainer, range.startOffset, 'start');
    const to = textPoint(range.endContainer, range.endOffset, 'end');
    if (from === null || to === null || to <= from) return null;
    return { start: from, end: to };
  }

  /**
   * Точка выделения → смещение в тексте главы. Внутри реплики или
   * пропущенного куска — точно; на значке роли или интонации (их нет в
   * тексте) — ближайшая граница текста в нужную сторону.
   */
  function textPoint(node, offset, edge) {
    const host = node.nodeType === 3 ? node.parentElement : null;
    if (host?.dataset.start !== undefined) return Number(host.dataset.start) + offset;
    const spans = [...textHost.querySelectorAll('[data-start]')];
    const probe = document.createRange();
    probe.setStart(node, offset);
    if (edge === 'start') {
      const after = spans.find((span) => probe.comparePoint(span, 0) >= 0);
      return after ? Number(after.dataset.start) : null;
    }
    const before = spans.filter((span) => probe.comparePoint(span, 0) < 0).at(-1);
    return before ? Number(before.dataset.start) + before.textContent.length : null;
  }

  /** Нажали на пропущенный разметкой текст — выделить его и предложить роли. */
  function pickGap(span) {
    if (selectionRange()) return;
    const text = span.textContent;
    const lead = text.length - text.trimStart().length;
    const range = document.createRange();
    range.setStart(span.firstChild, lead);
    range.setEnd(span.firstChild, text.trimEnd().length);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    showSelectionPop();
  }

  /** Панель ролей над выделением. */
  function showSelectionPop() {
    const range = selectionRange();
    if (!range || !state.data) { hideSelectionPop(); return; }
    const keep = (event) => event.preventDefault();  // клик по панели не снимает выделение
    selPop.replaceChildren(
      el('span', { class: 'sel-pop-label' }, 'Кто говорит:'),
      ...state.data.speakers.map((speaker, index) => tint(el('button', {
        class: 'choice', onmousedown: keep, onclick: () => assign(speaker.name),
        title: index < 9 ? `Клавиша ${index + 1}` : '',
      }, el('span', { class: 'dot', 'aria-hidden': 'true' }), speakerName(speaker.name),
      index < 9 ? el('kbd', {}, String(index + 1)) : null), speaker.slot)),
      el('button', { class: 'choice', onmousedown: keep, onclick: newSpeaker }, icon('plus', { size: 14 }), 'Новый'));
    selPop.hidden = false;
    const rect = window.getSelection().getRangeAt(0).getBoundingClientRect();
    const { width, height } = selPop.getBoundingClientRect();
    const above = rect.top - height - 10;
    selPop.style.top = `${above > 8 ? above : rect.bottom + 10}px`;
    selPop.style.left = `${Math.max(8, Math.min(innerWidth - width - 8, rect.left + rect.width / 2 - width / 2))}px`;
  }

  function hideSelectionPop() {
    selPop.hidden = true;
  }

  /** Позиция курсора для разделения реплики. */
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

  // ---------- роли и проверки ----------

  function renderRoles() {
    if (!state.data) return;
    const bookId = state.data.book.id;
    rolesHost.replaceChildren(
      ...state.data.speakers.map((speaker, index) => {
        const info = state.cast.get(speaker.name) || { name: speaker.name };
        const more = el('button', {
          class: 'round ghost sm', 'aria-label': `Действия с ролью ${speakerName(speaker.name)}`, title: 'Переименовать, объединить, убрать',
        }, icon('ellipsis'));
        more.onclick = () => popupMenu(more, roleMenu(speaker));
        const select = state.catalog ? voiceSelect(info, state.catalog, guard(async (voiceId) => {
          await setVoice(bookId, info, voiceId);
          await loadCast();
          loadChecks();
          say(voiceId ? `голос для ${speakerName(speaker.name)} назначен` : `голос снят`, 'ok');
        }), { className: 'voice-select mini' }) : null;
        return el('div', { class: `role-line${select && !info.voice ? ' no-voice' : ''}`, dataset: { role: speaker.name } },
          el('button', {
            class: 'role-key', onclick: () => assign(speaker.name),
            title: `Назначить «${speakerName(speaker.name)}» выбранному`,
          },
            avatar(speaker.name, speaker.slot, 'md'),
            el('span', { class: 'grow' }, speakerName(speaker.name)),
            el('span', { class: 'hint' }, String(speaker.count)),
            index < 9 ? el('kbd', {}, String(index + 1)) : null),
          more,
          select);
      }),
      el('button', { class: 'ghost sm', onclick: newSpeaker }, icon('plus', { size: 14 }), 'Новый персонаж…'));
  }

  /** Что можно сделать с ролью. Рассказчика нельзя убрать — ему и отдают реплики. */
  function roleMenu(speaker) {
    const others = state.data.speakers.map((s) => s.name).filter((name) => name !== speaker.name);
    return [
      ['Цвет роли…', guard(async () => {
        const anchor = rolesHost.querySelector(`[data-role="${CSS.escape(speaker.name)}"]`) || rolesHost;
        const slot = await pickRoleColor(anchor, speaker.slot);
        if (slot === null || slot === speaker.slot) return;
        await put(`/api/books/${state.data.book.id}/role-color`, { speaker: speaker.name, slot });
        await load();
        say(`цвет роли «${speakerName(speaker.name)}» изменён`, 'ok');
      })],
      ['Переименовать…', () => rename(speaker)],
      ['Объединить с другой ролью…', guard(async () => {
        const answer = await askMerge(speaker.name, others);
        if (answer) edit('rename', { old: speaker.name, new: answer.name, whole_book: answer.wholeBook });
      }), '', !others.length],
      speaker.name === NARRATOR ? null : ['Отдать реплики рассказчику', guard(async () => {
        const ok = await confirmAction(`Убрать роль «${speakerName(speaker.name)}»?`,
          `Её реплики во всей книге (${plural(speaker.count, 'реплика', 'реплики', 'реплик')} в этой главе) прочитает рассказчик. Отменить можно кнопкой «Отменить».`,
          'Отдать рассказчику');
        if (ok) edit('rename', { old: speaker.name, new: NARRATOR, whole_book: true });
      })],
    ].filter(Boolean);
  }

  const loadChecks = guard(async () => {
    const data = await get(`/api/chapters/${chapterId}/checks`);
    if (!state.alive) return;
    const segments = state.data?.segments || [];
    const good = [];
    if (segments.length && !data.findings.some((f) => f.kind === 'no_markup')) good.push(`Все ${plural(segments.length, 'реплика размечена', 'реплики размечены', 'реплик размечены')}`);
    if (segments.length && !data.findings.some((f) => f.kind === 'no_voice')) good.push('У каждой роли есть голос');
    const noVoice = data.findings.some((f) => f.kind === 'no_voice');
    checksHost.replaceChildren(
      ...good.map((text) => el('div', { class: 'check-line ok' }, el('span', { class: 'check-mark' }, icon('check', { size: 14 })), text)),
      noVoice ? el('button', {
        class: 'primary sm check-fix',
        onclick: guard(async () => {
          if (await ensureVoices(state.data.book.id, { chapterId })) { await loadCast(); loadChecks(); }
        }),
      }, icon('mic', { size: 14 }), 'Выбрать голоса') : '',
      ...data.findings.map((finding) => el('button', {
        class: `check-line ${finding.severity}`,
        onclick: () => {
          if (!finding.segment_ids?.length) return;
          state.picked = new Set(finding.segment_ids);
          renderText();
          textHost.querySelector('.seg.pick')?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        },
      }, el('span', { class: 'check-mark' }, icon(finding.severity === 'blocker' ? 'circle-alert' : 'triangle-alert', { size: 14 })), finding.message)));
  });

  // ---------- выбор ----------

  function pick(id, event) {
    if (event && (event.ctrlKey || event.metaKey || event.shiftKey)) {
      if (state.picked.has(id)) state.picked.delete(id); else state.picked.add(id);
    } else {
      if (selectionRange()) return; // выделение мышью важнее клика
      state.picked = state.picked.size === 1 && state.picked.has(id) ? new Set() : new Set([id]);
    }
    renderText();
  }

  function step(delta) {
    const { segments } = state.data;
    if (!segments.length) return;
    const current = [...state.picked].at(-1);
    const index = segments.findIndex((s) => s.id === current);
    const next = segments[Math.min(segments.length - 1, Math.max(0, index + delta))];
    state.picked = new Set([next.id]);
    renderText();
    textHost.querySelector('.seg.pick')?.scrollIntoView({ block: 'nearest' });
  }

  // ---------- операции ----------

  async function edit(path, payload) {
    try {
      apply(await post(`/api/chapters/${chapterId}/${path}`, payload || {}));
      loadChecks();
      if (path === 'rename' || path === 'undo' || path === 'redo') loadCast();
    } catch (error) {
      say(error.message, 'error');
    }
  }

  function assign(speaker) {
    const range = selectionRange();
    hideSelectionPop();
    if (range) {
      window.getSelection().removeAllRanges();
      return edit('assign-range', { char_start: range.start, char_end: range.end, speaker });
    }
    if (!state.picked.size) return say('Сначала нажмите на фразу или выделите текст — потом выберите роль', 'error');
    return edit('assign', { segment_ids: [...state.picked], speaker });
  }

  function splitAtCaret() {
    const caret = caretOffset();
    if (!caret || !caret.offset) return say('Поставьте курсор внутрь фразы — там, где её разделить', 'error');
    return edit('split', { segment_id: caret.segmentId, offset: caret.offset });
  }

  async function newSpeaker() {
    const name = await ask('Новый персонаж', { placeholder: 'Имя' });
    if (name) assign(name);
  }

  async function rename(segment) {
    const name = segment.speaker ?? segment.name;
    const answer = await askRename(name);
    if (answer) edit('rename', { old: name, new: answer.name, whole_book: answer.wholeBook });
  }

  /** Послушать реплику: из готовой озвучки главы, если она есть. */
  const listen = guard(async (segment) => {
    if (state.data.chapter.audio_path && segment.audio_start_ms !== null && segment.audio_start_ms !== undefined) {
      await player.playChapter(chapterId, { positionMs: segment.audio_start_ms });
      return;
    }
    const speaker = state.data.speakers.find((s) => s.name === segment.speaker);
    const voiceId = speaker?.voice?.voice_id;
    if (!voiceId) return toast('У этой роли нет голоса — выберите его в списке ролей справа', 'error');
    const result = await post(`/api/voices/${voiceId}/preview`, {
      text: segment.text, rate: speaker.voice.rate, pitch: speaker.voice.pitch, volume: speaker.voice.volume,
    });
    const audio = new Audio(result.url);
    await audio.play();
  });

  // Разметка идёт через очередь: окно не висит, её видно в «Задачах» и можно
  // отменить, а закрытие приложения посреди разметки её не теряет.
  markupButton.onclick = async () => {
    markupButton.disabled = true;
    say('ставлю в очередь…', 'busy');
    try {
      const job = await post(`/api/chapters/${chapterId}/markup-job`, {});
      emit('jobs-changed');
      const final = await waitForJob(job.id, (current) => {
        if (current.status === 'pending') say('разметка ждёт своей очереди…', 'busy');
        if (current.status === 'running') say(`Claude размечает главу… ${Math.round((current.progress || 0) * 100)}%`, 'busy');
      }, { alive: () => state.alive });
      if (!state.alive) return;
      if (final.status === 'done') {
        await load();
        emit('library-changed');
        say('размечено', 'ok');
      } else if (final.status === 'cancelled') {
        say('разметка отменена', 'error');
      } else {
        say(final.error || 'разметка не удалась', 'error');
      }
    } catch (error) {
      say(error.message, 'error');
    } finally {
      markupButton.disabled = false;
    }
  };

  voiceButton.onclick = guard(async () => {
    if (!(await ensureVoices(state.data.book.id, { chapterId }))) return;
    await loadCast();
    loadChecks();
    const job = await post(`/api/chapters/${chapterId}/synthesize`, {});
    toast(`Задача добавлена: ${job.title}`, 'ok');
    emit('jobs-changed');
  });

  const listenButton = el('button', { class: 'primary' }, icon('play', { className: 'filled' }), 'Слушать главу');
  listenButton.onclick = guard(() => player.playChapter(chapterId));
  const readLink = el('a', { class: 'button round surface', title: 'Читать вместе со звуком', 'aria-label': 'Читать вместе со звуком' }, icon('book-open'));

  /**
   * Что делать с главой дальше: озвучить; ждать очереди; слушать готовое;
   * доозвучить реплики, изменённые после озвучки.
   */
  let voiceKey = '';
  let hadJob = false;
  function drawVoiceHost() {
    if (!state.data) return;
    const job = synthJobForChapter(chapterId, state.data.book.id);
    // Задача закончилась — перечитать главу: появилась озвучка.
    if (hadJob && !job) load().catch(() => {});
    hadJob = Boolean(job);
    const segments = state.data.segments;
    const stale = segments.filter((s) => !s.audio_path).length;
    const voiced = Boolean(state.data.chapter.audio_path);
    const mode = job ? 'job' : !segments.length ? 'none' : voiced && !stale ? 'listen' : voiced ? 'update' : 'voice';
    const key = job ? `${job.id}:${job.status}:${Math.round((job.progress || 0) * 100)}` : `${mode}:${stale}`;
    if (key === voiceKey) return;
    voiceKey = key;
    readLink.href = `#/chapter/${chapterId}`;
    if (mode === 'job') voiceHost.replaceChildren(busyPill(job));
    else if (mode === 'listen') voiceHost.replaceChildren(readLink, listenButton);
    else if (mode === 'update') {
      voiceButton.replaceChildren(icon('mic'), `Доозвучить изменения · ${stale}`);
      voiceHost.replaceChildren(readLink, voiceButton);
    } else {
      voiceButton.replaceChildren(icon('mic'), 'Озвучить главу');
      voiceButton.disabled = mode === 'none';
      voiceButton.title = mode === 'none' ? 'Сначала разберите главу по ролям' : '';
      voiceHost.replaceChildren(voiceButton);
    }
  }
  const unwatchJobs = watchActiveJobs(drawVoiceHost);

  chapterPick.onchange = () => { location.hash = `#/chapter/${chapterPick.value}/edit`; };
  undoButton.onclick = () => edit('undo');
  redoButton.onclick = () => edit('redo');

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
    if (event.key === 'Escape' && !selPop.hidden) { window.getSelection().removeAllRanges(); hideSelectionPop(); return; }
    if (event.key === 'Escape' && state.picked.size) { state.picked = new Set(); renderText(); }
    if (event.key === 'ArrowDown') { event.preventDefault(); step(1); }
    if (event.key === 'ArrowUp') { event.preventDefault(); step(-1); }
    if (event.key === 'Enter' && !event.target.matches?.('button')) { event.preventDefault(); splitAtCaret(); }
    if (event.key.toLowerCase() === 'm' && state.picked.size > 1) {
      event.preventDefault();
      edit('merge', { segment_ids: [...state.picked] });
    }
  }

  // Выделили текст мышью — над ним панель ролей.
  // И с клавиатуры (Shift+стрелки): показываем, когда выделение перестало меняться.
  let mouseDown = false;
  let settle = null;
  const onMouseDown = () => { mouseDown = true; };
  const onMouseUp = () => { mouseDown = false; setTimeout(() => { if (selectionRange()) showSelectionPop(); }, 0); };
  const onSelection = () => {
    if (!selectionRange()) { hideSelectionPop(); return; }
    clearTimeout(settle);
    settle = setTimeout(() => { if (!mouseDown && selectionRange()) showSelectionPop(); }, 250);
  };
  const onScroll = () => { if (!selPop.hidden) showSelectionPop(); };
  // Щелчок мимо текста и панелей — выбор снимается, панель под фразой закрывается.
  const onOutside = (event) => {
    if (!state.picked.size || !state.data) return;
    const target = event.target;
    if (textHost.contains(target) || selPop.contains(target)) return;
    if (target.closest?.('.studio-side, .editor-toolbar, .menu-pop, dialog')) return;
    state.picked = new Set();
    renderText();
  };
  document.addEventListener('click', onOutside);
  textHost.addEventListener('mousedown', onMouseDown);
  document.addEventListener('mouseup', onMouseUp);
  document.addEventListener('selectionchange', onSelection);
  view.addEventListener('scroll', onScroll, { passive: true });
  document.body.append(selPop);

  document.addEventListener('keydown', onKey);
  load().catch((error) => say(error.message, 'error'));

  return () => {
    state.alive = false;
    unwatchJobs();
    selPop.remove();
    clearTimeout(settle);
    document.removeEventListener('mouseup', onMouseUp);
    document.removeEventListener('click', onOutside);
    document.removeEventListener('selectionchange', onSelection);
    view.removeEventListener('scroll', onScroll);
    document.removeEventListener('keydown', onKey);
  };
}
