// Студия — словарь произношений: как читать слова, на которых голос спотыкается.
// Правило действует во всех книгах или только в одной.

import { del, get, post, put } from '../../api.js';
import { dropdown, el, guard, icon, toast } from '../../ui.js';
import { setStudioJobs, studioFrame } from './common.js';

const player = new Audio();

function flattenBooks(tree) {
  const books = [...tree.books];
  const walk = (folders) => folders.forEach((folder) => { books.push(...folder.books); walk(folder.folders); });
  walk(tree.folders);
  return books.sort((a, b) => a.title.localeCompare(b.title, 'ru'));
}

export function render(view, bookId = null) {
  let alive = true;
  let books = [];
  const page = studioFrame(view, 'pronunciation');

  const term = el('input', { placeholder: 'Велимир', 'aria-label': 'Слово' });
  const reading = el('input', { placeholder: 'Велим+ир', 'aria-label': 'Как читать' });
  const scope = dropdown({ label: 'Где действует правило', options: [{ value: '', label: 'во всех книгах' }], value: '' });
  const wholeWord = el('input', { type: 'checkbox', checked: true });
  const caseSensitive = el('input', { type: 'checkbox' });
  const list = el('div', { class: 'rules' });

  /** Голос для пробы: рассказчик выбранной книги, иначе первый голос Silero. */
  async function previewVoice(forBook) {
    if (forBook) {
      const cast = await get(`/api/books/${forBook}/cast`);
      const narrator = cast.speakers.find((s) => s.is_narrator && s.voice) || cast.speakers.find((s) => s.voice);
      if (narrator) return narrator.voice.id;
    }
    const { voices } = await get('/api/voices?engine=silero');
    if (!voices.length) throw new Error('нет голосов Silero — обновите каталог в разделе «Голоса»');
    return voices[0].id;
  }

  const listen = (text, forBook, button) => guard(async () => {
    if (!text.trim()) return toast('Впишите, как читать', 'error');
    button.disabled = true;
    try {
      const voiceId = await previewVoice(forBook);
      const result = await post(`/api/voices/${voiceId}/preview`, { text: text.trim() });
      player.src = result.url;
      await player.play();
    } finally {
      button.disabled = false;
    }
  });

  const add = guard(async () => {
    if (!term.value.trim()) return toast('Впишите слово', 'error');
    await put('/api/pronunciations', {
      term: term.value.trim(), replacement: reading.value.trim(),
      whole_word: wholeWord.checked, case_sensitive: caseSensitive.checked,
      book_id: scope.value ? Number(scope.value) : null,
    });
    toast(`«${term.value.trim()}» — в словаре. Правило сработает при следующей озвучке.`, 'ok');
    term.value = reading.value = '';
    term.focus();
    loadRules();
  });
  reading.addEventListener('keydown', (event) => { if (event.key === 'Enter') add(); });
  term.addEventListener('keydown', (event) => { if (event.key === 'Enter') reading.focus(); });

  const loadRules = guard(async () => {
    const { rules } = await get('/api/pronunciations');
    if (!alive) return;
    if (!rules.length) {
      list.replaceChildren(el('p', { class: 'hint' }, 'Словарь пуст. Добавьте первое слово выше.'));
      return;
    }
    list.replaceChildren(
      el('div', { class: 'rule-row head' }, ['Слово', 'Как читать', 'Где', 'Условия', ''].map((h) => el('span', {}, h))),
      ...rules.map((rule) => {
        const play = el('button', { class: 'round ghost', 'aria-label': `Послушать, как прочитается «${rule.term}»`, title: 'Послушать' },
          icon('play', { className: 'filled', size: 14 }));
        play.onclick = listen(rule.replacement || rule.term, rule.book_id, play);
        const conditions = [rule.whole_word ? 'целое слово' : 'и внутри слов', rule.case_sensitive ? 'с учётом регистра' : ''].filter(Boolean);
        return el('div', { class: 'rule-row' },
          el('b', {}, rule.term),
          el('span', {}, rule.replacement || el('span', { class: 'muted' }, '— без замены —')),
          el('span', { class: 'hint' }, rule.book_id ? `только в «${rule.book_title}»` : 'во всех книгах'),
          el('span', { class: 'hint' }, conditions.join(', ')),
          el('span', { class: 'rule-actions' }, play,
            el('button', {
              class: 'round ghost', 'aria-label': `Удалить «${rule.term}»`, title: 'Удалить',
              onclick: guard(async () => { await del(`/api/pronunciations/${rule.id}`); loadRules(); }),
            }, icon('trash-2'))));
      }));
  });

  const load = guard(async () => {
    const [tree, jobs] = await Promise.all([get('/api/library'), get('/api/jobs?active=true').catch(() => ({ active: 0 }))]);
    if (!alive) return;
    books = flattenBooks(tree);
    scope.setOptions([{ value: '', label: 'во всех книгах' },
      ...books.map((book) => ({ value: book.id, label: `только в «${book.title}»` }))], bookId ?? '');

    const preview = el('button', {}, icon('play', { className: 'filled', size: 12 }), 'Послушать');
    preview.onclick = () => listen(reading.value || term.value, scope.value ? Number(scope.value) : null, preview)();

    setStudioJobs(jobs.active);
    page.replaceChildren(
      el('p', { class: 'lead' }, 'Если голос ставит ударение не туда или спотыкается о слово — запишите, как его читать. ',
        'Правило сработает при следующей озвучке, и переозвучатся только реплики с этим словом.'),
      el('section', { class: 'card-box rule-form' },
        el('div', { class: 'rule-fields' },
          el('label', { class: 'field-inline' }, el('span', { class: 'hint' }, 'Слово'), term),
          el('span', { class: 'rule-arrow', 'aria-hidden': 'true' }, icon('arrow-right')),
          el('label', { class: 'field-inline' }, el('span', { class: 'hint' }, 'Как читать'), reading),
          el('label', { class: 'field-inline' }, el('span', { class: 'hint' }, 'Где'), scope),
          preview,
          el('button', { class: 'primary', onclick: add }, 'Добавить')),
        el('div', { class: 'row hint' },
          el('label', { class: 'row' }, wholeWord, 'только целое слово'),
          el('label', { class: 'row' }, caseSensitive, 'с учётом регистра'),
          el('span', { class: 'grow' }),
          'Ударение — знак + перед ударной гласной: Велим+ир. Можно заменить и сочетание: «т. е.» → «то есть».')),
      el('section', { class: 'card-box' }, list));
    loadRules();
  });

  load();
  return () => {
    alive = false;
    player.pause();
  };
}
