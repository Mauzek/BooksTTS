// Общение с бэкендом и с оболочкой. Интерфейс одинаков в Tauri и в браузере:
// всё, что зависит от окружения, спрятано здесь.

const SESSION_KEY = 'booktts-session';

/** Сессия редактирования — по ней живёт история отмены. Только ASCII: уходит в заголовок. */
export const SESSION = (() => {
  const fresh = () => 'sess-' + (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2));
  try {
    let value = localStorage.getItem(SESSION_KEY);
    if (!value) { value = fresh(); localStorage.setItem(SESSION_KEY, value); }
    return value;
  } catch {
    return fresh();
  }
})();

/** Оболочка Tauri, если мы в ней. В браузере — null. */
export const tauri = window.__TAURI__ || null;

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

function detailText(detail) {
  if (Array.isArray(detail)) return detail.map((d) => d.msg || String(d)).join('; ');
  return typeof detail === 'string' ? detail : '';
}

export async function api(path, { method = 'GET', body, headers = {} } = {}) {
  const init = {
    method,
    headers: { 'x-session': SESSION, 'x-requested-with': 'booktts', ...headers },
  };
  if (body !== undefined) {
    if (body instanceof Blob || body instanceof ArrayBuffer) {
      init.body = body;
    } else {
      init.body = JSON.stringify(body);
      init.headers['content-type'] = 'application/json';
    }
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError('нет связи с приложением — оно перезапускается, повторите через пару секунд', 0);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(detailText(data.detail) || response.statusText, response.status);
  return data;
}

export const get = (path) => api(path);
export const post = (path, body = {}) => api(path, { method: 'POST', body });
export const patch = (path, body = {}) => api(path, { method: 'PATCH', body });
export const put = (path, body = {}) => api(path, { method: 'PUT', body });
export const del = (path) => api(path, { method: 'DELETE' });

const FINISHED = new Set(['done', 'failed', 'cancelled']);

/**
 * Дождаться задачи из очереди, сообщая о прогрессе.
 * Возвращает итоговое состояние задачи; ошибкой не считает — решает вызывающий.
 */
export async function waitForJob(jobId, onUpdate = () => {}, { interval = 1000, alive = () => true } = {}) {
  for (;;) {
    const job = await get(`/api/jobs/${jobId}`);
    onUpdate(job);
    if (FINISHED.has(job.status) || !alive()) return job;
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
}

/**
 * Выбрать файлы книг. В десктопе — системный диалог (возвращает пути),
 * в браузере — обычный <input type=file> (возвращает File).
 */
export async function pickBookFiles(extensions, { title = 'Добавить книги' } = {}) {
  const plain = [...new Set(extensions.map((e) => e.split('.').pop()))];
  if (tauri?.dialog?.open) {
    const picked = await tauri.dialog.open({
      multiple: true,
      title,
      filters: [{ name: 'Книги', extensions: plain }],
    });
    if (!picked) return [];
    return (Array.isArray(picked) ? picked : [picked]).map((path) => ({ path }));
  }
  return new Promise((resolve) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.accept = extensions.join(',');
    input.onchange = () => resolve([...input.files].map((file) => ({ file })));
    input.click();
  });
}

/** Импортировать выбранное: путь (десктоп) или содержимое файла (браузер, drag-and-drop). */
export async function importBook(item, folderId = null) {
  if (item.path) return post('/api/books/import', { path: item.path, folder_id: folderId });
  const params = new URLSearchParams({ filename: item.file.name });
  if (folderId !== null && folderId !== undefined) params.set('folder_id', folderId);
  return api(`/api/books/upload?${params}`, { method: 'PUT', body: item.file });
}

/** Дописать в книгу главы из выбранного файла: путь (десктоп) или содержимое (браузер). */
export async function importChapters(bookId, item, after = null) {
  if (item.path) return post(`/api/books/${bookId}/chapters/import`, { path: item.path, after });
  const params = new URLSearchParams({ filename: item.file.name });
  if (after !== null && after !== undefined) params.set('after', after);
  return api(`/api/books/${bookId}/chapters/upload?${params}`, { method: 'PUT', body: item.file });
}

/**
 * Системное уведомление Windows. В десктопе его показывает оболочка от имени
 * BookTTS, и нажатие открывает экран route. Оболочка постарше этой команды не
 * знает — тогда плагин Tauri, в браузере — Web Notifications.
 */
export async function notify(title, body = '', route = null) {
  try {
    if (tauri?.core?.invoke) {
      try {
        await tauri.core.invoke('notify', { title, body, route });
        return;
      } catch {
        // старая оболочка — ниже плагин
      }
    }
    const plugin = tauri?.notification;
    if (plugin) {
      let granted = await plugin.isPermissionGranted();
      if (!granted) granted = (await plugin.requestPermission()) === 'granted';
      if (granted) plugin.sendNotification({ title, body });
      return;
    }
    if ('Notification' in window) {
      if (Notification.permission === 'default') await Notification.requestPermission();
      if (Notification.permission === 'granted') new Notification(title, { body });
    }
  } catch {
    // уведомление — не повод ронять интерфейс
  }
}
