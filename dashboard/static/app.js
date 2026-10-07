'use strict';
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const paths = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  settings: '<path d="m9 3-1 3-3 1 1 3-2 2 2 2-1 3 3 1 1 3h6l1-3 3-1-1-3 2-2-2-2 1-3-3-1-1-3z"/><circle cx="12" cy="12" r="3"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/><path d="M12 14v3"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/>',
  git: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="6" r="2"/><path d="M6 7v10m12-9v3a5 5 0 0 1-5 5H6"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  globe: '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  send: '<path d="m21 3-7 18-4-7-7-4zM10 14 21 3"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
  alert: '<path d="m12 3 10 18H2zM12 9v5M12 17h.01"/>',
  disk: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="12" cy="11" r="4"/><path d="m15 14 3 3M6 17h2"/>',
  folder: '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  external: '<path d="M14 3h7v7M10 14 21 3M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
};
function icon(name) { return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.grid}</svg>`; }
function icons(root = document) { $$('[data-icon]', root).forEach(e => e.innerHTML = icon(e.dataset.icon)); }
const escape = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const timeText = value => value ? new Date(value).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}) : '—';
const fullDate = value => value ? new Date(value).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : 'Ещё не проверялся';
const bytes = value => value == null ? '—' : value >= 1024 ** 3 ? (value / 1024 ** 3).toFixed(1) + ' ГБ' : (value / 1024 ** 2).toFixed(0) + ' МБ';
let projects = [], settings = {}, page = 'projects', selectedId = null, activeTab = 'overview', gitData = null, loggedIn = false, loadingProjects = false, pinEnabled = false;

async function api(path, options = {}) {
  const opts = {credentials:'same-origin', ...options, headers:{'X-App-Request':'1', ...(options.body ? {'Content-Type':'application/json'} : {}), ...options.headers}};
  if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
  const response = await fetch('/api' + path, opts);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== '/unlock') showLock();
    const message = Array.isArray(data.detail) ? data.detail.map(x => x.msg.replace(/^Value error, /, '')).join('; ') : (data.detail || 'Запрос не выполнен');
    throw new Error(message);
  }
  return data;
}
function toast(message, error = false) {
  const node = document.createElement('div'); node.className = 'toast' + (error ? ' error' : '');
  node.innerHTML = icon(error ? 'alert' : 'check') + '<span></span><button aria-label="Закрыть уведомление">×</button>';
  $('span', node).textContent = message;
  $('button', node).onclick = () => node.remove();
  $('#toast-region').append(node); setTimeout(() => node.remove(), error ? 14000 : 6500);
}
async function busy(button, fn) {
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try { return await fn(); } catch (error) { toast(error.message, true); }
  finally { if (button) button.disabled = false; }
}
function confirmAction(title, description, extra = '') {
  const dialog = $('#confirm-dialog');
  if (dialog.open) dialog.close();
  $('#confirm-title').textContent = title; $('#confirm-description').textContent = description;
  $('#confirm-extra').textContent = extra; $('#confirm-extra').hidden = !extra;
  return new Promise(resolve => {
    let accepted = false;
    $('#confirm-accept').onclick = () => { accepted = true; dialog.close(); };
    $('#confirm-cancel').onclick = () => dialog.close();
    dialog.addEventListener('close', () => resolve(accepted), {once:true});
    dialog.showModal();
  });
}
function statusInfo(p) {
  if (!p.enabled) return ['neutral', 'На паузе'];
  const status = p.state?.status;
  if (status === 'error') return ['error', 'Внимание'];
  if (status === 'ok') return ['good', 'В порядке'];
  if (status === 'unconfigured') return ['neutral', 'Не настроен'];
  return ['neutral', 'Ожидает проверки'];
}
function badge(p) { const [tone, label] = statusInfo(p); return `<span class="badge ${tone}">${label}</span>`; }
function getProject() { return projects.find(p => p.id === selectedId); }

function renderProjects() {
  const count = projects.length, active = projects.filter(p => p.enabled).length;
  $('#nav-count').textContent = count; $('#project-heading-count').textContent = String(count).padStart(2, '0');
  $('#monitor-summary').textContent = active ? `Мониторинг: ${active} ${active === 1 ? 'проект' : active < 5 ? 'проекта' : 'проектов'}` : 'Мониторинг готов';
  const healthy = projects.filter(p => p.enabled && p.state?.status === 'ok').length;
  const issues = projects.filter(p => p.enabled && p.state?.status === 'error').length;
  const changed = projects.filter(p => (p.state?.git?.files?.length || 0) > 0).length;
  const stats = [
    ['Всего проектов', count, 'folder', 'В вашем рабочем пространстве', ''],
    ['В порядке', healthy, 'shield', 'Все настроенные проверки проходят', 'good'],
    ['Требуют внимания', issues, 'alert', 'Недоступность или ошибка проверки', issues ? 'warning' : ''],
    ['Изменения файлов', changed, 'git', 'Проекты с изменениями на сервере', ''],
  ];
  $('#stats').innerHTML = stats.map(([title,n,i,caption,tone]) => `<div class="stat-card"><div class="stat-label">${title}${icon(i)}</div><div class="stat-value ${tone}">${String(n).padStart(2,'0')}</div><div class="stat-caption">${caption}</div></div>`).join('');
  const query = $('#search').value.toLowerCase(), filter = $('#status-filter').value;
  const visible = projects.filter(p => `${p.name} ${p.url} ${p.description}`.toLowerCase().includes(query) && (filter === 'all' || (filter === 'paused' ? !p.enabled : p.enabled && p.state?.status === filter)));
  if (!visible.length) {
    $('#project-list').innerHTML = `<div class="empty-state"><div class="empty-icon">${icon(count ? 'search' : 'folder')}</div><h3>${count ? 'Ничего не найдено' : 'Начните с первого проекта'}</h3><p>${count ? 'Попробуйте другой запрос или измените фильтр статуса.' : 'Добавьте адрес сайта или подключите сервер. Мы поможем следить за доступностью, свободным местом и изменениями файлов.'}</p>${count ? '<button class="button secondary" id="clear-search">Сбросить фильтры</button>' : '<button class="button primary" id="empty-add">'+icon('plus')+'Добавить проект</button>'}</div>`;
    $('#empty-add')?.addEventListener('click', () => openForm());
    $('#clear-search')?.addEventListener('click', () => { $('#search').value = ''; $('#status-filter').value = 'all'; renderProjects(); });
  } else {
    $('#project-list').innerHTML = visible.map((p, index) => {
      const state = p.state || {}, web = state.web || {}, disk = state.disk || {}, git = state.git || {};
      let response = web.code ? `HTTP ${web.code}` : p.url ? 'Ожидание' : 'Не настроен';
      if (web.ok === false) response = web.code ? `HTTP ${web.code}` : 'Недоступен';
      const diskText = disk.free != null ? bytes(disk.free) : disk.error ? 'Ошибка' : '—';
      const gitText = git.error ? 'Ошибка' : git.files ? `${git.files.length} файлов` : '—';
      return `<article class="project-card"><div class="card-header"><div class="project-avatar tone-${index%4}">${escape(p.name[0].toUpperCase())}</div><div class="card-title"><h3>${escape(p.name)}</h3>${p.url ? `<a class="card-url" href="${escape(p.url)}" target="_blank" rel="noopener noreferrer">${escape(p.url)}</a>` : `<span class="card-url">${escape(p.ssh_host || 'Подключение не настроено')}</span>`}</div>${badge(p)}</div><p class="card-description">${escape(p.description || (p.ssh_path ? p.ssh_path : 'Ваш проект в едином рабочем пространстве'))}</p><div class="card-metrics"><div class="card-metric"><small>Доступность</small><strong class="${web.ok ? 'mint' : ''}">${response}</strong></div><div class="card-metric"><small>Свободно на диске</small><strong>${diskText}</strong></div><div class="card-metric"><small>Изменения Git</small><strong>${gitText}</strong></div></div><div class="card-footer"><div class="connection-tags">${p.url ? `<span class="connection-tag">${icon('globe')}HTTP</span>` : ''}${p.ssh_host ? `<span class="connection-tag">${icon('terminal')}SSH</span>` : ''}${p.git_monitor && p.ssh_host ? `<span class="connection-tag">${icon('git')}GIT</span>` : ''}</div><button class="card-open" data-open="${p.id}">Открыть проект ${icon('arrow')}</button></div></article>`;
    }).join('');
  }
  $('#last-updated').textContent = timeText(new Date());
}
async function loadProjects() {
  if (!loggedIn || loadingProjects) return;
  loadingProjects = true;
  try {
    projects = await api('/projects'); renderProjects();
    if ($('#detail-dialog').open && activeTab === 'overview' && getProject()) renderOverview();
  } finally { loadingProjects = false; }
}
async function navigate(target) {
  page = target;
  $$('[data-nav]').forEach(b => b.classList.toggle('active', b.dataset.nav === target));
  ['projects','events','settings'].forEach(name => $('#' + name + '-page').hidden = name !== target);
  $('#breadcrumb-current').textContent = {projects:'Проекты',events:'Журнал событий',settings:'Настройки'}[target];
  if (target === 'events') await loadEvents();
  if (target === 'settings') await loadSettings();
}

function openForm(id = null) {
  const form = $('#project-form'); form.reset();
  const p = id ? projects.find(p => p.id === id) : null;
  $('#project-form-title').textContent = p ? 'Настройки проекта' : 'Новый проект';
  form.elements.id.value = id || '';
  if (p) {
    Object.entries(p).forEach(([key, value]) => {
      const field = form.elements.namedItem(key);
      if (!field || ['ssh_password','disk_token'].includes(key)) return;
      if (field.type === 'checkbox') field.checked = Boolean(value); else field.value = value ?? '';
    });
  }
  $('#ssh-password-hint').textContent = p?.has_password ? 'Пароль сохранён. Оставьте пустым, чтобы не менять.' : 'Хранится зашифрованным на вашем ПК.';
  $('#project-form-error').textContent = '';
  $('#project-form-dialog').showModal();
}
function formProject(form) {
  const data = Object.fromEntries(new FormData(form)); delete data.id;
  ['enabled','notify','clear_password','git_monitor','auto_sync','auto_push'].forEach(key => data[key] = form.elements[key].checked);
  ['interval','ssh_port','disk_threshold'].forEach(key => data[key] = Number(data[key]));
  if (!data.ssh_password) delete data.ssh_password;
  if (!data.disk_token) delete data.disk_token;
  return data;
}
$('#project-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget, id = form.elements.id.value, data = formProject(form);
  const old = projects.find(p => p.id === id);
  if (data.auto_sync && (!old?.auto_sync || (data.auto_push && !old?.auto_push)) && !await confirmAction('Включить автоматические коммиты?', 'На каждой проверке приложение будет добавлять допустимые изменённые файлы проекта в Git и создавать коммит на сервере.' + (data.auto_push ? '\nКоммиты также будут отправляться в текущую ветку origin без отдельного подтверждения.' : '') + '\nStaged и потенциально секретные файлы остановят автоматизацию.')) return;
  const button = $('button[type=submit]', form); button.disabled = true; $('#project-form-error').textContent = '';
  try {
    await api(id ? '/projects/' + id : '/projects', {method:id ? 'PUT' : 'POST', body:data});
    $('#project-form-dialog').close(); await loadProjects(); toast('Проект сохранён');
    if (id === selectedId && $('#detail-dialog').open) await renderDetail();
  } catch (error) { $('#project-form-error').textContent = error.message; }
  finally { button.disabled = false; }
});

async function openDetail(id) {
  selectedId = id; activeTab = 'overview'; gitData = null;
  const p = getProject(); if (!p) return;
  $('#detail-name').textContent = p.name; $('#detail-avatar').textContent = p.name[0].toUpperCase();
  $('#detail-dialog').showModal(); await renderDetail();
}
async function renderDetail() {
  const p = getProject(); if (!p) { $('#detail-dialog').close(); return; }
  $('#detail-name').textContent = p.name;
  $$('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === activeTab));
  if (activeTab === 'overview') renderOverview();
  if (activeTab === 'git') await loadGit();
  if (activeTab === 'events') {
    $('#detail-content').innerHTML = '<div class="loading">Загружаем события…</div>';
    const id = selectedId, events = await api('/events?project_id=' + id);
    if (selectedId === id && activeTab === 'events') $('#detail-content').innerHTML = '<div class="event-list">' + eventsHTML(events) + '</div>';
  }
}
function renderOverview() {
  const p = getProject(); if (!p) return;
  const state = p.state || {}, web = state.web || {}, disk = state.disk || {};
  const git = state.git || {};
  $('#detail-content').innerHTML = `<div class="detail-topline"><div>${badge(p)}<p class="value-caption" style="margin-top:9px">${p.url ? `<a href="${escape(p.url)}" target="_blank" rel="noopener noreferrer">${escape(p.url)}</a>` : 'Адрес сайта не указан'}<br>Последняя проверка: ${fullDate(state.checked_at)}</p></div><button class="button primary" data-detail-action="check">${icon('refresh')}Проверить сейчас</button></div><div class="detail-grid"><section class="panel"><h3>ДОСТУПНОСТЬ САЙТА</h3><div class="big-value ${web.ok ? 'mint' : ''}">${web.code ? `HTTP ${web.code}` : web.ok === false ? 'Недоступен' : '—'}</div><p class="value-caption">${web.latency != null ? 'Ответ за ' + web.latency + ' мс' : p.url ? 'Ожидаем результат проверки' : 'Добавьте адрес сайта в настройках'}<br>Интервал: ${p.interval < 60 ? p.interval + ' сек.' : Math.round(p.interval / 60) + ' мин.'}</p></section><section class="panel"><h3>СВОБОДНОЕ МЕСТО НА СЕРВЕРЕ</h3><div class="big-value">${bytes(disk.free)} <span style="font-size:13px;color:var(--muted)">${disk.total ? 'из ' + bytes(disk.total) : ''}</span></div><p class="value-caption">${disk.free_percent != null ? 'Свободно ' + disk.free_percent + '% · ' + escape(disk.source) : escape(disk.error || 'Нет данных')}<br>Порог уведомления: ${p.disk_threshold}% свободного места</p>${disk.free_percent != null ? `<div class="disk-bar"><span style="width:${Math.max(0,Math.min(100,disk.free_percent))}%"></span></div>` : ''}</section></div>${state.issues?.length ? `<div class="issue-list">${state.issues.map(i=>`<p>${escape(i)}</p>`).join('')}</div>` : ''}<div class="detail-connection"><div><h3>SSH-подключение</h3><p>${p.ssh_host ? escape(`${p.ssh_user}@${p.ssh_host}:${p.ssh_port} · ${p.ssh_path}`) : 'Не настроено'}</p><p>${p.ssh_fingerprint ? 'Отпечаток подтверждён · ' + escape(p.ssh_fingerprint) : 'Отпечаток ещё не подтверждён'}</p></div><div class="detail-actions" style="margin:0">${p.ssh_host ? `<button class="button secondary small" data-detail-action="fingerprint">${icon('shield')}${p.ssh_fingerprint ? 'Проверить ключ' : 'Подтвердить ключ'}</button><button class="button secondary small" data-detail-action="ssh-test">${icon('terminal')}Тест SSH</button>` : '<button class="button secondary small" data-detail-action="edit">Настроить</button>'}</div></div><div class="detail-connection"><div><h3>Git на сервере</h3><p>${git.connected ? escape(git.branch + ' · ' + git.remote) : escape(git.error || 'Репозиторий не проверен или ещё не подключён')}</p><p>${p.auto_sync ? 'Автоматические коммиты включены' : 'Изменения принимаются вручную'}${p.auto_sync && p.auto_push ? ' · push в текущую ветку' : ''}</p></div><button class="button secondary small" data-detail-action="git">${icon('git')}Открыть Git</button></div><div class="detail-actions">${p.ssh_host ? `<button class="button secondary small" data-detail-action="deploy">${icon('disk')}Установить PHP-пробу</button>` : ''}<button class="button secondary small" data-detail-action="pause">${p.enabled ? 'Приостановить мониторинг' : 'Включить мониторинг'}</button><button class="danger-link" data-detail-action="delete">Удалить проект</button></div><p class="hint">HTTP-проба необязательна: по SSH место проверяется без установки файлов. Для пробы сервер должен выполнять PHP 8+ и передавать заголовок Authorization; HTTPS URL укажите в настройках.</p>`;
}
$('#detail-edit').onclick = () => openForm(selectedId);
$$('[data-tab]').forEach(b => b.addEventListener('click', () => busy(b, async () => {activeTab = b.dataset.tab; await renderDetail();})));
$('#detail-content').addEventListener('click', event => {
  const button = event.target.closest('[data-detail-action]'); if (!button) return;
  busy(button, () => detailAction(button.dataset.detailAction));
});
async function detailAction(action) {
  const p = getProject(), id = p.id;
  if (action === 'edit') return openForm(id);
  if (action === 'git') { activeTab = 'git'; return renderDetail(); }
  if (action === 'check') {
    await api(`/projects/${id}/check`, {method:'POST'}); await loadProjects(); toast('Проверка завершена');
  }
  if (action === 'ssh-test') { const result = await api(`/projects/${id}/ssh/test`, {method:'POST'}); toast(result.message); }
  if (action === 'fingerprint') {
    const key = await api(`/projects/${id}/ssh/fingerprint`);
    const accepted = await confirmAction('Проверить ключ SSH-сервера', `Сверьте этот отпечаток с ключом сервера через панель хостинга или доверенный канал.\nСервер: ${key.host}:${key.port}\nАлгоритм: ${key.algorithm}\nПодтверждение закрепит ключ для следующих подключений.`, key.fingerprint);
    if (accepted) { await api(`/projects/${id}/ssh/trust`, {method:'POST', body:{fingerprint:key.fingerprint}}); await loadProjects(); toast('Ключ сервера подтверждён'); }
  }
  if (action === 'deploy') {
    if (!await confirmAction('Установить PHP-пробу?', `В ${p.ssh_path}/.devlin-disk.php будет добавлен файл для чтения объёма диска по HTTPS.\nСуществующий файл не перезаписывается. Проба возвращает только свободный и общий объём после проверки ключа.\nПродолжить установку?`)) return;
    const result = await api(`/projects/${id}/disk/deploy`, {method:'POST', body:{confirm:true}});
    await loadProjects();
    await confirmAction('PHP-проба установлена', 'Ключ сохранён в приложении. Скопируйте его для внешних проверок сейчас — повторно он не показывается.\nЗатем укажите HTTPS URL файла и выберите проверку через PHP-пробу в настройках проекта.', result.token);
  }
  if (action === 'pause') {
    const data = Object.fromEntries(Object.entries(p).filter(([k]) => !['id','state','has_password','has_disk_token','created_at','ssh_fingerprint'].includes(k)));
    data.enabled = !p.enabled;
    await api(`/projects/${id}`, {method:'PUT', body:data}); await loadProjects(); toast(data.enabled ? 'Мониторинг включён' : 'Мониторинг приостановлен');
  }
  if (action === 'delete' && await confirmAction('Удалить проект?', `«${p.name}» будет удалён из приложения. Файлы сервера и Git-репозиторий сохранятся.`)) {
    await api(`/projects/${id}`, {method:'DELETE'}); $('#detail-dialog').close(); selectedId = null; await loadProjects(); toast('Проект удалён');
  }
}

async function loadGit() {
  const id = selectedId, p = getProject(); gitData = null;
  if (!p.ssh_host) { $('#detail-content').innerHTML = '<div class="git-info">Для работы с файлами сервера сначала настройте SSH и подтвердите ключ сервера в разделе «Обзор».</div><button class="button secondary" data-detail-action="edit">Настроить SSH</button>'; return; }
  $('#detail-content').innerHTML = '<div class="loading">Читаем состояние репозитория на сервере…</div>';
  try {
    const data = await api(`/projects/${id}/git`);
    if (id !== selectedId || activeTab !== 'git') return;
    gitData = data; renderGit();
  } catch (error) {
    if (id === selectedId && activeTab === 'git') $('#detail-content').innerHTML = `<div class="error-panel">${escape(error.message)}<br><button class="button secondary" data-detail-action="edit">Настроить подключение</button> <button class="button secondary" data-detail-action="git">Повторить</button></div>`;
  }
}
function renderGit() {
  const g = gitData, p = getProject();
  $('#detail-content').innerHTML = `<div class="git-heading"><div><h3>${g.connected ? escape(g.branch) : 'Подключите репозиторий'}</h3><p>${g.connected ? escape(g.remote || 'origin не настроен') + (g.head ? ' · ' + escape(g.head) : '') : 'Файлы проекта уже на сервере. Выберите их для первого коммита.'}</p></div><div class="detail-actions" style="margin:0"><button class="button secondary small" data-git-action="refresh">${icon('refresh')}Обновить</button>${g.connected ? `<button class="button secondary small" data-git-action="pull">${icon('git')}Загрузить из Git</button><button class="button secondary small" data-git-action="push">${icon('send')}Отправить коммиты</button>` : ''}</div></div><div class="git-info">${g.connected ? 'Отметьте файлы, проверьте diff и создайте коммит из текущих файлов на сервере. Push отправит его в текущую ветку origin, в том числе в GitLab.' : 'Создайте пустой репозиторий в GitLab или GitHub и укажите его URL. SSH-ключ сервера должен иметь право записи. Начальное содержимое удалённого репозитория должно быть пустым.'}</div>${g.staged ? '<div class="git-info git-warning">В индексе уже есть staged-изменения. Завершите или отмените их на сервере: приложение не будет включать их в свой коммит.</div>' : ''}${g.files.some(f=>!f.allowed) ? '<div class="git-info git-warning">Потенциально секретные файлы и зависимости исключены из синхронизации. Проверяйте содержимое разрешённых файлов — фильтр имени не обнаруживает все секреты.</div>' : ''}${!g.connected ? `<div class="fields"><label>URL репозитория<input id="git-remote" placeholder="git@gitlab.com:group/project.git"></label><label>Начальная ветка<input id="git-branch" value="${escape(p.git_branch || 'main')}" placeholder="main"></label></div>` : ''}${g.files.length ? `<div class="file-list"><div class="file-row"><label><input type="checkbox" id="select-all-files" checked><span>Выбрать все допустимые файлы (${g.files.filter(f=>f.allowed).length})</span></label></div>${g.files.map(f=>`<div class="file-row ${f.allowed ? '' : 'blocked'}"><label><input class="git-file" type="checkbox" value="${escape(f.path)}" ${f.allowed ? 'checked' : 'disabled'}><span class="file-status">${escape(f.status)}</span><span class="file-name" title="${escape(f.path)}">${escape(f.path)}</span></label>${f.allowed ? `<button class="view-diff" data-file-diff="${escape(f.path)}">Посмотреть →</button>` : '<span class="badge warning">Исключён</span>'}</div>`).join('')}</div><div class="git-controls"><label>Сообщение коммита<input id="commit-message" value="${g.connected ? 'Обновление файлов проекта' : 'Initial project import'}" maxlength="500" required></label><button class="button primary" data-git-action="commit" ${g.staged ? 'disabled' : ''}>${icon('git')}${g.connected ? 'Создать коммит' : 'Подключить и выгрузить'}</button></div><label class="check-line"><input id="commit-push" type="checkbox" checked><span>Отправить коммит в origin после создания</span></label><pre id="file-diff" class="diff-view" hidden></pre>` : '<div class="events-empty">Рабочая директория чистая. Изменений для коммита нет.</div>'}<p class="hint">Без force push, reset или перезаписи файлов. «Загрузить из Git» использует pull --ff-only и требует чистую рабочую директорию. Изменившийся после просмотра набор файлов потребует нового подтверждения.</p>`;
  $('#select-all-files')?.addEventListener('change', event => $$('.git-file:not(:disabled)').forEach(box => box.checked = event.target.checked));
}
$('#detail-content').addEventListener('click', event => {
  const diffButton = event.target.closest('[data-file-diff]');
  if (diffButton) return busy(diffButton, async () => {
    const id = selectedId, result = await api(`/projects/${id}/git/diff?path=${encodeURIComponent(diffButton.dataset.fileDiff)}`);
    if (selectedId !== id || activeTab !== 'git' || !$('#file-diff')) return;
    $('#file-diff').hidden = false; $('#file-diff').textContent = result.diff; $('#file-diff').scrollIntoView({behavior:'smooth',block:'nearest'});
  });
  const button = event.target.closest('[data-git-action]');
  if (button) busy(button, () => gitAction(button.dataset.gitAction));
});
async function gitAction(action) {
  if (action === 'refresh') return loadGit();
  const p = getProject(), id = p.id;
  if (action === 'pull' || action === 'push') {
    if (!await confirmAction(action === 'pull' ? 'Обновить файлы сервера из Git?' : 'Отправить коммиты?', action === 'pull' ? `В ${p.ssh_path} будет выполнен pull --ff-only из origin/${gitData.branch}. Операция остановится при локальных изменениях или расхождении истории.` : `Коммиты текущей ветки ${gitData.branch} будут отправлены в origin без force push.`)) return;
    const result = await api(`/projects/${id}/git/${action}`, {method:'POST', body:{confirm:true}}); toast(result.message); await loadGit(); return;
  }
  if (action === 'commit') {
    const files = $$('.git-file:checked').map(box => box.value), message = $('#commit-message').value.trim(), push = $('#commit-push').checked;
    if (!files.length || !message) throw new Error('Выберите файлы и укажите сообщение коммита');
    const data = {snapshot:gitData.snapshot, files, message, push};
    if (!gitData.connected) { data.remote = $('#git-remote').value.trim(); data.branch = $('#git-branch').value.trim(); if (!data.remote || !data.branch) throw new Error('Укажите URL репозитория и ветку'); }
    if (!await confirmAction(gitData.connected ? 'Принять изменения сервера в Git?' : 'Создать репозиторий на сервере?', `${files.length} файлов будут добавлены в коммит «${message}».\n${push ? 'Коммит будет отправлен в выбранную ветку origin.' : 'Коммит останется только на сервере.'}\nПроверьте, что в файлах нет паролей и других секретов.`, files.join('\n'))) return;
    const result = await api(`/projects/${id}/git/${gitData.connected ? 'sync' : 'init'}`, {method:'POST', body:data});
    toast(result.warning || `Коммит ${result.commit} ${result.pushed ? 'отправлен в origin' : 'создан на сервере'}`, Boolean(result.warning));
    await loadGit();
  }
}

function eventsHTML(events) {
  if (!events.length) return '<div class="events-empty">Событий пока нет. Здесь появятся результаты проверок и операций.</div>';
  return events.map(event => {
    const project = projects.find(p=>p.id===event.project_id);
    return `<div class="event-row"><span class="event-indicator ${escape(event.level)}">${icon(event.level === 'error' || event.level === 'warning' ? 'alert' : event.level === 'success' ? 'check' : 'activity')}</span><div class="event-copy">${escape(event.message)}<small>${escape(project?.name || (event.project_id ? 'Удалённый проект' : 'Приложение'))}</small></div><time datetime="${escape(event.created_at)}">${fullDate(event.created_at)}</time></div>`;
  }).join('');
}
async function loadEvents() {
  $('#all-events').innerHTML = '<div class="loading">Загружаем журнал…</div>';
  $('#all-events').innerHTML = eventsHTML(await api('/events'));
}
async function loadSettings() {
  settings = await api('/settings'); const form = $('#settings-form'); form.reset();
  Object.entries(settings).forEach(([key,value])=>{ const field = form.elements.namedItem(key); if (!field) return; if (field.type === 'checkbox') field.checked = Boolean(value); else field.value = value; });
  $('#bot-status').className = 'badge ' + (settings.telegram_configured ? 'good' : 'neutral');
  $('#bot-status').textContent = settings.telegram_configured ? 'Токен сохранён' : 'Не подключён';
  $('#token-hint').textContent = settings.telegram_configured ? 'Токен сохранён. Оставьте поле пустым, чтобы не менять.' : 'Создайте бота через @BotFather.';
  $('#pin-status').className = 'badge ' + (settings.pin_enabled ? 'good' : 'neutral');
  $('#pin-status').textContent = settings.pin_enabled ? 'Включена' : 'Выключена';
  pinEnabled = settings.pin_enabled;
  $('#current-pin-field').hidden = !settings.pin_enabled;
  form.elements.disable_pin.disabled = !settings.pin_enabled;
}
$('#settings-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('button[type=submit]', form), async () => {
    const data = Object.fromEntries(new FormData(form));
    ['notify_recovery','clear_telegram_token','disable_pin'].forEach(k=>data[k]=form.elements[k].checked);
    if (!data.pin) delete data.pin;
    if (!data.telegram_token) delete data.telegram_token;
    await api('/settings', {method:'PUT',body:data}); await loadSettings(); toast('Настройки сохранены');
  });
});
$('#test-telegram').onclick = event => busy(event.currentTarget, async () => {
  if (!await confirmAction('Отправить тестовое уведомление?', 'Бот отправит одно сообщение в сохранённый Telegram Chat ID.')) return;
  const result = await api('/telegram/test',{method:'POST'}); toast(result.message);
});

function showLock() {
  loggedIn = false;
  $$('dialog[open]').forEach(d=>d.close());
  $('#app-shell').hidden = true; $('#lock-screen').hidden = false; $('#unlock-form').hidden = false;
  $('#lock-description').textContent = pinEnabled ? 'Введите PIN-код, чтобы продолжить.' : 'Сессия завершена. Откройте приложение повторно.';
  $('#unlock-pin').required = pinEnabled; $('#unlock-pin').hidden = !pinEnabled; $('#unlock-form label').hidden = !pinEnabled;
  $('#unlock-pin').value = '';
  if (pinEnabled) $('#unlock-pin').focus(); else $('#unlock-form button').focus();
}
async function boot() {
  const session = await api('/session');
  pinEnabled = session.pin_enabled;
  if (!session.authenticated) {
    if (session.pin_enabled) { showLock(); return; }
    await api('/unlock', {method:'POST',body:{pin:''}});
  }
  loggedIn = true; $('#app-shell').hidden = false; $('#lock-screen').hidden = true;
  await loadProjects(); await navigate(page);
}
$('#unlock-form').addEventListener('submit', event => {
  event.preventDefault(); const button = $('button', event.currentTarget); button.disabled = true;
  api('/unlock',{method:'POST',body:{pin:$('#unlock-pin').value}}).then(()=>{ $('#unlock-error').textContent=''; return boot(); }).catch(error=>$('#unlock-error').textContent=error.message).finally(()=>button.disabled=false);
});
$('#lock-button').onclick = event => busy(event.currentTarget, async () => {
  const state = await api('/session');
  if (!state.pin_enabled) { await navigate('settings'); toast('Сначала установите PIN в настройках', true); return; }
  await api('/lock',{method:'POST'}); showLock();
});
$$('[data-nav]').forEach(button => button.onclick = () => busy(button, () => navigate(button.dataset.nav)));
$('.brand').onclick = event => {event.preventDefault(); busy(null, () => navigate('projects'));};
['hero-add','list-add'].forEach(id => $('#'+id).onclick=()=>openForm());
$('#events-shortcut').onclick = () => busy(null, () => navigate('events'));
$('#refresh-events').onclick = event => busy(event.currentTarget, loadEvents);
$('#search').oninput = renderProjects; $('#status-filter').onchange = renderProjects;
$('#project-list').onclick = event => { const button=event.target.closest('[data-open]'); if(button) busy(button,()=>openDetail(button.dataset.open)); };
document.addEventListener('click', event => { const button=event.target.closest('[data-close]'); if(button) $('#'+button.dataset.close).close(); });
icons();
boot().catch(error => { $('#lock-description').textContent = 'Не удалось подключиться: ' + error.message; });
setInterval(()=> {if(loggedIn) loadProjects().catch(error=>toast(error.message,true));},15000);
