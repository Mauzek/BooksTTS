// Сведения о книге: жанры, настроение, метки, описание — и подбор их через Claude.
//
// Жанр рисует на обложке фигуру, настроение — цвета, поэтому в диалоге
// обложка перерисовывается сразу, пока человек выбирает.

import { get, patch, post } from './api.js';
import { GENRE_SHAPES, MOOD_PALETTES, cover } from './covers.js';
import { el, emit, guard, humanError, icon, toast } from './ui.js';

const MAX_GENRES = 3;
const MAX_MOODS = 2;
const MAX_TAGS = 12;

let vocabulary = null;

async function loadVocabulary() {
  if (!vocabulary) {
    vocabulary = await get('/api/book-vocabulary').catch(() => ({
      genres: Object.keys(GENRE_SHAPES), moods: Object.keys(MOOD_PALETTES),
    }));
  }
  return vocabulary;
}

function openDialog(className, build) {
  return new Promise((resolve) => {
    const dialog = el('dialog', { class: className });
    const close = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(null); });
    build(dialog, close);
    document.body.append(dialog);
    dialog.showModal();
  });
}

/** Чипы-переключатели с пределом: сверх предела старый выбор уступает новому. */
function chipPicker(options, selected, limit, { dot = null, onChange } = {}) {
  const chosen = selected.filter((name) => options.includes(name)).slice(0, limit);
  const host = el('div', { class: 'pick-chips' });
  const draw = () => host.replaceChildren(...options.map((name) => el('button', {
    type: 'button', class: 'pick-chip', 'aria-pressed': String(chosen.includes(name)),
    onclick: () => {
      const at = chosen.indexOf(name);
      if (at >= 0) chosen.splice(at, 1);
      else {
        chosen.push(name);
        if (chosen.length > limit) chosen.shift();
      }
      draw();
      onChange?.();
    },
  }, dot ? dot(name) : null, name)));
  draw();
  return {
    host,
    value: () => [...chosen],
    set(values) { chosen.splice(0, chosen.length, ...values.filter((v) => options.includes(v)).slice(0, limit)); draw(); },
  };
}

/** Свободные метки: ввод + Enter, крестик у каждой. */
function tagEditor(initial) {
  const tags = [...initial];
  const list = el('div', { class: 'tag-list' });
  const input = el('input', { placeholder: 'метка и Enter', 'aria-label': 'Новая метка', maxlength: '40' });
  const add = () => {
    const value = input.value.trim().replace(/\s+/g, ' ');
    if (value && !tags.some((t) => t.toLowerCase() === value.toLowerCase()) && tags.length < MAX_TAGS) tags.push(value);
    input.value = '';
    draw();
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); add(); }
    if (event.key === 'Backspace' && !input.value && tags.length) { tags.pop(); draw(); }
  });
  input.addEventListener('blur', () => { if (input.value.trim()) add(); });
  function draw() {
    list.replaceChildren(...tags.map((tag, index) => el('span', { class: 'tag' }, tag,
      el('button', {
        type: 'button', class: 'tag-remove', 'aria-label': `Убрать метку ${tag}`,
        onclick: () => { tags.splice(index, 1); draw(); },
      }, icon('x', { size: 12 })))), input);
  }
  draw();
  return {
    host: el('div', { class: 'tag-editor', onclick: (event) => { if (event.target === event.currentTarget) input.focus(); } }, list),
    value: () => { if (input.value.trim()) add(); return [...tags]; },
    set(values) { tags.splice(0, tags.length, ...values.slice(0, MAX_TAGS)); draw(); },
  };
}

const moodDot = (name) => el('span', { class: 'mood-dot', style: { background: MOOD_PALETTES[name]?.[0] || 'var(--track)' }, 'aria-hidden': 'true' });

/**
 * Спросить у Claude жанры, настроение, метки и описание. Сначала — название,
 * автор, год и страна: по ним нейросеть узнаёт книгу. Возвращает
 * предложение или null, если человек передумал.
 */
export function askAI(book, current = {}) {
  return openDialog('ai-dialog', (dialog, close) => {
    const title = el('input', { value: current.title ?? book.title ?? '', required: true, 'aria-label': 'Название произведения' });
    const author = el('input', { value: current.author ?? book.author ?? '', placeholder: 'необязательно', 'aria-label': 'Автор' });
    const year = el('input', { value: current.year ?? book.year ?? '', inputmode: 'numeric', placeholder: 'гггг', 'aria-label': 'Год' });
    const country = el('input', { value: current.country ?? book.country ?? '', placeholder: 'необязательно', 'aria-label': 'Страна' });
    const error = el('p', { class: 'form-error', role: 'alert', hidden: true });
    const run = el('button', { class: 'primary lg', type: 'submit' }, icon('sparkles'), 'Подобрать');

    const form = el('form', { class: 'ai-form' },
      el('div', { class: 'dialog-head' },
        el('span', { class: 'ai-badge', 'aria-hidden': 'true' }, icon('sparkles', { className: 'big' })),
        el('div', { class: 'grow' },
          el('h3', {}, 'Заполнить с помощью ИИ'),
          el('span', { class: 'hint' }, 'Нейросеть узнает книгу по названию и предложит жанр, настроение, метки и описание без спойлеров')),
        el('button', { type: 'button', class: 'round', 'aria-label': 'Закрыть', onclick: () => close(null) }, icon('x'))),
      el('div', { class: 'callout caution' },
        icon('triangle-alert'),
        el('div', {},
          el('b', {}, 'Чётко укажите название произведения.'),
          el('span', {}, ' По нему нейросеть узнаёт книгу — лучше так, как оно издано: «Мастер и Маргарита», а не «мастер и марг». ',
            'Автор, год и страна необязательны, но с ними анализ точнее — особенно у малоизвестных книг и книг с одинаковыми названиями.'))),
      el('label', { class: 'field' }, el('span', { class: 'field-label' }, 'Название ', el('span', { class: 'req' }, 'обязательно')), title),
      el('div', { class: 'field-grid' },
        el('label', { class: 'field' }, el('span', { class: 'field-label' }, 'Автор'), author),
        el('label', { class: 'field small' }, el('span', { class: 'field-label' }, 'Год'), year),
        el('label', { class: 'field' }, el('span', { class: 'field-label' }, 'Страна'), country)),
      el('p', { class: 'hint' }, 'Вместе с названием в Claude уйдёт начало книги — около 2 500 знаков. Запрос идёт по вашему ключу и стоит меньше цента. Ничего не сохранится, пока вы не нажмёте «Сохранить».'),
      error,
      el('div', { class: 'buttons' },
        el('button', { type: 'button', class: 'ghost lg', onclick: () => close(null) }, 'Отмена'),
        run));

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const name = title.value.trim();
      if (!name) { title.focus(); return; }
      const yearValue = year.value.trim();
      if (yearValue && !/^-?\d{1,4}$/.test(yearValue)) {
        error.textContent = 'Год — это число, например 2012.';
        error.hidden = false;
        year.focus();
        return;
      }
      error.hidden = true;
      run.disabled = true;
      run.replaceChildren(el('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Claude думает…');
      dialog.classList.add('busy');
      try {
        const suggestion = await post(`/api/books/${book.id}/describe`, {
          title: name, author: author.value.trim(), year: yearValue ? Number(yearValue) : null, country: country.value.trim(),
        });
        close({ ...suggestion, asked: { title: name, author: author.value.trim(), year: yearValue ? Number(yearValue) : null, country: country.value.trim() } });
      } catch (failure) {
        error.textContent = humanError(failure);
        error.hidden = false;
        run.disabled = false;
        run.replaceChildren(icon('sparkles'), 'Попробовать ещё раз');
        dialog.classList.remove('busy');
      }
    });
    dialog.append(form);
    setTimeout(() => { title.focus(); title.select(); }, 0);
  });
}

/**
 * Карточка книги: название, автор, год, страна, жанры, настроение, метки,
 * описание. suggestion — ответ ИИ: его значения подставляются в поля, а
 * человек проверяет и сохраняет. Возвращает true, если сохранили.
 */
export async function editBookInfo(book, { suggestion = null } = {}) {
  const vocab = await loadVocabulary();
  return openDialog('info-dialog', (dialog, close) => {
    const title = el('input', { value: book.title || '', 'aria-label': 'Название' });
    const author = el('input', { value: book.author || '', placeholder: 'не указан', 'aria-label': 'Автор' });
    const year = el('input', { value: book.year ?? '', inputmode: 'numeric', placeholder: '—', 'aria-label': 'Год' });
    const country = el('input', { value: book.country || '', placeholder: '—', 'aria-label': 'Страна' });
    const description = el('textarea', { rows: '4', placeholder: 'О чём книга — пара предложений без спойлеров', 'aria-label': 'Описание' }, book.description || '');
    const preview = el('div', { class: 'info-cover' });
    const drawPreview = () => preview.replaceChildren(cover({
      id: book.id, title: title.value || book.title, author: author.value,
      genres: genres.value(), moods: moods.value(), cover_path: null,
    }, { remember: false }));
    const genres = chipPicker(vocab.genres, book.genres || [], MAX_GENRES, { onChange: () => drawPreview() });
    const moods = chipPicker(vocab.moods, book.moods || [], MAX_MOODS, { dot: moodDot, onChange: () => drawPreview() });
    // Жанры, которых больше нет в списке (узкие вроде «Исекай»), становятся метками.
    const legacy = (book.genres || []).filter((name) => !vocab.genres.includes(name)).map((name) => name.toLowerCase());
    const tags = tagEditor([...new Set([...(book.tags || []), ...legacy])]);
    let fromAI = false;
    const banner = el('div', { hidden: true });
    const error = el('p', { class: 'form-error', role: 'alert', hidden: true });
    title.addEventListener('input', drawPreview);
    author.addEventListener('input', drawPreview);

    /** Подставить предложение ИИ: пустое в ответе не затирает заполненное. */
    function apply(result) {
      fromAI = true;
      const mark = (input, value) => {
        if (value === null || value === undefined || value === '') return;
        if (String(input.value) !== String(value)) input.classList.add('suggested');
        input.value = value;
      };
      mark(title, result.title);
      mark(author, result.author);
      mark(year, result.year);
      mark(country, result.country);
      mark(description, result.description);
      if (result.genres?.length) genres.set(result.genres);
      if (result.moods?.length) moods.set(result.moods);
      if (result.tags?.length) tags.set([...new Set([...tags.value(), ...result.tags])]);
      banner.className = `callout ${result.recognized ? 'ok' : 'caution'}`;
      banner.replaceChildren(icon(result.recognized ? 'sparkles' : 'triangle-alert'),
        el('div', {},
          el('b', {}, result.recognized ? 'Claude узнал книгу.' : 'Claude не узнал книгу.'),
          el('span', {}, result.recognized
            ? ' Предложенное подсвечено — проверьте и сохраните.'
            : ' Жанры, настроение и описание подобраны по началу текста — проверьте их. Уточните название или автора и попробуйте ещё раз.'),
          result.note ? el('span', { class: 'hint note' }, result.note) : null));
      banner.hidden = false;
      drawPreview();
    }

    const aiButton = el('button', { type: 'button', class: 'surface ai-fill' }, icon('sparkles'),
      'Подобрать с ИИ');
    aiButton.title = 'Жанр, настроение, метки и описание — по названию книги';
    aiButton.onclick = guard(async () => {
      const result = await askAI(book, {
        title: title.value.trim(), author: author.value.trim(), year: year.value.trim(), country: country.value.trim(),
      });
      if (result) apply(result);
    });

    const save = el('button', { class: 'primary lg', type: 'submit' }, 'Сохранить');
    const form = el('form', { class: 'info-form' },
      el('div', { class: 'dialog-head' },
        el('div', { class: 'grow' }, el('h3', {}, 'О книге'),
          el('span', { class: 'hint' }, 'Жанр рисует на обложке фигуру, настроение — цвета')),
        el('button', { type: 'button', class: 'round', 'aria-label': 'Закрыть', onclick: () => close(false) }, icon('x'))),
      banner,
      el('div', { class: 'info-layout' },
        el('div', { class: 'info-side' }, preview,
          book.cover_path ? el('p', { class: 'hint' }, 'Сейчас у книги своя картинка. Нарисованная обложка появится, если убрать картинку.') : null,
          aiButton),
        el('div', { class: 'info-fields' },
          el('div', { class: 'field-grid' },
            el('label', { class: 'field wide' }, el('span', { class: 'field-label' }, 'Название'), title),
            el('label', { class: 'field wide' }, el('span', { class: 'field-label' }, 'Автор'), author),
            el('label', { class: 'field small' }, el('span', { class: 'field-label' }, 'Год'), year),
            el('label', { class: 'field' }, el('span', { class: 'field-label' }, 'Страна'), country)),
          el('div', { class: 'field' }, el('span', { class: 'field-label' }, `Жанр · до ${MAX_GENRES}`), genres.host),
          el('div', { class: 'field' }, el('span', { class: 'field-label' }, `Настроение · до ${MAX_MOODS}`), moods.host),
          el('div', { class: 'field' }, el('span', { class: 'field-label' }, 'Метки'), tags.host),
          el('label', { class: 'field' }, el('span', { class: 'field-label' }, 'Описание'), description))),
      error,
      el('div', { class: 'buttons' },
        el('button', { type: 'button', class: 'ghost lg', onclick: () => close(false) }, 'Отмена'),
        save));

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const yearValue = year.value.trim();
      if (yearValue && !/^-?\d{1,4}$/.test(yearValue)) {
        error.textContent = 'Год — это число, например 2012.';
        error.hidden = false;
        year.focus();
        return;
      }
      if (!title.value.trim()) {
        error.textContent = 'У книги должно быть название.';
        error.hidden = false;
        title.focus();
        return;
      }
      save.disabled = true;
      try {
        await patch(`/api/books/${book.id}`, {
          title: title.value.trim(), author: author.value.trim(),
          year: yearValue ? Number(yearValue) : null, country: country.value.trim(),
          description: description.value.trim(),
          genres: genres.value(), moods: moods.value(), tags: tags.value(),
          // Отметка «подобрано ИИ» — только если сохраняют подобранное.
          ...(fromAI ? { info_source: 'ai' } : {}),
        });
        toast('Сведения о книге сохранены', 'ok');
        emit('library-changed');
        close(true);
      } catch (failure) {
        error.textContent = humanError(failure);
        error.hidden = false;
        save.disabled = false;
      }
    });
    dialog.append(form);
    drawPreview();
    if (suggestion) apply(suggestion);
  });
}

/** Подбор с ИИ одной кнопкой: спросить, показать, сохранить. */
export async function suggestAndEdit(book) {
  const result = await askAI(book);
  if (!result) return false;
  return editBookInfo(book, { suggestion: result });
}
