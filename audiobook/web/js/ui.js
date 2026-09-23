// Мелкие помощники интерфейса: элементы, диалоги, уведомления, форматирование.

export const $ = (selector, root = document) => root.querySelector(selector);

/** Создать элемент: el('button', {class: 'primary', onclick}, 'Текст'). Текст — всегда textContent. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/**
 * Текст с маркерами подсветки \x02…\x03 от бэкенда -> узлы с <mark>.
 * Строится из текстовых узлов, поэтому HTML в названиях книг не исполнится.
 */
export function marked(text) {
  const fragment = document.createDocumentFragment();
  const parts = String(text || '').split(/(\x02[^\x03]*\x03)/);
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith('\x02')) fragment.append(el('mark', {}, part.slice(1, -1)));
    else fragment.append(document.createTextNode(part));
  }
  return fragment;
}

export const plain = (text) => String(text || '').replace(/[\x02\x03]/g, '');

const TOAST_ICONS = { ok: 'circle-check', error: 'circle-alert', '': 'refresh-cw' };

export function toast(message, kind = '') {
  const host = document.getElementById('toasts');
  const close = el('button', { class: 'ghost icon-button toast-close', title: 'Закрыть' }, icon('x', { size: 14 }));
  const node = el('div', { class: `toast ${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    icon(TOAST_ICONS[kind] ?? TOAST_ICONS[''], { className: 'toast-icon' }),
    el('span', { class: 'grow' }, message),
    close);
  close.onclick = () => node.remove();
  host.append(node);
  // Больше трёх одновременно — старые уходят: стопка уведомлений закрывает экран.
  while (host.children.length > 3) host.firstElementChild.remove();
  setTimeout(() => node.remove(), kind === 'error' ? 8000 : 3500);
}

function modal(build) {
  return new Promise((resolve) => {
    const dialog = el('dialog');
    const close = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(null); });
    build(dialog, close);
    document.body.append(dialog);
    dialog.showModal();
    dialog.querySelector('input, select, button.primary')?.focus();
  });
}

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * Иконка из набора Lucide. Спрайт лежит в проекте, поэтому работает без сети.
 * Цвет наследуется от текста, размер задаётся классом.
 */
export function icon(name, { size = 0, className = '' } = {}) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', `icon ${className}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  if (size) {
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
  }
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `/ui/icons.svg#${name}`);
  svg.append(use);
  return svg;
}

/** Событие уровня приложения: экраны и дерево общаются через window. */
export function emit(name, detail = null) {
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

/** Спросить строку. null — отмена; пустая строка — только при allowEmpty. */
export function ask(title, { value = '', placeholder = '', label = '', okText = 'Готово', allowEmpty = false } = {}) {
  return modal((dialog, close) => {
    const input = el('input', { value, placeholder });
    const form = el('form', { method: 'dialog' },
      el('h3', {}, title),
      el('label', { class: 'field' }, label ? el('span', { class: 'hint' }, label) : null, input),
      el('div', { class: 'buttons' },
        el('button', { type: 'button', onclick: () => close(null) }, 'Отмена'),
        el('button', { class: 'primary', type: 'submit' }, okText)),
    );
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const text = input.value.trim();
      close(allowEmpty ? text : text || null);
    });
    dialog.append(form);
    setTimeout(() => input.select(), 0);
  });
}

/** Подтверждение опасного действия. */
export function confirmAction(title, message, okText = 'Удалить') {
  return modal((dialog, close) => {
    dialog.append(
      el('h3', {}, title),
      el('p', { class: 'muted' }, message),
      el('div', { class: 'buttons' },
        el('button', { onclick: () => close(false) }, 'Отмена'),
        el('button', { class: 'primary danger', onclick: () => close(true) }, okText)),
    );
  });
}

export function formatDuration(ms) {
  if (!ms) return '0:00';
  const total = Math.round(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
}

export function formatHours(ms) {
  const hours = (ms || 0) / 3_600_000;
  return hours >= 10 ? `${Math.round(hours)} ч` : `${hours.toFixed(1).replace('.', ',')} ч`;
}

/** Крупная величина в понятных единицах: «3,5 ч», «12 мин», «20 с». */
export function formatSpan(ms) {
  const value = ms || 0;
  if (value >= 3_600_000) return formatHours(value);
  if (value >= 60_000) return `${Math.round(value / 60_000)} мин`;
  return `${Math.round(value / 1000)} с`;
}

/** 1 глава, 2 главы, 5 глав. */
export function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return `${n} ${one}`;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${n} ${few}`;
  return `${n} ${many}`;
}

/** Обработчик ошибок для async-действий интерфейса. */
export function guard(action) {
  return async (...args) => {
    try {
      return await action(...args);
    } catch (error) {
      console.error(error);
      toast(error.message || String(error), 'error');
      return undefined;
    }
  };
}
