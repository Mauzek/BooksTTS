// Настройки: ключи API в системном хранилище и параметры движков.

import { del, get, post, put } from '../api.js';
import { THEMES, applyTheme, currentTheme } from '../theme.js';
import { confirmAction, el, guard, toast } from '../ui.js';

export function render(view) {
  let alive = true;
  const page = el('div', { class: 'page' }, el('h1', {}, 'Настройки'));
  view.replaceChildren(page);

  const tokensHost = el('div', { class: 'list' });
  const settingsHost = el('div', { class: 'list' });

  function themeRow() {
    const select = el('select', { class: 'grow' }, THEMES.map(([name, label]) =>
      el('option', { value: name, selected: name === currentTheme() }, label)));
    select.onchange = () => applyTheme(select.value);
    return el('div', { class: 'list-row settings-row' },
      el('div', { class: 'title' },
        el('div', {}, 'Тема оформления'),
        el('div', { class: 'sub' }, 'Светлая, тёмная или как в системе.')),
      el('div', { class: 'row grow' }, select));
  }

  const draw = () => page.replaceChildren(
    el('h1', {}, 'Настройки'),
    el('h2', {}, 'Внешний вид'),
    el('div', { class: 'list' }, themeRow()),
    el('h2', {}, 'Ключи API'),
    el('p', { class: 'hint' },
      'Ключи хранятся в системном хранилище паролей Windows, не в базе и не в открытом файле. ',
      'Приложение показывает только последние символы.'),
    tokensHost,
    el('h2', {}, 'Движки синтеза'),
    settingsHost,
  );

  const loadTokens = guard(async () => {
    const { tokens } = await get('/api/tokens');
    if (!alive) return;
    tokensHost.replaceChildren(...tokens.map(tokenRow));
  });

  function tokenRow(token) {
    const input = el('input', { type: 'password', placeholder: 'Вставьте ключ', class: 'grow' });
    const save = el('button', { class: 'primary' }, 'Сохранить');
    save.onclick = guard(async () => {
      const key = input.value.trim();
      if (!key) return toast('Пустой ключ', 'error');
      await put(`/api/tokens/${token.service}`, { key });
      input.value = '';
      toast(`Ключ ${token.title} сохранён`, 'ok');
      loadTokens();
    });

    const status = token.present
      ? el('span', { class: 'hint' }, `${token.masked} · ${token.source === 'keyring' ? 'в хранилище' : 'из окружения'}`)
      : el('span', { class: 'hint warn' }, 'не задан');

    const actions = el('div', { class: 'row' });
    if (token.can_import_env) {
      actions.append(el('button', {
        title: `Перенести значение ${token.env} в системное хранилище`,
        onclick: guard(async () => {
          await post(`/api/tokens/${token.service}/from-env`);
          toast('Ключ перенесён в хранилище', 'ok');
          loadTokens();
        }),
      }, 'Перенести из .env'));
    }
    if (token.present && token.source === 'keyring') {
      actions.append(el('button', {
        class: 'danger',
        onclick: guard(async () => {
          if (!await confirmAction(`Удалить ключ ${token.title}?`, 'Его можно будет ввести заново.')) return;
          await del(`/api/tokens/${token.service}`);
          toast('Ключ удалён', 'ok');
          loadTokens();
        }),
      }, 'Удалить'));
    }

    return el('div', { class: 'list-row settings-row' },
      el('div', { class: 'title' },
        el('div', {}, token.title),
        el('div', { class: 'sub' }, token.hint, ' · переменная ', token.env)),
      el('div', { class: 'row grow' }, input, save, actions),
      status);
  }

  const loadSettings = guard(async () => {
    const data = await get('/api/settings');
    if (!alive) return;
    const rows = Object.keys(data.defaults)
      .filter((key) => key.startsWith('engine.') || key === 'preview.text')
      .map((key) => settingRow(key, data));
    settingsHost.replaceChildren(...rows);
  });

  const LABELS = {
    'engine.silero.model': ['Модель Silero', 'Новее звучит чище; смена модели заставит переозвучить реплики.'],
    'engine.silero.device': ['Устройство Silero', 'cuda — если есть подходящая видеокарта.'],
    'engine.elevenlabs.model': ['Модель ElevenLabs', 'Например, eleven_multilingual_v2.'],
    'engine.qwen.url': ['Адрес Qwen3-TTS', 'Локальный сервер Gradio, который вы запускаете сами.'],
    'preview.text': ['Фраза для прослушивания', 'Ей озвучиваются образцы голосов.'],
  };

  function settingRow(key, data) {
    const [label, hint] = LABELS[key] || [key, ''];
    const choices = data.choices[key];
    const value = data.settings[key];
    const input = choices
      ? el('select', { class: 'grow' }, choices.map((choice) =>
        el('option', { value: choice, selected: choice === value }, choice)))
      : el('input', { class: 'grow', value: value ?? '' });
    const save = guard(async () => {
      await put('/api/settings', { values: { [key]: input.value } });
      toast('Сохранено', 'ok');
      loadSettings();
    });
    input.onchange = save;
    return el('div', { class: 'list-row settings-row' },
      el('div', { class: 'title' }, el('div', {}, label), el('div', { class: 'sub' }, hint)),
      el('div', { class: 'row grow' }, input));
  }

  draw();
  loadTokens();
  loadSettings();
  return () => { alive = false; };
}
