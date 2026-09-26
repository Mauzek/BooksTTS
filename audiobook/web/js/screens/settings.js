// Настройки: слева разделы, справа карточки. Ключи API — в системном
// хранилище Windows, остальное — в библиотеке.

import { del, get, post, put, tauri } from '../api.js';
import { THEMES, applyTheme, currentTheme } from '../theme.js';
import { confirmAction, dropdown, el, emit, guard, icon, toast } from '../ui.js';

const SECTIONS = [
  ['look', 'Внешний вид'],
  ['voice', 'Озвучка'],
  ['markup', 'Разбор по ролям'],
  ['keys', 'Ключи'],
  ['about', 'Обновления'],
];

const LABELS = {
  'anthropic.base_url': ['Адрес сервера', 'Пусто — официальный API Anthropic. Для прокси — его адрес, например https://…:8443.'],
  'anthropic.model': ['Модель разметки', 'Какой моделью Claude разбирать главы по ролям.'],
  'synthesis.parallelism': ['Сколько реплик сразу', 'Для Silero обычно 1, для облака — 2–4.'],
  'engine.silero.model': ['Модель Silero', 'Новее звучит чище; смена модели переозвучит реплики.'],
  'engine.silero.device': ['Где считать Silero', 'Процессор работает везде. Видеокарта быстрее, но нужен torch с CUDA.'],
  'engine.elevenlabs.model': ['Модель ElevenLabs', 'Например, eleven_multilingual_v2.'],
  'engine.qwen.home': ['Папка Qwen3-TTS', 'Окружение и модель, около 9 ГБ. Пусто — %LOCALAPPDATA%\\BookTTS-qwen. Папку можно перенести на другой диск целиком и указать здесь.'],
  'engine.qwen.model': ['Модель Qwen3-TTS', '1.7B звучит лучше, 0.6B вдвое легче для видеопамяти.'],
  'preview.text': ['Фраза для пробы голосов', 'Ей озвучиваются образцы в каталоге голосов.'],
};
const CHOICE_TEXT = { cpu: 'Процессор', cuda: 'Видеокарта (CUDA)' };

export function render(view) {
  let alive = true;
  const hosts = Object.fromEntries(SECTIONS.map(([key]) => [key, el('div', { class: 'settings-rows' })]));
  const navLinks = SECTIONS.map(([key, label]) => el('a', {
    href: `#settings-${key}`, dataset: { section: key },
    onclick: (event) => {
      event.preventDefault();
      document.getElementById(`settings-${key}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    },
  }, label));
  const cards = SECTIONS.map(([key, label]) => el('section', { class: 'card-box settings-card', id: `settings-${key}` },
    el('h2', {}, label), hosts[key]));

  view.replaceChildren(el('div', { class: 'page settings-page' },
    el('div', { class: 'settings-layout' },
      el('nav', { class: 'settings-nav', 'aria-label': 'Разделы настроек' }, el('h1', {}, 'Настройки'), navLinks),
      el('div', { class: 'settings-cards' }, cards))));

  // Подсвечиваем раздел, который сейчас на экране.
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (!visible) return;
    const key = visible.target.id.replace('settings-', '');
    navLinks.forEach((link) => link.classList.toggle('active', link.dataset.section === key));
  }, { root: view, rootMargin: '0px 0px -60% 0px' });
  cards.forEach((card) => observer.observe(card));
  navLinks[0].classList.add('active');

  const row = (title, hint, ...controls) => el('div', { class: 'setting-row' },
    el('div', { class: 'setting-text' }, el('b', {}, title), hint ? el('span', { class: 'hint' }, hint) : null),
    el('div', { class: 'setting-control' }, ...controls));

  function themeRow() {
    const switcher = el('div', { class: 'switcher' });
    const draw = () => switcher.replaceChildren(...THEMES.map(([name, label, iconName]) => el('button', {
      'aria-pressed': String(name === currentTheme()),
      onclick: () => { applyTheme(name, { animate: true }); draw(); },
    }, icon(iconName, { size: 16 }), label)));
    draw();
    return row('Тема', 'Тёмная удобнее вечером. «Как в системе» следует за Windows.', switcher);
  }

  const loadSettings = guard(async () => {
    const data = await get('/api/settings');
    if (!alive) return;
    const keys = Object.keys(data.defaults);
    hosts.look.replaceChildren(themeRow());
    hosts.markup.replaceChildren(...keys.filter((key) => key.startsWith('anthropic.')).map((key) => settingRow(key, data)));
    hosts.voice.replaceChildren(...keys
      .filter((key) => key.startsWith('engine.') || key === 'preview.text' || key === 'synthesis.parallelism')
      .map((key) => (key === 'engine.qwen.home' ? qwenHomeRow() : settingRow(key, data))));
  });

  const gb = (bytes) => `${(bytes / 1024 ** 3).toFixed(1).replace('.', ',')} ГБ`;

  /** Папка Qwen: где лежит сейчас и перенос — например, рядом с программой. */
  function qwenHomeRow() {
    const [label, hint] = LABELS['engine.qwen.home'];
    const where = el('code', { class: 'path' }, 'проверяю…');
    const move = el('button', { class: 'surface', disabled: true }, icon('folder'), 'Перенести…');
    get('/api/engines/qwen/home').then((info) => {
      where.textContent = info.installed ? info.home : 'Qwen3-TTS не установлен';
      move.disabled = !info.installed;
      move.onclick = () => moveQwenDialog(info);
    }).catch(() => { where.textContent = 'не удалось узнать'; });
    return row(label, `${hint} Перенос копирует окружение и модель целиком, а потом удаляет старую папку.`, where, move);
  }

  function moveQwenDialog(info) {
    const dialog = el('dialog', { class: 'move-dialog' });
    const close = () => { dialog.close(); dialog.remove(); };
    const size = el('span', { class: 'hint' }, 'Считаю размер…');
    const custom = el('input', { placeholder: 'например, D:\\BookTTS\\qwen', 'aria-label': 'Новая папка' });
    const beside = info.suggested && !info.home.toLowerCase().startsWith(info.suggested.toLowerCase());
    const choice = (value, title, text, checked) => el('label', { class: 'export-option' },
      el('input', { type: 'radio', name: 'qwen-target', value, checked }),
      el('span', {}, el('b', {}, title), el('span', { class: 'hint' }, text)));
    const pick = el('button', { type: 'button', class: 'surface sm' }, 'Выбрать папку…');
    pick.onclick = guard(async () => {
      if (!tauri?.dialog?.open) return toast('Выбор папки работает в установленном приложении — впишите путь руками');
      const folder = await tauri.dialog.open({ directory: true, title: 'Куда перенести Qwen3-TTS' });
      if (folder) {
        custom.value = `${folder}\\qwen`;
        dialog.querySelector('input[value="custom"]').checked = true;
      }
    });
    const error = el('p', { class: 'form-error', role: 'alert', hidden: true });
    const run = el('button', { class: 'primary lg' }, 'Перенести');
    run.onclick = guard(async () => {
      const kind = dialog.querySelector('input[name="qwen-target"]:checked')?.value;
      const target = kind === 'beside' ? info.suggested : custom.value.trim();
      if (!target) { error.textContent = 'Укажите папку.'; error.hidden = false; return; }
      try {
        const job = await post('/api/engines/qwen/move', { target });
        emit('jobs-changed');
        toast(`${job.title}. Это займёт несколько минут — Qwen в это время недоступен.`, 'ok',
          { title: 'Перенос начался', action: ['Открыть задачи', () => { location.hash = '#/studio/jobs'; }] });
        close();
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
      }
    });
    dialog.append(
      el('div', { class: 'dialog-head' },
        el('div', { class: 'grow' }, el('h3', {}, 'Перенести Qwen3-TTS'),
          el('span', { class: 'hint' }, 'Окружение с CUDA и модель весят около 9 ГБ — их удобно держать не на системном диске.')),
        el('button', { class: 'round', 'aria-label': 'Закрыть', onclick: close }, icon('x'))),
      el('div', { class: 'row hint' }, 'Сейчас: ', el('code', { class: 'path' }, info.home), size),
      el('div', { class: 'export-options' },
        info.suggested ? choice('beside', 'Рядом с программой', `${info.suggested}` +
          (info.suggested_free ? ` · свободно ${gb(info.suggested_free)}` : ''), beside) : null,
        el('label', { class: 'export-option' },
          el('input', { type: 'radio', name: 'qwen-target', value: 'custom', checked: !beside }),
          el('span', { class: 'grow' }, el('b', {}, 'Другая папка'),
            el('span', { class: 'row' }, custom, pick)))),
      error,
      el('div', { class: 'buttons' }, el('button', { class: 'ghost lg', onclick: close }, 'Отмена'), run));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
    document.body.append(dialog);
    dialog.showModal();
    get('/api/engines/qwen/home?size=true')
      .then((full) => { size.textContent = full.size ? ` · ${gb(full.size)}` : ''; })
      .catch(() => { size.textContent = ''; });
  }

  function settingRow(key, data) {
    const [label, hint] = LABELS[key] || [key, ''];
    const choices = data.choices[key];
    const value = data.settings[key];
    const input = choices
      ? dropdown({ label, value, options: choices.map((choice) => ({ value: choice, label: CHOICE_TEXT[choice] || choice })) })
      : el('input', { value: value ?? '', placeholder: data.defaults[key] || '' });
    input.onchange = guard(async () => {
      await put('/api/settings', { values: { [key]: input.value } });
      toast(`${label}: сохранено`, 'ok');
      loadSettings();
    });
    return row(label, hint, input);
  }

  const loadTokens = guard(async () => {
    const { tokens } = await get('/api/tokens');
    if (!alive) return;
    hosts.keys.replaceChildren(
      el('p', { class: 'hint settings-note' }, 'Ключи хранятся в системном хранилище паролей Windows — не в базе и не в открытом файле. Приложение показывает только последние символы.'),
      ...tokens.map(tokenRow));
  });

  function tokenRow(token) {
    const input = el('input', { type: 'password', placeholder: token.present ? 'Новый ключ — заменит старый' : 'Вставьте ключ', 'aria-label': `Ключ ${token.title}` });
    const save = el('button', { class: 'primary' }, 'Сохранить');
    save.onclick = guard(async () => {
      const key = input.value.trim();
      if (!key) return toast('Пустой ключ', 'error');
      await put(`/api/tokens/${token.service}`, { key });
      input.value = '';
      toast(`Ключ ${token.title} сохранён`, 'ok');
      loadTokens();
    });
    input.addEventListener('keydown', (event) => { if (event.key === 'Enter') save.click(); });
    const status = token.present
      ? el('span', { class: 'key-state ok' }, icon('check', { size: 14 }), `${token.masked} · ${token.source === 'keyring' ? 'в хранилище' : 'из окружения'}`)
      : el('span', { class: 'key-state' }, 'не задан');
    const extra = [];
    if (token.can_import_env) {
      extra.push(el('button', {
        class: 'surface', title: `Перенести значение ${token.env} в системное хранилище`,
        onclick: guard(async () => { await post(`/api/tokens/${token.service}/from-env`); toast('Ключ перенесён в хранилище', 'ok'); loadTokens(); }),
      }, 'Перенести из .env'));
    }
    if (token.present && token.source === 'keyring') {
      extra.push(el('button', {
        class: 'ghost danger',
        onclick: guard(async () => {
          if (!await confirmAction(`Удалить ключ ${token.title}?`, 'Его можно будет ввести заново.')) return;
          await del(`/api/tokens/${token.service}`);
          toast('Ключ удалён', 'ok');
          loadTokens();
        }),
      }, 'Удалить'));
    }
    return row(token.title, token.hint, el('div', { class: 'key-controls' }, input, save, ...extra), status);
  }

  /** «Проверить обновления»: пока идёт запрос — «Проверяю…», чтобы не казалось, что окно зависло. */
  function checkButton() {
    const button = el('button', { class: 'surface' }, icon('refresh-cw'), 'Проверить обновления');
    button.onclick = () => {
      button.disabled = true;
      button.replaceChildren(el('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Проверяю…');
      emit('check-updates', {
        onDone: () => {
          button.disabled = false;
          button.replaceChildren(icon('refresh-cw'), 'Проверить обновления');
        },
      });
    };
    return button;
  }

  const loadAbout = guard(async () => {
    const config = await get('/api/config');
    const shell = tauri?.core?.invoke ? await tauri.core.invoke('app_version').catch(() => '') : '';
    if (!alive) return;
    hosts.about.replaceChildren(
      row(`BookTTS ${shell || config.version}`,
        shell ? 'Обновления приходят из GitHub Releases и проверяются по подписи. Кнопка появится в шапке сама.'
          : 'Режим разработки: обновления проверяет только установленное приложение.',
        checkButton()),
      row('Что нового', 'Что изменилось в последней версии — с картинками.',
        el('button', { class: 'surface', onclick: () => emit('whats-new') }, icon('sparkles'), 'Что нового'),
        ),
      row('Папка библиотеки', 'База, копии книг и озвучка. Её можно перенести целиком — пути внутри относительные.',
        el('code', { class: 'path' }, config.library)),
      row('Шрифты', 'Onest и Unbounded — под лицензией SIL Open Font License.', el('span', { class: 'hint' }, 'встроены в приложение')));
  });

  loadSettings();
  loadTokens();
  loadAbout();
  return () => {
    alive = false;
    observer.disconnect();
  };
}
