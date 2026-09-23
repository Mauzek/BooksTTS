// Тема оформления: как в системе, светлая или тёмная.
//
// Выбор хранится в localStorage, а не в базе: это настройка окна, а не данные
// библиотеки, и применить её нужно до первой отрисовки, без запроса к серверу.

import { emit } from './ui.js';

const KEY = 'booktts-theme';
export const THEMES = [
  ['auto', 'Как в системе', 'monitor'],
  ['light', 'Светлая', 'sun'],
  ['dark', 'Тёмная', 'moon'],
];

export function currentTheme() {
  try {
    const saved = localStorage.getItem(KEY);
    return THEMES.some(([name]) => name === saved) ? saved : 'auto';
  } catch {
    return 'auto';
  }
}

export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // приватный режим: тема продержится до закрытия окна
  }
  emit('theme-changed', theme);
  return theme;
}

export function nextTheme() {
  const names = THEMES.map(([name]) => name);
  const index = names.indexOf(currentTheme());
  return applyTheme(names[(index + 1) % names.length]);
}

export function themeIcon(theme = currentTheme()) {
  return (THEMES.find(([name]) => name === theme) || THEMES[0])[2];
}

export function themeTitle(theme = currentTheme()) {
  return (THEMES.find(([name]) => name === theme) || THEMES[0])[1];
}
