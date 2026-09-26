// Обновления глазами человека: окно «Доступна новая версия» с описанием
// изменений и окно «Что нового» со слайдами — после обновления.
//
// Описание версии — whatsnew/v<версия>.md: тот же файл CI кладёт в описание
// релиза на GitHub, а оттуда его получает проверка обновлений.

import { el, icon } from './ui.js';

const SEEN_KEY = 'booktts-seen-version';
const BASE = '/ui/whatsnew';

/** Слайды «Что нового» по версиям. Картинки — снимки нового вида. */
export const RELEASES = [
  {
    version: '0.4.0',
    title: 'Что нового в BookTTS 0.4',
    slides: [
      { title: 'Новый вид', image: '0.4/home.webp',
        text: 'Светлая и тёмная тема, новые шрифты и главная с большим плеером: обложка, реплика и кто говорит — цветом роли. Кнопки «Назад» и «Вперёд» — как в браузере.' },
      { title: 'Студия по шагам', image: '0.4/steps.webp',
        text: 'У каждой книги пять шагов: текст, роли, голоса, озвучка, готово. Видно, что уже сделано и что дальше, — ничего не запускается вслепую.' },
      { title: 'Разметка стала проще', image: '0.4/markup.webp',
        text: 'Выделите любой кусок текста — над ним появятся роли. Голос роли выбирается тут же, а её цвет закреплён и меняется по желанию.' },
      { title: 'Карточка книги', image: '0.4/book.webp',
        text: 'Жанр, настроение, метки и описание — вручную или с помощью ИИ по названию книги. Обложка рисуется по жанру и настроению, можно загрузить и свою.' },
      { title: 'Библиотека и голоса', image: '0.4/library.webp',
        text: 'Папки как в облачном диске — книги перетаскиваются мышью. В каталоге голосов готовые образцы: ▶ играет сразу.' },
    ],
  },
];

const parse = (version) => String(version || '0').split('.').map((n) => parseInt(n, 10) || 0);
function newer(a, b) {
  const [x, y] = [parse(a), parse(b)];
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0);
  }
  return false;
}

/** Описание версии: markdown из whatsnew/v<версия>.md. */
export async function releaseNotes(version) {
  try {
    const response = await fetch(`${BASE}/v${version}.md`);
    return response.ok ? await response.text() : '';
  } catch {
    return '';
  }
}

/**
 * Простой markdown в узлы: заголовки «## », списки «- », абзацы, **жирный**.
 * Строится из текстовых узлов — HTML из описания не исполнится.
 */
export function renderNotes(markdown) {
  const host = el('div', { class: 'notes' });
  let list = null;
  const inline = (text) => text.split(/(\*\*[^*]+\*\*)/).filter(Boolean)
    .map((part) => (part.startsWith('**') ? el('b', {}, part.slice(2, -2)) : part));
  for (const raw of String(markdown || '').split('\n')) {
    const line = raw.trim();
    if (!line) { list = null; continue; }
    if (/^#{1,4} /.test(line)) {
      list = null;
      host.append(el('h4', {}, ...inline(line.replace(/^#+ /, ''))));
    } else if (/^[-*] /.test(line)) {
      if (!list) { list = el('ul'); host.append(list); }
      list.append(el('li', {}, ...inline(line.slice(2))));
    } else {
      list = null;
      host.append(el('p', {}, ...inline(line)));
    }
  }
  return host;
}

function modal(className, build) {
  const dialog = el('dialog', { class: className });
  const close = () => { dialog.close(); dialog.remove(); };
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
  build(dialog, close);
  document.body.append(dialog);
  dialog.showModal();
  return close;
}

/** Окно «Что нового»: слайды с картинками, листаются стрелками и точками. */
export function showWhatsNew(version) {
  const release = RELEASES.find((item) => item.version === version) || RELEASES[0];
  if (!release) return;
  let index = 0;
  modal('whatsnew-dialog', (dialog, close) => {
    const image = el('img', { class: 'whatsnew-image', alt: '', draggable: 'false' });
    const title = el('h3', {});
    const text = el('p', { class: 'whatsnew-text' });
    const dots = el('div', { class: 'whatsnew-dots', role: 'tablist' });
    const prev = el('button', { class: 'ghost lg', type: 'button' }, icon('arrow-left'), 'Назад');
    const next = el('button', { class: 'primary lg', type: 'button' });
    const draw = () => {
      const slide = release.slides[index];
      image.src = `${BASE}/${slide.image}`;
      image.alt = slide.title;
      title.textContent = slide.title;
      text.textContent = slide.text;
      prev.disabled = index === 0;
      const last = index === release.slides.length - 1;
      next.replaceChildren(last ? 'Понятно' : 'Дальше', last ? '' : icon('arrow-right'));
      dots.replaceChildren(...release.slides.map((item, n) => el('button', {
        type: 'button', class: `whatsnew-dot${n === index ? ' current' : ''}`, role: 'tab',
        'aria-selected': String(n === index), 'aria-label': item.title, onclick: () => { index = n; draw(); },
      })));
    };
    prev.onclick = () => { if (index > 0) { index -= 1; draw(); } };
    next.onclick = () => { if (index < release.slides.length - 1) { index += 1; draw(); } else close(); };
    dialog.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowRight') { event.preventDefault(); next.click(); }
      if (event.key === 'ArrowLeft') { event.preventDefault(); prev.click(); }
    });
    dialog.append(
      el('div', { class: 'whatsnew-head' },
        el('span', { class: 'hint' }, release.title),
        el('button', { class: 'round', type: 'button', 'aria-label': 'Закрыть', onclick: close }, icon('x'))),
      el('div', { class: 'whatsnew-frame' }, image),
      title, text,
      el('div', { class: 'whatsnew-foot' }, prev, dots, next));
    draw();
  });
}

/**
 * После обновления — показать «Что нового» один раз. На свежей установке
 * (библиотека пуста, прежней версии не было) показывать не к чему.
 */
export function maybeShowWhatsNew(current, { hasBooks }) {
  let seen = null;
  try {
    seen = localStorage.getItem(SEEN_KEY);
    localStorage.setItem(SEEN_KEY, current);
  } catch {
    return;
  }
  if (!seen && !hasBooks) return;
  if (seen && !newer(current, seen)) return;
  const release = RELEASES.find((item) => item.version === current);
  if (release) showWhatsNew(release.version);
}

/**
 * Окно «Доступна новая версия»: что изменилось и кнопка обновления.
 * install(onProgress) — скачать и поставить; оболочка потом перезапустит приложение.
 */
export function updateDialog(update, { install }) {
  modal('update-dialog', (dialog, close) => {
    const notes = el('div', { class: 'update-notes' }, update.notes
      ? renderNotes(update.notes)
      : el('p', { class: 'hint' }, 'Описание изменений не пришло — оно есть на странице релиза на GitHub.'));
    const status = el('p', { class: 'hint update-status' });
    const run = el('button', { class: 'primary lg' }, icon('download'), 'Обновить и перезапустить');
    run.onclick = async () => {
      run.disabled = true;
      dialog.classList.add('busy');
      try {
        await install((text) => { run.replaceChildren(el('span', { class: 'spinner', 'aria-hidden': 'true' }), text); });
      } catch (error) {
        status.textContent = String(error?.message || error);
        run.disabled = false;
        dialog.classList.remove('busy');
        run.replaceChildren(icon('download'), 'Попробовать ещё раз');
      }
    };
    dialog.append(
      el('div', { class: 'dialog-head' },
        el('span', { class: 'update-badge', 'aria-hidden': 'true' }, icon('download', { className: 'big' })),
        el('div', { class: 'grow' },
          el('h3', {}, `Доступна версия ${update.version}`),
          el('span', { class: 'hint' }, `У вас ${update.current}. Обновление скачается и поставится само, библиотека не пострадает.`)),
        el('button', { class: 'round', 'aria-label': 'Закрыть', onclick: close }, icon('x'))),
      notes, status,
      el('div', { class: 'buttons' },
        el('button', { class: 'ghost lg', onclick: close }, 'Позже'),
        run));
  });
}
