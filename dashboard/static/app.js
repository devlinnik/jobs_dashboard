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
let sshProfiles = [], remoteProjectId = null, remoteProfileId = '', remoteEndpoint = '', remoteCurrent = '', remoteParent = null, remoteRequest = 0, probeProjectId = null;
let gitHistory = null, gitHistoryRequest = 0;
let gitProfiles = [], gitConnectState = null, repositoryPage = 1, repositoryItems = [], repositoryRequest = 0;

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
  const region = $('#toast-region'); region.append(node); region.scrollTop = region.scrollHeight;
  setTimeout(() => node.remove(), error ? 20000 : 10000);
}

// A modal dialog lives in the browser's top layer: z-index on the page cannot
// raise a toast above it, and the rest of the document is inert. Keep the same
// live region inside the most recently opened dialog, moving it back on close.
const notificationDialogs = [];
new MutationObserver(records => {
  for (const {target} of records) {
    if (!(target instanceof HTMLDialogElement)) continue;
    const index = notificationDialogs.indexOf(target);
    if (index !== -1) notificationDialogs.splice(index, 1);
    if (target.open) notificationDialogs.push(target);
  }
  const host = notificationDialogs.at(-1) || document.body;
  if ($('#toast-region').parentElement !== host) host.append($('#toast-region'));
}).observe(document.body, {subtree:true, attributes:true, attributeFilter:['open']});
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
    [projects, sshProfiles, gitProfiles] = await Promise.all([api('/projects'), api('/ssh-profiles'), api('/git-profiles')]); renderProjects();
    if ($('#detail-dialog').open && activeTab === 'overview' && getProject()) renderOverview();
  } finally { loadingProjects = false; }
}
async function navigate(target) {
  page = target;
  $$('[data-nav]').forEach(b => b.classList.toggle('active', b.dataset.nav === target));
  ['projects','events','settings','ssh-profiles','git-profiles'].forEach(name => $('#' + name + '-page').hidden = name !== target);
  $('#breadcrumb-current').textContent = {projects:'Проекты',events:'Журнал событий',settings:'Настройки','ssh-profiles':'SSH-профили','git-profiles':'Git-профили'}[target];
  if (target === 'events') await loadEvents();
  if (target === 'settings') await loadSettings();
  if (target === 'ssh-profiles') await loadProfiles();
  if (target === 'git-profiles') await loadGitProfiles();
}

function openForm(id = null) {
  const form = $('#project-form'); form.reset();
  const p = id ? projects.find(p => p.id === id) : null;
  form.elements.ssh_profile_id.innerHTML = '<option value="">Индивидуальное подключение</option>' + sshProfiles.map(profile => `<option value="${escape(profile.id)}">${escape(profile.name)}</option>`).join('');
  form.elements.git_profile_id.innerHTML = gitProfileOptions();
  $('#project-form-title').textContent = p ? 'Настройки проекта' : 'Новый проект';
  $('#individual-ssh-fields').open = Boolean(p?.ssh_host && !p.ssh_profile_id);
  form.elements.id.value = id || '';
  if (p) {
    Object.entries(p).forEach(([key, value]) => {
      const field = form.elements.namedItem(key);
      if (!field || ['ssh_password','disk_token'].includes(key)) return;
      if (field.type === 'checkbox') field.checked = Boolean(value); else field.value = value ?? '';
    });
  }
  $('#ssh-password-hint').textContent = p?.has_password ? 'Пароль сохранён. Оставьте пустым, чтобы не менять.' : 'Хранится зашифрованным на вашем ПК.';
  $('#disk-token-hint').textContent = p?.has_disk_token ? 'Ключ сохранён. Оставьте пустым, чтобы не менять.' : 'Ключ создаётся при установке PHP-пробы из карточки проекта.';
  updateProfileFields(); updateDiskFields();
  $('#project-form-error').textContent = '';
  $('#project-form-dialog').showModal();
}
function formProject(form) {
  const data = Object.fromEntries(new FormData(form)); delete data.id;
  ['enabled','notify','clear_password','git_monitor','auto_sync','auto_push'].forEach(key => data[key] = form.elements[key].checked);
  ['interval','ssh_port','disk_threshold'].forEach(key => { if (data[key] !== undefined) data[key] = Number(data[key]); });
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
  $('#detail-content').innerHTML = `<div class="detail-topline"><div>${badge(p)}<p class="value-caption" style="margin-top:9px">${p.url ? `<a href="${escape(p.url)}" target="_blank" rel="noopener noreferrer">${escape(p.url)}</a>` : 'Адрес сайта не указан'}<br>Последняя проверка: ${fullDate(state.checked_at)}</p></div><button class="button primary" data-detail-action="check">${icon('refresh')}Проверить сейчас</button></div><div class="detail-grid"><section class="panel"><h3>ДОСТУПНОСТЬ САЙТА</h3><div class="big-value ${web.ok ? 'mint' : ''}">${web.code ? `HTTP ${web.code}` : web.ok === false ? 'Недоступен' : '—'}</div><p class="value-caption">${web.latency != null ? 'Ответ за ' + web.latency + ' мс' : p.url ? 'Ожидаем результат проверки' : 'Добавьте адрес сайта в настройках'}<br>Интервал: ${p.interval < 60 ? p.interval + ' сек.' : Math.round(p.interval / 60) + ' мин.'}</p></section><section class="panel"><h3>СВОБОДНОЕ МЕСТО НА СЕРВЕРЕ</h3><div class="big-value">${bytes(disk.free)} <span style="font-size:13px;color:var(--muted)">${disk.total ? 'из ' + bytes(disk.total) : ''}</span></div><p class="value-caption">${disk.free_percent != null ? 'Свободно ' + disk.free_percent + '% · ' + escape(disk.source) : escape(disk.error || 'Нет данных')}<br>Порог уведомления: ${p.disk_threshold}% свободного места</p>${disk.free_percent != null ? `<div class="disk-bar"><span style="width:${Math.max(0,Math.min(100,disk.free_percent))}%"></span></div>` : ''}</section></div>${state.issues?.length ? `<div class="issue-list">${state.issues.map(i=>`<p>${escape(i)}</p>`).join('')}</div>` : ''}<div class="detail-connection"><div><h3>SSH-подключение</h3><p>${p.ssh_host ? escape(`${p.ssh_user}@${p.ssh_host}:${p.ssh_port} · ${p.ssh_path}`) : 'Не настроено'}</p><p>${p.ssh_fingerprint ? 'Отпечаток подтверждён · ' + escape(p.ssh_fingerprint) : 'Отпечаток ещё не подтверждён'}</p></div><div class="detail-actions" style="margin:0">${p.ssh_host ? `<button class="button secondary small" data-detail-action="fingerprint">${icon('shield')}${p.ssh_fingerprint ? 'Проверить ключ' : 'Подтвердить ключ'}</button><button class="button secondary small" data-detail-action="ssh-test">${icon('terminal')}Тест SSH</button><button class="button secondary small" data-detail-action="browse">${icon('folder')}Выбрать папку</button>` : '<button class="button secondary small" data-detail-action="edit">Настроить</button>'}</div></div><div class="detail-connection"><div><h3>Git на сервере</h3><p>${git.connected ? escape(git.branch + ' · ' + git.remote) : escape(git.error || 'Репозиторий не проверен или ещё не подключён')}</p><p>${p.auto_sync ? 'Автоматические коммиты включены' : 'Изменения принимаются вручную'}${p.auto_sync && p.auto_push ? ' · push в текущую ветку' : ''}</p></div><button class="button secondary small" data-detail-action="git">${icon('git')}Открыть Git</button></div><div class="detail-actions">${p.ssh_host ? `<button class="button secondary small" data-detail-action="deploy">${icon('disk')}Установить PHP-пробу</button>` : ''}<button class="button secondary small" data-detail-action="pause">${p.enabled ? 'Приостановить мониторинг' : 'Включить мониторинг'}</button><button class="danger-link" data-detail-action="delete">Удалить проект</button></div><p class="hint">HTTP-проба необязательна: по SSH место проверяется без установки файлов. Для пробы сервер должен выполнять PHP 8+ и передавать заголовок Authorization; HTTPS URL укажите в настройках.</p>`;
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
  if (action === 'ssh-test') { const result = await api(`/projects/${id}/ssh/test`, {method:'POST'}); toast(result.message); if (result.directory?.ok === false) toast(result.directory.error, true); }
  if (action === 'browse') { openForm(id); return openRemoteBrowser(); }
  if (action === 'fingerprint') {
    const key = await api(`/projects/${id}/ssh/fingerprint`);
    const accepted = await confirmAction('Проверить ключ SSH-сервера', `Сверьте этот отпечаток с ключом сервера через панель хостинга или доверенный канал.\nСервер: ${key.host}:${key.port}\nАлгоритм: ${key.algorithm}\n${p.ssh_profile_id ? 'Подтверждение применяется ко всем проектам этого SSH-профиля.' : 'Подтверждение закрепит ключ для следующих подключений.'}`, key.fingerprint);
    if (accepted) { await api(`/projects/${id}/ssh/trust`, {method:'POST', body:{fingerprint:key.fingerprint}}); await loadProjects(); toast('Ключ сервера подтверждён'); }
  }
  if (action === 'deploy') {
    if (!p.ssh_path) throw new Error('Сначала выберите папку проекта через SSH-проводник.');
    probeProjectId = id;
    $('#probe-target').textContent = `Файл будет создан в ${p.ssh_path}/.devlin-disk.php. Укажите его адрес на сайте.`;
    let suggested = p.disk_url || '';
    if (!suggested && p.url?.startsWith('https://')) suggested = new URL('.devlin-disk.php', p.url).href;
    $('#probe-url').value = suggested; $('#probe-setup-error').textContent = '';
    $('#probe-setup-dialog').showModal();
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
  gitHistoryRequest++;
  const id = selectedId, p = getProject(); gitData = null;
  if (!p.ssh_host) { $('#detail-content').innerHTML = '<div class="git-info">Для работы с файлами сервера сначала настройте SSH и подтвердите ключ сервера в разделе «Обзор».</div><button class="button secondary" data-detail-action="edit">Настроить SSH</button>'; return; }
  $('#detail-content').innerHTML = '<div class="loading">Читаем состояние репозитория на сервере…</div>';
  try {
    const data = await api(`/projects/${id}/git`);
    if (id !== selectedId || activeTab !== 'git') return;
    gitData = data; gitHistory = null; renderGit(); mountGitIgnore(); await loadGitHistory(true);
  } catch (error) {
    if (id === selectedId && activeTab === 'git') $('#detail-content').innerHTML = `<div class="error-panel">${escape(error.message)}<br><button class="button secondary" data-detail-action="edit">Настроить подключение</button> <button class="button secondary" data-detail-action="git">Повторить</button></div>`;
    if (id === selectedId && activeTab === 'git') mountGitIgnore();
  }
}
function renderGit() {
  const g = gitData, p = getProject();
  $('#detail-content').innerHTML = `<div class="git-heading"><div><h3>${g.connected ? escape(g.branch) : 'Подключите репозиторий'}</h3><p>${g.connected ? escape(g.remote || 'origin не настроен') + (g.head ? ' · ' + escape(g.head) : '') : 'Файлы проекта уже на сервере. Выберите их для первого коммита.'}</p></div><div class="detail-actions" style="margin:0"><button class="button secondary small" data-git-action="refresh">${icon('refresh')}Обновить</button>${g.connected ? `<button class="button secondary small" data-git-action="pull">${icon('git')}Загрузить из Git</button><button class="button secondary small" data-git-action="push">${icon('send')}Отправить коммиты</button>` : ''}</div></div><div class="git-info">${g.connected ? 'Отметьте файлы, проверьте diff и создайте коммит из текущих файлов на сервере. Push отправит его в текущую ветку origin, в том числе в GitLab.' : 'Создайте пустой репозиторий в GitLab или GitHub, затем выберите его через «Подключить репозиторий». Для первого импорта удалённый репозиторий должен быть пустым. Git-профиль либо доступы сервера должны разрешать запись.'}</div>${g.staged ? '<div class="git-info git-warning">В индексе уже есть staged-изменения. Завершите или отмените их на сервере: приложение не будет включать их в свой коммит.</div>' : ''}${g.files.some(f=>!f.allowed) ? '<div class="git-info git-warning">Потенциально секретные файлы и зависимости исключены из синхронизации. Проверяйте содержимое разрешённых файлов — фильтр имени не обнаруживает все секреты.</div>' : ''}${!g.connected ? `<div class="fields"><label>URL репозитория<input id="git-remote" placeholder="git@gitlab.com:group/project.git"></label><label>Начальная ветка<input id="git-branch" value="${escape(p.git_branch || 'main')}" placeholder="main"></label></div>` : ''}${g.files.length ? `<div class="file-list"><div class="file-row"><label><input type="checkbox" id="select-all-files" checked><span>Выбрать все допустимые файлы (${g.files.filter(f=>f.allowed).length})</span></label></div>${g.files.map(f=>`<div class="file-row ${f.allowed ? '' : 'blocked'}"><label><input class="git-file" type="checkbox" value="${escape(f.path)}" ${f.allowed ? 'checked' : 'disabled'}><span class="file-status">${escape(f.status)}</span><span class="file-name" title="${escape(f.path)}">${escape(f.path)}</span></label>${f.allowed ? `<button class="view-diff" data-file-diff="${escape(f.path)}">Посмотреть →</button>` : '<span class="badge warning">Исключён</span>'}</div>`).join('')}</div><div class="git-controls"><label>Сообщение коммита<input id="commit-message" value="${g.head ? 'Обновление файлов проекта' : 'Initial project import'}" maxlength="500" required></label><button class="button primary" data-git-action="commit" ${g.staged ? 'disabled' : ''}>${icon('git')}${g.connected ? 'Создать коммит' : 'Подключить и создать коммит'}</button></div><p class="hint">Коммит будет создан на сервере. Отправка выполняется отдельно из списка неотправленных коммитов.</p><pre id="file-diff" class="diff-view" hidden></pre>` : '<div class="events-empty">Рабочая директория чистая. Изменений для коммита нет.</div>'}<p class="hint">Без force push, reset или перезаписи файлов. «Загрузить из Git» использует pull --ff-only и требует чистую рабочую директорию. Изменившийся после просмотра набор файлов потребует нового подтверждения.</p>`;
  $('#detail-content').insertAdjacentHTML('afterbegin', `<section class="git-profile-summary"><p class="hint">${p.git_profile_id ? 'Git-профиль: ' + escape(p.git_profile_name || 'Выбран') + ' · HTTPS авторизация с ПК' : 'Авторизация Git настроена на SSH-сервере'}<br>${g.connected ? 'Локальный Git обнаружен' : 'Локальный Git не создан'} · ${g.remote ? 'origin настроен' : 'репозиторий сервиса не подключён'}</p><div class="detail-actions"><button class="button secondary" data-git-action="check">Проверить репозиторий</button><button class="button secondary" data-git-action="attach">${g.remote ? 'Настроить подключение репозитория' : 'Подключить репозиторий'}</button></div></section>`);
  $('#detail-content').insertAdjacentHTML('beforeend', '<section id="git-history-panel" class="git-history-panel"><p class="hint">Загружаем коммиты…</p></section>');
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
  if (action === 'ignore') return openGitIgnore();
  if (action === 'refresh') return loadGit();
  if (action === 'fetch-commits') return loadGitHistory(true);
  const p = getProject(), id = p.id;
  if (action === 'check') { const result = await api(`/projects/${id}/git/check`, {method:'POST'}); toast(result.message, !result.ok); return; }
  if (action === 'attach') return openGitConnect();
  if (action === 'push') {
    await loadGitHistory(true);
    const h = gitHistory;
    if (!h || h.error || !h.outgoing_known) throw new Error(h?.error || 'Обновите список коммитов с origin перед отправкой.');
    if (h.behind) throw new Error('На origin есть новые коммиты. Сначала обновите чистый проект через pull или согласуйте историю вручную.');
    if (!h.ahead) { toast('Неотправленных коммитов нет'); return; }
    if (!await confirmAction('Отправить неотправленные коммиты?', `Ветка: ${h.branch}\n${h.remote}\nБудут отправлены все ${h.ahead} неотправленных коммитов этой ветки без force push.`, h.outgoing.map(c => `${c.short_hash} · ${c.subject} · ${c.author}`).join('\n') + (h.ahead > h.outgoing.length ? '\nПоказаны первые 50 коммитов.' : ''))) return;
    const result = await api(`/projects/${id}/git/push`, {method:'POST',body:{confirm:true,expected_head:h.head,expected_branch:h.branch,expected_remote:h.remote}});
    toast(result.message); await loadGit(); return;
  }
  if (action === 'pull') {
    if (!await confirmAction(action === 'pull' ? 'Обновить файлы сервера из Git?' : 'Отправить коммиты?', action === 'pull' ? `В ${p.ssh_path} будет выполнен pull --ff-only из origin/${gitData.branch}. Операция остановится при локальных изменениях или расхождении истории.` : `Коммиты текущей ветки ${gitData.branch} будут отправлены в origin без force push.`)) return;
    const result = await api(`/projects/${id}/git/${action}`, {method:'POST', body:{confirm:true}}); toast(result.message); await loadGit(); return;
  }
  if (action === 'commit') {
    const files = [...new Set($$('.git-file:checked:not(:disabled)', $('#detail-content')).map(box => box.value))], message = $('#commit-message').value.trim(), push = false;
    if (!files.length || !message) throw new Error('Выберите файлы и укажите сообщение коммита');
    const data = {snapshot:gitData.snapshot, files, message, push};
    if (!gitData.connected) { data.remote = $('#git-remote').value.trim(); data.branch = $('#git-branch').value.trim(); if (!data.remote || !data.branch) throw new Error('Укажите URL репозитория и ветку'); }
    if (!await confirmAction(gitData.connected ? 'Принять изменения сервера в Git?' : 'Создать репозиторий на сервере?', `${files.length} файлов будут добавлены в коммит «${message}».\n${push ? 'Коммит будет отправлен в выбранную ветку origin.' : 'Коммит останется только на сервере.'}\nПроверьте, что в файлах нет паролей и других секретов.`, files.join('\n'))) return;
    const result = await api(`/projects/${id}/git/${gitData.connected ? 'sync' : 'init'}`, {method:'POST', body:data});
    toast(result.warning || `Коммит ${result.commit} ${result.pushed ? 'отправлен в origin' : 'создан на сервере'}`, Boolean(result.warning));
    await loadGit(); $('#git-history-panel')?.scrollIntoView({behavior:'smooth',block:'start'});
  }
}

async function loadGitHistory(refresh = false) {
  const id = selectedId, sequence = ++gitHistoryRequest;
  const current = () => sequence === gitHistoryRequest && selectedId === id && activeTab === 'git' && $('#detail-dialog').open && $('#git-history-panel');
  try {
    const local = await api(`/projects/${id}/git/commits`);
    if (!current()) return;
    gitHistory = local; renderGitHistory();
    if (refresh && local.remote && local.branch !== '(detached HEAD)') {
      $('#git-history-status').textContent = 'Обновляем историю с origin. Файлы проекта не изменяются…';
      const data = await api(`/projects/${id}/git/fetch`, {method:'POST'});
      if (!current()) return;
      gitHistory = data; renderGitHistory();
    }
  } catch (error) {
    if (!current()) return;
    gitHistory = {...(gitHistory || {commits:[],outgoing:[]}), error:error.message}; renderGitHistory();
  }
}
function renderGitHistory() {
  const h = gitHistory, panel = $('#git-history-panel'); if (!panel || !h) return;
  const entries = list => list.map(c => `<article class="commit-row"><code>${escape(c.short_hash)}</code><div><strong>${escape(c.subject)}</strong><p>${escape(c.author)} &lt;${escape(c.email)}&gt; · ${fullDate(c.date)}</p></div></article>`).join('');
  panel.innerHTML = `<div class="section-heading"><h3>Коммиты на отправку${h.outgoing_known ? ' · ' + h.ahead : ''}</h3><button class="button secondary small" data-git-action="fetch-commits">Обновить с origin</button></div><p id="git-history-status" class="hint">${escape(h.message || '')}</p>${h.author ? `<p class="hint">Автор следующего коммита: <strong>${escape(h.author.name)} &lt;${escape(h.author.email)}&gt;</strong> · ${escape(h.author.source)}</p>` : ''}${h.error ? `<p class="git-info git-warning">${escape(h.error)}</p>` : ''}${h.behind ? `<p class="git-info git-warning">На origin ${h.behind} коммитов, которых нет на сервере. При расхождении истории отправка остановится; рабочие файлы сохраняются.</p>` : ''}${h.outgoing_known ? h.outgoing.length ? `<div id="outgoing-commits" class="commit-list">${entries(h.outgoing)}</div><p class="hint">Отправятся все неотправленные коммиты текущей ветки. ${h.ahead > h.outgoing.length ? 'Показаны последние 50.' : ''}</p><button class="button primary" data-git-action="push" ${h.error || h.behind ? 'disabled' : ''}>Отправить коммиты (${h.ahead})</button>` : '<p class="hint">Неотправленных коммитов нет.</p>' : '<p class="hint">История на сервере доступна ниже. Для точного списка на отправку обновите origin.</p>'}<details class="commit-history" ${h.outgoing_known ? '' : 'open'}><summary>История текущей ветки · последние ${h.commits.length}</summary><div id="local-commits" class="commit-list">${entries(h.commits) || '<p class="hint">Коммитов пока нет.</p>'}</div></details>`;
}

let ignoreEditor = null, ignoreRequest = 0, ignoreFolder = null, ignoreChoices = new Map();
function mountGitIgnore() {
  const id = selectedId;
  $('#detail-content').insertAdjacentHTML('afterbegin', '<section class="ignore-summary"><div><h3>.gitignore</h3><p id="gitignore-status" class="hint">Проверяем файл на сервере…</p></div><button class="button secondary" data-git-action="ignore">Открыть редактор</button></section>');
  api(`/projects/${id}/git/ignore`).then(data => {
    if (selectedId !== id || activeTab !== 'git' || !$('#gitignore-status')) return;
    $('#gitignore-status').textContent = data.exists ? 'Файл существует. Можно изменить правила или выбрать новые пути.' : 'Файл отсутствует. Создайте его и выберите, что исключить из Git.';
    $('[data-git-action="ignore"]').textContent = data.exists ? 'Редактировать .gitignore' : 'Создать .gitignore';
  }).catch(error => {
    if (selectedId === id && $('#gitignore-status')) $('#gitignore-status').textContent = error.message;
  });
}
async function openGitIgnore() {
  const id = selectedId, sequence = ++ignoreRequest;
  ignoreEditor = null;
  $('#gitignore-error').textContent = ''; $('#gitignore-content').value = '';
  $('#gitignore-info').textContent = 'Читаем .gitignore с сервера…';
  ['#gitignore-save','#gitignore-pick','#gitignore-content'].forEach(q => $(q).disabled = true);
  $('#gitignore-dialog').showModal();
  try {
    const data = await api(`/projects/${id}/git/ignore`);
    if (sequence !== ignoreRequest || !$('#gitignore-dialog').open) return;
    ignoreEditor = {id, ...data}; $('#gitignore-content').value = data.content;
    $('#gitignore-info').textContent = `${getProject().ssh_path}/.gitignore · ${data.exists ? 'Редактирование существующего файла' : 'Файл отсутствует — будет создан при сохранении'}`;
    ['#gitignore-save','#gitignore-pick','#gitignore-content'].forEach(q => $(q).disabled = false);
  } catch (error) {
    if (sequence === ignoreRequest && $('#gitignore-dialog').open) $('#gitignore-error').textContent = error.message;
  }
}
$('#gitignore-dialog').addEventListener('close', () => { ignoreRequest++; ignoreEditor = null; });
$('#gitignore-form').addEventListener('submit', event => {
  event.preventDefault();
  busy($('#gitignore-save'), async () => {
    const editor = ignoreEditor; if (!editor) return;
    const content = $('#gitignore-content').value;
    $('#gitignore-error').textContent = '';
    if (!await confirmAction(editor.exists ? 'Сохранить изменения .gitignore?' : 'Создать .gitignore?', 'Правила будут записаны в корень проекта на сервере. Уже отслеживаемые файлы останутся в Git.', content || '(пустой файл)')) return;
    try {
      const result = await api(`/projects/${editor.id}/git/ignore`, {method:'PUT',body:{content,snapshot:editor.snapshot,confirm:true}});
      $('#gitignore-dialog').close(); toast(result.message);
      if (selectedId === editor.id && activeTab === 'git') await loadGit();
    } catch (error) { $('#gitignore-error').textContent = error.message; }
  });
});
let ignoreBrowseRequest = 0;
async function loadIgnoreFolder(path = '') {
  const id = ignoreEditor?.id, sequence = ++ignoreBrowseRequest; if (!id) return;
  ignoreFolder = null; $('#gitignore-files').textContent = 'Читаем каталог…';
  $('#gitignore-picker-error').textContent = ''; $('#gitignore-picker-note').textContent = '';
  $('#gitignore-search').value = ''; $('#gitignore-up').disabled = true;
  try {
    const data = await api(`/projects/${id}/git/ignore/files?path=${encodeURIComponent(path)}`);
    if (sequence !== ignoreBrowseRequest || !$('#gitignore-picker-dialog').open) return;
    ignoreFolder = data; $('#gitignore-path').textContent = '/' + data.path;
    $('#gitignore-up').disabled = data.parent === null;
    $('#gitignore-picker-note').textContent = data.truncated ? 'Показана часть каталога (до 1000 элементов). Остальные правила можно вписать в редактор вручную.' : 'Отметьте файл или всю папку. Кнопка «Открыть» позволяет выбрать отдельные элементы внутри папки. По ссылкам проводник не переходит.';
    renderIgnoreFolder();
  } catch (error) {
    if (sequence !== ignoreBrowseRequest || !$('#gitignore-picker-dialog').open) return;
    $('#gitignore-files').textContent = ''; $('#gitignore-picker-error').textContent = error.message;
  }
}
function renderIgnoreFolder() {
  if (!ignoreFolder) return;
  const term = $('#gitignore-search').value.toLocaleLowerCase();
  const entries = ignoreFolder.entries.filter(e => e.name.toLocaleLowerCase().includes(term));
  $('#gitignore-files').innerHTML = entries.map(e => `<div class="ignore-file-row"><label><input type="checkbox" data-ignore-path="${escape(e.path)}" ${ignoreChoices.has(e.path) ? 'checked' : ''}><span>${e.directory ? icon('folder') : icon('git')}</span><span>${escape(e.name)}${e.directory ? '/' : ''}${e.symlink ? ' (ссылка)' : ''}</span></label>${e.directory ? `<button type="button" class="button secondary small" data-ignore-open="${escape(e.path)}">Открыть</button>` : ''}</div>`).join('') || '<p class="hint">Нет файлов и папок для выбора.</p>';
}
$('#gitignore-pick').onclick = () => {
  ignoreChoices = new Map(); $('#gitignore-selection').textContent = 'Выбрано: 0'; $('#gitignore-add').disabled = true;
  $('#gitignore-picker-dialog').showModal(); loadIgnoreFolder();
};
$('#gitignore-picker-dialog').addEventListener('close', () => { ignoreBrowseRequest++; });
$('#gitignore-search').oninput = renderIgnoreFolder;
$('#gitignore-root').onclick = () => loadIgnoreFolder();
$('#gitignore-up').onclick = () => { if (ignoreFolder?.parent !== null) loadIgnoreFolder(ignoreFolder?.parent || ''); };
$('#gitignore-files').onclick = event => { const button = event.target.closest('[data-ignore-open]'); if (button) loadIgnoreFolder(button.dataset.ignoreOpen); };
$('#gitignore-files').onchange = event => {
  const path = event.target.dataset.ignorePath, entry = ignoreFolder?.entries.find(e => e.path === path); if (!entry) return;
  if (event.target.checked) ignoreChoices.set(path, entry.rule); else ignoreChoices.delete(path);
  $('#gitignore-selection').textContent = 'Выбрано: ' + ignoreChoices.size; $('#gitignore-add').disabled = !ignoreChoices.size;
};
$('#gitignore-add').onclick = () => {
  const input = $('#gitignore-content'), existing = new Set(input.value.split(/\r?\n/));
  const rules = [...ignoreChoices.values()].filter(rule => !existing.has(rule));
  if (rules.length) input.value += (input.value && !input.value.endsWith('\n') ? '\n' : '') + rules.join('\n') + '\n';
  $('#gitignore-picker-dialog').close(); input.focus(); toast(`Добавлено правил: ${rules.length}. Нажмите «Сохранить .gitignore», чтобы записать их на сервер.`);
};

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
  await loadProfiles();
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
  const info = await api('/app-info');
  $('#app-version').textContent = `v${info.version}`;
  $('#app-build').textContent = info.commit ? `${info.commit.slice(0, 8)}${info.dirty ? ' · локальные изменения' : ''}` : 'Запуск из исходников';
  $('#app-version').title = `Запущено: ${info.executable}${info.built_at ? '\nСборка: ' + info.built_at : ''}`;
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
function updateDiskFields() {
  const form = $('#project-form'), http = form.elements.disk_mode.value === 'http';
  $('#http-probe-fields').hidden = !http;
  $('#disk-setup-hint').textContent = http ? 'Для PHP-пробы нужны адрес файла и ключ. Если пробы ещё нет, сохраните проект, затем установите её из раздела «Обзор».' : form.elements.disk_mode.value === 'ssh' ? 'Проверка через SSH не требует PHP-файла, HTTPS URL и ключа пробы.' : 'Проверка диска выключена.';
}
function updateProfileFields() {
  const form = $('#project-form'), profile = sshProfiles.find(p => p.id === form.elements.ssh_profile_id.value);
  ['ssh_host','ssh_port','ssh_user','ssh_key','ssh_password','clear_password'].forEach(k => form.elements[k].disabled = Boolean(profile));
  $('#individual-ssh-fields').hidden = Boolean(profile);
  $('#project-ssh-status').textContent = profile ? `${profile.ssh_user}@${profile.ssh_host}:${profile.ssh_port} · ${profile.ssh_fingerprint ? 'ключ подтверждён, папку можно выбрать через проводник' : 'сначала подтвердите ключ в разделе «SSH-профили»'}` : 'Выберите общий SSH-профиль. Он настраивается и проверяется в отдельном разделе панели.';
  if (profile) {
    ['ssh_host','ssh_port','ssh_user','ssh_key'].forEach(k => form.elements[k].value = profile[k] || '');
    form.elements.ssh_password.value = ''; form.elements.clear_password.checked = false;
    $('#ssh-password-hint').textContent = 'Авторизация хранится в общем SSH-профиле. Изменяйте её в настройках профиля.';
  } else if (projects.find(p => p.id === form.elements.id.value)?.ssh_profile_id) {
    $('#ssh-password-hint').textContent = 'Для индивидуального подключения укажите пароль или используйте ключ/SSH-агент.';
  }
}
$('#project-form [name=disk_mode]').addEventListener('change', updateDiskFields);
$('#project-form [name=ssh_profile_id]').addEventListener('change', updateProfileFields);

async function openRemoteBrowser() {
  const form = $('#project-form'), id = form.elements.id.value, saved = projects.find(p => p.id === id);
  const profileId = form.elements.ssh_profile_id.value, profile = sshProfiles.find(p => p.id === profileId);
  if (profileId) {
    if (!profile?.ssh_fingerprint) throw new Error('Подтвердите ключ выбранного профиля в разделе «SSH-профили», затем откройте проводник.');
    remoteEndpoint = `/ssh-profiles/${profileId}/directories`;
  } else {
    if (!saved?.ssh_host) throw new Error('Выберите SSH-профиль или сохраните индивидуальные SSH-настройки проекта.');
    if (!saved.ssh_fingerprint) throw new Error('Сначала подтвердите ключ SSH-сервера в разделе «Обзор».');
    if (saved.ssh_profile_id || ['ssh_host','ssh_user','ssh_key'].some(k => form.elements[k].value.trim() !== (saved[k] || '')) ||
        Number(form.elements.ssh_port.value) !== saved.ssh_port || form.elements.ssh_password.value || form.elements.clear_password.checked) {
      throw new Error('Индивидуальные SSH-настройки изменены. Сначала сохраните проект.');
    }
    remoteEndpoint = `/projects/${id}/ssh/directories`;
  }
  remoteProjectId = id; remoteProfileId = profileId; $('#remote-browser-dialog').showModal(); await loadRemoteFolders('');
}
$('#ssh-browse').onclick = () => busy($('#ssh-browse'), openRemoteBrowser);
async function loadRemoteFolders(path) {
  const sequence = ++remoteRequest;
  remoteCurrent = ''; remoteParent = null;
  $('#remote-error').textContent = ''; $('#remote-list-hint').textContent = ''; $('#remote-breadcrumbs').innerHTML = '';
  $('#remote-folders').innerHTML = '<div class="loading">Подключаемся к серверу и читаем папки…</div>';
  $('#remote-path').value = path;
  ['remote-home','remote-root','remote-up','remote-select'].forEach(id => $('#' + id).disabled = true);
  $('#remote-path-form button').disabled = true;
  try {
    const data = await api(`${remoteEndpoint}?path=${encodeURIComponent(path)}`);
    if (sequence !== remoteRequest || !$('#remote-browser-dialog').open) return;
    remoteCurrent = data.path; remoteParent = data.parent; $('#remote-path').value = data.path;
    const crumbs = [{name:'/', path:'/'}]; let cumulative = '';
    data.path.split('/').filter(Boolean).forEach(name => { cumulative += '/' + name; crumbs.push({name, path:cumulative}); });
    $('#remote-breadcrumbs').innerHTML = crumbs.map(c => `<button type="button" data-remote-path="${escape(c.path)}">${escape(c.name)}</button>`).join('<span>/</span>');
    $('#remote-folders').innerHTML = data.directories.length ? data.directories.map(d => `<button type="button" class="remote-folder" data-remote-path="${escape(d.path)}">${icon('folder')}<span>${escape(d.name)}</span>${d.symlink ? '<small>ссылка</small>' : ''}</button>`).join('') : '<div class="events-empty">Вложенных папок нет. Можно выбрать текущую папку.</div>';
    $('#remote-list-hint').textContent = data.truncated ? 'Показана часть папок. Нужный путь можно ввести сверху.' : 'Файлы не показаны. Ссылки открываются по их реальному пути.';
  } catch (error) {
    if (sequence !== remoteRequest || !$('#remote-browser-dialog').open) return;
    $('#remote-error').textContent = error.message;
    $('#remote-folders').innerHTML = '<div class="events-empty">Попробуйте домашнюю папку или укажите другой путь.</div>';
  } finally {
    if (sequence === remoteRequest && $('#remote-browser-dialog').open) {
      ['remote-home','remote-root'].forEach(id => $('#' + id).disabled = false);
      $('#remote-path-form button').disabled = false; $('#remote-up').disabled = !remoteParent; $('#remote-select').disabled = !remoteCurrent;
    }
  }
}
$('#remote-browser-dialog').addEventListener('close', () => { remoteRequest++; });
$('#remote-home').onclick = () => loadRemoteFolders(''); $('#remote-root').onclick = () => loadRemoteFolders('/');
$('#remote-up').onclick = () => loadRemoteFolders(remoteParent || '/');
$('#remote-path-form').onsubmit = event => { event.preventDefault(); loadRemoteFolders($('#remote-path').value.trim()); };
['remote-folders','remote-breadcrumbs'].forEach(id => $('#' + id).onclick = event => { const button = event.target.closest('[data-remote-path]'); if (button) loadRemoteFolders(button.dataset.remotePath); });
$('#remote-select').onclick = () => {
  if (!remoteCurrent || $('#project-form').elements.id.value !== remoteProjectId || $('#project-form').elements.ssh_profile_id.value !== remoteProfileId) return;
  $('#project-form').elements.ssh_path.value = remoteCurrent; $('#remote-browser-dialog').close(); toast('Папка выбрана. Нажмите «Сохранить проект», чтобы применить путь.');
};

$('#probe-setup-form').onsubmit = async event => {
  event.preventDefault(); const button = $('#probe-setup-form button[type=submit]'); if (button.disabled) return;
  const url = $('#probe-url').value.trim();
  if (!url.startsWith('https://')) { $('#probe-setup-error').textContent = 'Укажите HTTPS URL файла пробы.'; return; }
  button.disabled = true; $('#probe-setup-error').textContent = '';
  try {
    const result = await api(`/projects/${probeProjectId}/disk/deploy`, {method:'POST', body:{confirm:true, disk_url:url}});
    $('#probe-setup-dialog').close(); await loadProjects();
    await confirmAction('PHP-проба установлена и настроена', 'HTTPS URL и ключ сохранены. Проверка через PHP-пробу включена.\nСкопируйте ключ сейчас, если он нужен для внешних проверок.', result.token);
    toast('Проба настроена. Нажмите «Проверить сейчас».');
  } catch (error) { $('#probe-setup-error').textContent = error.message; }
  finally { button.disabled = false; }
};

async function loadProfiles() {
  sshProfiles = await api('/ssh-profiles');
  $('#ssh-profile-list').innerHTML = sshProfiles.length ? sshProfiles.map(p => `<article class="ssh-profile-row"><h3>${escape(p.name)}</h3><p>${escape(p.ssh_user)}@${escape(p.ssh_host)}:${p.ssh_port} · проектов: ${p.project_count}</p><p>${p.ssh_fingerprint ? 'Ключ подтверждён' : 'Ключ ещё не подтверждён'}</p><div class="detail-actions"><button class="button secondary small" data-profile-action="edit" data-profile-id="${p.id}">Настроить</button><button class="button secondary small" data-profile-action="trust" data-profile-id="${p.id}">Подтвердить ключ</button><button class="button secondary small" data-profile-action="test" data-profile-id="${p.id}">Тест SSH</button><button class="danger-link" data-profile-action="delete" data-profile-id="${p.id}">Удалить</button></div></article>`).join('') : '<p class="hint">Добавьте профиль, затем выберите его в проектах.</p>';
}
function openProfile(id = '') {
  const form = $('#ssh-profile-form'), profile = sshProfiles.find(p => p.id === id); form.reset();
  if (profile) Object.entries(profile).forEach(([key, value]) => { const field = form.elements.namedItem(key); if (field && field.type !== 'checkbox') field.value = value; });
  $('#ssh-profile-error').textContent = ''; profileInfo(profile); $('#ssh-profile-dialog').showModal();
}
function profileInfo(profile) {
  $('#ssh-profile-info').textContent = !profile ? 'Сохраните профиль, затем подтвердите ключ сервера и проверьте SSH.' : profile.ssh_fingerprint ? 'Ключ подтверждён для всех проектов этого профиля: ' + profile.ssh_fingerprint : 'Профиль сохранён. Подтвердите ключ сервера и проверьте SSH.';
  $('#ssh-profile-trust').disabled = !profile; $('#ssh-profile-test').disabled = !profile;
}
$('#new-ssh-profile').onclick = () => openProfile(); $('#project-new-profile').onclick = () => openProfile();
$('#ssh-profile-form').onsubmit = async event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('button[type=submit]', form), async () => {
    $('#ssh-profile-error').textContent = ''; const id = form.elements.id.value, data = Object.fromEntries(new FormData(form)); delete data.id;
    data.ssh_port = Number(data.ssh_port); data.clear_password = form.elements.clear_password.checked; if (!data.ssh_password) delete data.ssh_password;
    try {
      const profile = await api(id ? '/ssh-profiles/' + id : '/ssh-profiles', {method:id ? 'PUT' : 'POST', body:data});
      form.elements.id.value = profile.id; form.elements.ssh_password.value = ''; form.elements.clear_password.checked = false;
      await loadProfiles(); await loadProjects(); profileInfo(profile);
      if ($('#project-form-dialog').open) {
        const select = $('#project-form').elements.ssh_profile_id, selected = id ? select.value : profile.id;
        select.innerHTML = '<option value="">Индивидуальное подключение</option>' + sshProfiles.map(p => `<option value="${p.id}">${escape(p.name)}</option>`).join('');
        select.value = selected; updateProfileFields();
      }
      toast('SSH-профиль сохранён');
    } catch (error) { $('#ssh-profile-error').textContent = error.message; }
  });
};
async function profileAction(action, id) {
  if (!id) throw new Error('Сначала сохраните SSH-профиль.');
  if (action === 'edit') return openProfile(id);
  if (action === 'trust') {
    const key = await api(`/ssh-profiles/${id}/fingerprint`);
    if (!await confirmAction('Подтвердить ключ SSH-профиля', `Сверьте отпечаток через панель хостинга или доверенный канал.\nСервер: ${key.host}:${key.port}\nАлгоритм: ${key.algorithm}\nПодтверждение применяется ко всем проектам этого профиля.`, key.fingerprint)) return;
    await api(`/ssh-profiles/${id}/trust`, {method:'POST', body:{fingerprint:key.fingerprint}}); toast('Ключ SSH-профиля подтверждён');
  }
  if (action === 'test') toast((await api(`/ssh-profiles/${id}/test`, {method:'POST'})).message);
  if (action === 'delete') {
    if (!await confirmAction('Удалить SSH-профиль?', 'Удаление возможно только если профиль не используется проектами.')) return;
    await api(`/ssh-profiles/${id}`, {method:'DELETE'});
  }
  await loadProfiles(); await loadProjects();
  if ($('#ssh-profile-dialog').open) profileInfo(sshProfiles.find(p => p.id === id));
}
$('#ssh-profile-list').onclick = event => { const button = event.target.closest('[data-profile-action]'); if (button) busy(button, () => profileAction(button.dataset.profileAction, button.dataset.profileId)); };
$('#ssh-profile-trust').onclick = event => busy(event.currentTarget, () => profileAction('trust', $('#ssh-profile-form').elements.id.value));
$('#ssh-profile-test').onclick = event => busy(event.currentTarget, () => profileAction('test', $('#ssh-profile-form').elements.id.value));

function gitProfileOptions() {
  return '<option value="">Авторизация Git на сервере</option>' + gitProfiles.map(p => `<option value="${p.id}">${escape(p.name)} · ${escape(p.provider)}</option>`).join('');
}
async function loadGitProfiles() {
  gitProfiles = await api('/git-profiles');
  $('#git-profile-list').innerHTML = gitProfiles.length ? gitProfiles.map(p => `<article class="ssh-profile-row"><h3>${escape(p.name)}</h3><p>${escape(p.provider)} · ${escape(p.base_url)} · проектов: ${p.project_count}</p><p>${p.account ? 'Аккаунт: ' + escape(p.account) + ' · проверено ' + fullDate(p.verified_at) : p.has_token ? 'Токен сохранён; авторизация ещё не проверена' : 'Токен не задан'}</p><div class="detail-actions"><button class="button secondary small" data-git-profile-action="edit" data-profile-id="${p.id}">Настроить</button><button class="button secondary small" data-git-profile-action="test" data-profile-id="${p.id}">Проверить авторизацию</button><button class="danger-link" data-git-profile-action="delete" data-profile-id="${p.id}">Удалить</button></div></article>`).join('') : '<p class="hint">Добавьте GitHub или GitLab, затем выберите профиль в проекте.</p>';
}
function gitTokenHelp() {
  const github = $('#git-profile-form').elements.provider.value === 'github';
  $('#git-token-help').textContent = github ? 'GitHub → Settings → Developer settings → Personal access tokens: fine-grained токен с доступом к нужным репозиториям, Contents: Read and write, Metadata: Read. Для classic токена — repo. Учитывайте разрешение организации и SSO.' : 'GitLab → Edit profile → Access tokens: токен с api и write_repository. Можно указать адрес собственного GitLab. Роль в проекте и защита ветки также ограничивают push.';
}
function gitProfileInfo(profile) {
  $('#git-profile-info').textContent = profile?.account ? 'Авторизация проверена: ' + profile.account : profile ? 'Профиль сохранён. Проверьте авторизацию.' : 'Сохраните профиль с токеном, затем проверьте авторизацию.';
  $('#git-profile-test').disabled = !profile;
}
function openGitProfile(id = '') {
  const form = $('#git-profile-form'), profile = gitProfiles.find(p => p.id === id); form.reset();
  if (profile) ['id','name','provider','base_url','git_name','git_email'].forEach(k => form.elements[k].value = profile[k] || '');
  $('#git-profile-error').textContent = ''; gitTokenHelp(); gitProfileInfo(profile); $('#git-profile-dialog').showModal();
}
$('#new-git-profile').onclick = () => openGitProfile();
$('#project-new-git-profile').onclick = () => openGitProfile();
$('#git-profile-form [name=provider]').onchange = () => {
  const form = $('#git-profile-form'); form.elements.base_url.value = form.elements.provider.value === 'github' ? 'https://github.com' : 'https://gitlab.com'; gitTokenHelp();
};
$('#git-profile-form').onsubmit = event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('button[type=submit]', form), async () => {
    $('#git-profile-error').textContent = ''; const id = form.elements.id.value, data = Object.fromEntries(new FormData(form)); delete data.id;
    data.clear_token = form.elements.clear_token.checked; if (!data.token) delete data.token;
    try {
      const profile = await api(id ? '/git-profiles/' + id : '/git-profiles', {method:id ? 'PUT' : 'POST',body:data});
      form.elements.id.value = profile.id; form.elements.token.value = ''; form.elements.clear_token.checked = false;
      await loadGitProfiles(); await loadProjects(); gitProfileInfo(profile);
      if ($('#project-form-dialog').open) { const select = $('#project-form').elements.git_profile_id, selected = id ? select.value : profile.id; select.innerHTML = gitProfileOptions(); select.value = selected; }
      toast('Git-профиль сохранён');
    } catch (error) { $('#git-profile-error').textContent = error.message; }
  });
};
async function gitProfileAction(action, id) {
  if (action === 'edit') return openGitProfile(id);
  if (!id) throw new Error('Сначала сохраните Git-профиль.');
  if (action === 'test') {
    try { toast((await api(`/git-profiles/${id}/test`, {method:'POST'})).message); }
    catch (error) { if ($('#git-profile-dialog').open) $('#git-profile-error').textContent = error.message; throw error; }
  }
  if (action === 'delete') {
    if (!await confirmAction('Удалить Git-профиль?', 'Сначала отключите профиль во всех проектах.')) return;
    await api(`/git-profiles/${id}`, {method:'DELETE'});
  }
  await loadGitProfiles(); await loadProjects();
  if ($('#git-profile-dialog').open) { $('#git-profile-error').textContent = ''; gitProfileInfo(gitProfiles.find(p => p.id === id)); }
}
$('#git-profile-list').onclick = event => { const button = event.target.closest('[data-git-profile-action]'); if (button) busy(button, () => gitProfileAction(button.dataset.gitProfileAction, button.dataset.profileId)); };
$('#git-profile-test').onclick = event => busy(event.currentTarget, () => gitProfileAction('test', $('#git-profile-form').elements.id.value));

function openGitConnect() {
  const p = getProject();
  gitConnectState = {id:p.id, profileId:p.git_profile_id || '', snapshot:gitData.snapshot, remote:gitData.remote || ''};
  $('#git-connect-remote').value = /^https:\/\//.test(gitData.remote) ? gitData.remote : '';
  $('#git-connect-branch').value = p.git_branch || (gitData.branch !== '(detached HEAD)' && gitData.branch) || 'main';
  $('#git-connect-info').textContent = `${p.ssh_path} · ${p.git_profile_name || 'Авторизация на сервере'}${gitData.remote ? '\nТекущий origin: ' + gitData.remote : '\norigin не настроен'}`;
  $('#git-connect-error').textContent = ''; $('#git-repository-picker').disabled = !p.git_profile_id;
  $('#git-connect-dialog').showModal();
}
$('#git-connect-form').onsubmit = event => {
  event.preventDefault(); busy($('button[type=submit]', event.currentTarget), async () => {
    $('#git-connect-error').textContent = '';
    const state = gitConnectState, remote = $('#git-connect-remote').value.trim(), branch = $('#git-connect-branch').value.trim();
    const replace = Boolean(state.remote && state.remote !== remote);
    if (!await confirmAction(replace ? 'Заменить origin проекта?' : 'Подключить репозиторий?', `${remote}\n${replace ? 'Текущий origin: ' + state.remote + '\nАдрес origin и его отдельный pushurl будут заменены.' : 'В выбранной серверной папке будет создан Git, если его ещё нет, и настроен origin.'}\nФайлы и история сохраняются. Загрузка файлов и отправка коммитов выполняются отдельно.`)) return;
    try {
      const result = await api(`/projects/${state.id}/git/attach`, {method:'POST',body:{remote,branch,snapshot:state.snapshot,expected_remote:state.remote,expected_git_profile_id:state.profileId,replace_remote:replace,confirm:true}});
      $('#git-connect-dialog').close(); toast(result.message); await loadProjects(); if (selectedId === state.id && activeTab === 'git') await loadGit();
    } catch (error) { $('#git-connect-error').textContent = error.message; }
  });
};
async function loadRepositoryPage(number) {
  const sequence = ++repositoryRequest, profileId = gitConnectState.profileId;
  $('#git-repository-error').textContent = ''; $('#git-repository-list').innerHTML = '<p class="hint">Загружаем репозитории…</p>';
  $('#git-repositories-prev').disabled = true; $('#git-repositories-next').disabled = true;
  try {
    const data = await api(`/git-profiles/${profileId}/repositories?page=${number}`);
    if (sequence !== repositoryRequest || !$('#git-repositories-dialog').open) return;
    repositoryPage = data.page; repositoryItems = data.repositories; $('#git-repository-search').value = ''; renderRepositories();
    $('#git-repositories-page').textContent = 'Страница ' + repositoryPage;
    $('#git-repositories-prev').disabled = repositoryPage <= 1; $('#git-repositories-next').disabled = !data.has_next;
  } catch (error) { if (sequence === repositoryRequest && $('#git-repositories-dialog').open) { $('#git-repository-error').textContent = error.message; $('#git-repository-list').innerHTML = ''; $('#git-repositories-prev').disabled = number <= 1; } }
}
function renderRepositories() {
  const query = $('#git-repository-search').value.toLowerCase();
  $('#git-repository-list').innerHTML = repositoryItems.map((r,i) => ({r,i})).filter(({r}) => r.name.toLowerCase().includes(query)).map(({r,i}) => `<button type="button" class="remote-folder" data-repository-index="${i}" ${r.archived ? 'disabled' : ''}>${icon('git')}<span>${escape(r.name)}<small>${r.private ? 'Приватный' : 'Публичный'} · ${escape(r.default_branch)}${r.archived ? ' · архив' : ''}</small></span></button>`).join('') || '<p class="hint">Репозиториев нет на этой странице. Проверьте фильтр, права токена или укажите URL вручную. Создайте репозиторий в GitHub/GitLab, если он ещё не существует.</p>';
}
$('#git-repository-picker').onclick = () => { $('#git-repositories-dialog').showModal(); loadRepositoryPage(1); };
$('#git-repositories-dialog').addEventListener('close', () => { repositoryRequest++; });
$('#git-repository-search').oninput = renderRepositories;
$('#git-repositories-prev').onclick = () => loadRepositoryPage(Math.max(1, repositoryPage-1));
$('#git-repositories-next').onclick = () => loadRepositoryPage(repositoryPage+1);
$('#git-repository-list').onclick = event => {
  const button = event.target.closest('[data-repository-index]'); if (!button) return;
  const r = repositoryItems[Number(button.dataset.repositoryIndex)]; if (!r || r.archived) return;
  $('#git-connect-remote').value = r.remote;
  if (!getProject()?.git_branch) $('#git-connect-branch').value = r.default_branch;
  $('#git-repositories-dialog').close();
};

icons();
boot().catch(error => { $('#lock-description').textContent = 'Не удалось подключиться: ' + error.message; });
setInterval(()=> {if(loggedIn) loadProjects().catch(error=>toast(error.message,true));},15000);
