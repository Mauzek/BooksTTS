// Мелкие помощники интерфейса: элементы, диалоги, уведомления, форматирование.

export const $ = (selector, root = document) => root.querySelector(selector);

// Щелчок по затемнению вокруг окна закрывает его, как «Отмена». Нажатие и
// отпускание оба должны быть снаружи: выделение текста в поле, отпущенное за
// краем окна, окно не закроет. Занятое окно (идёт запрос) не закрывается.
let pressedOutside = null;
const outsideDialog = (event) => {
  const dialog = event.target;
  if (!(dialog instanceof HTMLDialogElement) || !dialog.open) return null;
  const r = dialog.getBoundingClientRect();
  const inside = event.clientX >= r.left && event.clientX <= r.right && event.clientY >= r.top && event.clientY <= r.bottom;
  return inside ? null : dialog;
};
document.addEventListener('mousedown', (event) => { pressedOutside = outsideDialog(event); });
document.addEventListener('click', (event) => {
  const dialog = outsideDialog(event);
  if (!dialog || dialog !== pressedOutside || dialog.classList.contains('busy') || dialog.dataset.sticky) return;
  pressedOutside = null;
  const cancel = new Event('cancel', { cancelable: true });
  dialog.dispatchEvent(cancel);
  if (!cancel.defaultPrevented && dialog.open) dialog.close();
});

// Пустые части разметки (null, false) — не текст. Без этого условная часть
// вида «cond ? node : null» печатается на экране словом «null».
for (const method of ['append', 'prepend', 'replaceChildren']) {
  const original = Element.prototype[method];
  Element.prototype[method] = function withoutEmpty(...nodes) {
    return original.apply(this, nodes.filter((node) => node !== null && node !== undefined && node !== false));
  };
}

/** Создать элемент: el('button', {class: 'primary', onclick}, 'Текст'). Текст — всегда textContent. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style' && typeof value === 'object') {
      // Переменные CSS (--i) ставятся только через setProperty.
      for (const [prop, v] of Object.entries(value)) {
        if (prop.startsWith('--')) node.style.setProperty(prop, v);
        else node.style[prop] = v;
      }
    }
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

const TOAST_ICONS = { ok: 'circle-check', error: 'circle-alert', '': 'info' };

/** Убрать уведомление плавно: сначала уезжает, потом исчезает из разметки. */
function dismiss(node) {
  if (!node.isConnected || node.classList.contains('leaving')) return;
  node.classList.add('leaving');
  setTimeout(() => node.remove(), 180);
}

/**
 * Уведомление внутри окна. Короткое — одна строка; с title — карточка с
 * заголовком. action — [подпись, действие]: кнопка «Открыть», «Повторить»…
 */
export function toast(message, kind = '', { title = '', action = null, timeout = 0 } = {}) {
  const host = document.getElementById('toasts');
  const close = el('button', { class: 'ghost icon-button toast-close', title: 'Закрыть', 'aria-label': 'Закрыть' }, icon('x', { size: 14 }));
  const text = kind === 'error' ? humanError(message) : message;
  const node = el('div', { class: `toast ${kind}${title ? ' card' : ''}`, role: kind === 'error' ? 'alert' : 'status' },
    el('span', { class: 'toast-icon' }, icon(TOAST_ICONS[kind] ?? TOAST_ICONS[''])),
    el('div', { class: 'grow toast-body' },
      title ? el('b', { class: 'toast-title' }, title) : null,
      el('span', { class: 'toast-text' }, text),
      action ? el('button', {
        class: 'sm surface toast-action',
        onclick: () => { dismiss(node); action[1](); },
      }, action[0]) : null),
    close);
  close.onclick = () => dismiss(node);
  host.append(node);
  // Больше трёх одновременно — старые уходят: стопка уведомлений закрывает экран.
  while (host.children.length > 3) host.firstElementChild.remove();
  // Под курсором уведомление не исчезает: его как раз читают.
  let left = timeout || (action || title ? 9000 : kind === 'error' ? 8000 : 3500);
  let started = Date.now();
  let timer = setTimeout(() => dismiss(node), left);
  node.addEventListener('mouseenter', () => { clearTimeout(timer); left -= Date.now() - started; });
  node.addEventListener('mouseleave', () => { started = Date.now(); timer = setTimeout(() => dismiss(node), Math.max(1500, left)); });
  return node;
}

/**
 * Ошибка человеческими словами. Сырые «Error code: 403 - {…}», «Failed to
 * fetch» и английские ответы библиотек пользователю ничего не объясняют.
 */
export function humanError(text) {
  const raw = String(text?.message ?? text ?? '').trim();
  if (!raw) return 'что-то пошло не так';
  const code = raw.match(/Error code: (\d{3})/);
  const where = raw.match(/^(абзацы [\d-]+): /);
  const prefix = where ? `${where[1]}: ` : '';
  if (code) {
    const n = Number(code[1]);
    if (n === 401) return `${prefix}ключ не подошёл (401) — проверьте его в «Настройках»`;
    if (n === 403) return `${prefix}сервер разметки отказал в доступе (403) — проверьте адрес и ключ в «Настройках»`;
    if (n === 404) return `${prefix}сервер не знает такую модель или адрес (404) — проверьте «Настройки»`;
    if (n === 429) return `${prefix}слишком много запросов (429) — подождите минуту и повторите`;
    if (n >= 500) return `${prefix}сервер разметки временно недоступен (${n}) — повторите позже`;
    return `${prefix}ошибка сервера разметки (${n})`;
  }
  if (/failed to fetch|networkerror|load failed/i.test(raw)) {
    return 'нет связи с приложением — оно перезапускается, повторите через пару секунд';
  }
  if (/^internal server error$/i.test(raw)) return 'внутренняя ошибка приложения — подробности в журнале';
  if (/api[_ ]?key|authentication/i.test(raw) && /anthropic|claude|x-api-key/i.test(raw)) {
    return 'нет ключа Anthropic — добавьте его в «Настройках», раздел «Ключи»';
  }
  if (/connection error|timed? ?out/i.test(raw)) return 'нет связи с сервером — проверьте интернет и повторите';
  return raw;
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
let spriteReady = false;

/** Встроить набор иконок в страницу. Id с приставкой i-, чтобы не спорить с id элементов. */
export async function loadIcons() {
  try {
    const response = await fetch('/ui/icons.svg');
    const text = await response.text();
    const host = document.createElement('div');
    host.hidden = true;
    host.setAttribute('aria-hidden', 'true');
    host.innerHTML = text.replace(/<symbol id="/g, '<symbol id="i-');
    document.body.prepend(host);
    spriteReady = true;
  } catch {
    // Без встроенного набора иконки берутся из файла — работают, просто медленнее.
  }
}

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
  // Набор встроен в страницу (loadIcons): ссылка на внешний файл заново
  // разрешается у каждой новой иконки, и она на кадр пропадает — «мигает».
  use.setAttribute('href', spriteReady ? `#i-${name}` : `/ui/icons.svg#${name}`);
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
        el('button', { class: 'danger-fill', onclick: () => close(true) }, okText)),
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
      toast(error, 'error');
      return undefined;
    }
  };
}

/**
 * Всплывающее меню у курсора (MouseEvent) или под кнопкой (элемент).
 * Пункт — [подпись, действие, класс, выключен] или '-' для разделителя.
 */
export function popupMenu(anchor, items) {
  document.querySelector('.menu-pop')?.remove();
  const menu = el('div', { class: 'menu-pop', role: 'menu' },
    items.map((item) => (item === '-' ? el('hr') : el('button', {
      class: item[2] || '', role: 'menuitem', disabled: item[3] || false,
      onclick: () => { menu.remove(); item[1](); },
    }, item[0]))));
  document.body.append(menu);
  const { width, height } = menu.getBoundingClientRect();
  const at = anchor instanceof MouseEvent
    ? { x: anchor.clientX, y: anchor.clientY }
    : (() => { const r = anchor.getBoundingClientRect(); return { x: r.left, y: r.bottom + 6 }; })();
  menu.style.left = `${Math.max(8, Math.min(at.x, innerWidth - width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(at.y, innerHeight - height - 8))}px`;
  const close = (event) => {
    if (event.type === 'keydown' && event.key !== 'Escape') return;
    if (event.type === 'mousedown' && menu.contains(event.target)) return;
    menu.remove();
    document.removeEventListener('mousedown', close, true);
    document.removeEventListener('keydown', close, true);
  };
  setTimeout(() => {
    document.addEventListener('mousedown', close, true);
    document.addEventListener('keydown', close, true);
  }, 0);
  menu.querySelector('button:not(:disabled)')?.focus();
  return menu;
}

/**
 * Выпадающий список в стиле приложения вместо системного <select> — один
 * вид по всему приложению. Снаружи ведёт себя как select: свойство value и
 * событие change, поэтому заменяется без переделки логики.
 *
 * options: [{ value, label, hint?, group?, depth?, disabled? }]
 */
export function dropdown({ options = [], value = null, placeholder = 'Выберите…', label = '', className = '', onChange = null } = {}) {
  let items = options;
  let current = value;
  const text = el('span', { class: 'dropdown-value' });
  const button = el('button', {
    type: 'button', class: `dropdown ${className}`.trim(), 'aria-haspopup': 'listbox', 'aria-expanded': 'false',
    'aria-label': label || null,
  }, text, icon('chevron-down', { size: 16, className: 'dropdown-chevron' }));

  const same = (a, b) => String(a ?? '') === String(b ?? '');
  const draw = () => {
    const chosen = items.find((item) => same(item.value, current));
    text.textContent = chosen ? chosen.label : placeholder;
    button.classList.toggle('empty', !chosen || chosen.value === '' || chosen.value === null);
  };

  let pop = null;
  function close() {
    pop?.remove();
    pop = null;
    button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('mousedown', outside, true);
    window.removeEventListener('resize', close);
  }
  function outside(event) {
    if (!pop?.contains(event.target) && !button.contains(event.target)) close();
  }
  function choose(item) {
    close();
    button.focus();
    if (same(item.value, current)) return;
    current = item.value;
    draw();
    button.dispatchEvent(new Event('change'));
    onChange?.(item.value);
  }
  function open() {
    if (pop) { close(); return; }
    const rows = [];
    let group = null;
    for (const item of items) {
      if (item.group && item.group !== group) {
        group = item.group;
        rows.push(el('div', { class: 'dropdown-group', role: 'presentation' }, group));
      }
      const selected = same(item.value, current);
      rows.push(el('button', {
        type: 'button', class: `dropdown-option${selected ? ' selected' : ''}`, role: 'option',
        'aria-selected': String(selected), disabled: item.disabled || false,
        style: item.depth ? { paddingLeft: `${12 + item.depth * 16}px` } : null,
        onclick: () => choose(item),
      }, el('span', { class: 'grow' }, item.label, item.hint ? el('span', { class: 'hint' }, item.hint) : null),
      selected ? icon('check', { size: 16 }) : null));
    }
    pop = el('div', { class: 'menu-pop dropdown-pop', role: 'listbox' }, rows);
    pop.addEventListener('keydown', (event) => {
      const options = [...pop.querySelectorAll('.dropdown-option:not(:disabled)')];
      const at = options.indexOf(document.activeElement);
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const next = options[Math.max(0, Math.min(options.length - 1, at + (event.key === 'ArrowDown' ? 1 : -1)))];
        next?.focus();
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault();
        (event.key === 'Home' ? options[0] : options.at(-1))?.focus();
      } else if (event.key === 'Escape' || event.key === 'Tab') {
        event.preventDefault();
        close();
        button.focus();
      } else if (event.key.length === 1 && /\S/.test(event.key)) {
        // Первая буква — к ближайшему пункту на неё, как в системном списке.
        const letter = event.key.toLowerCase();
        const match = [...options.slice(at + 1), ...options.slice(0, at + 1)]
          .find((option) => option.textContent.trim().toLowerCase().startsWith(letter));
        match?.focus();
      }
    });
    document.body.append(pop);
    const rect = button.getBoundingClientRect();
    pop.style.minWidth = `${Math.max(rect.width, 180)}px`;
    const height = Math.min(pop.scrollHeight, innerHeight * 0.5);
    pop.style.maxHeight = `${height}px`;
    const below = innerHeight - rect.bottom - 8;
    const top = below >= height + 6 || below > rect.top ? rect.bottom + 6 : rect.top - height - 6;
    pop.style.top = `${Math.max(8, top)}px`;
    pop.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - pop.offsetWidth - 8))}px`;
    button.setAttribute('aria-expanded', 'true');
    const selected = pop.querySelector('.dropdown-option.selected') || pop.querySelector('.dropdown-option:not(:disabled)');
    selected?.scrollIntoView({ block: 'center' });
    selected?.focus();
    setTimeout(() => {
      document.addEventListener('mousedown', outside, true);
      window.addEventListener('resize', close);
    }, 0);
  }

  button.onclick = open;
  button.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); open(); }
  });
  Object.defineProperty(button, 'value', {
    get: () => (current === null || current === undefined ? '' : String(current)),
    set: (next) => { current = next; draw(); },
  });
  button.setOptions = (next, nextValue = current) => { items = next; current = nextValue; draw(); };
  button.close = close;
  draw();
  return button;
}
