"""Plain-text Telegram reports with server file metadata and an explicit timezone."""
from datetime import datetime, timedelta, timezone
from . import gitops

MOSCOW = timezone(timedelta(hours=3), 'МСК')


def local_time(value=None):
    return datetime.now(MOSCOW) if value is None else value.astimezone(MOSCOW)


def date(value):
    try:
        moment = datetime.fromtimestamp(value, timezone.utc) if isinstance(value,(int,float)) else datetime.fromisoformat(value)
        return local_time(moment).strftime('%d.%m.%Y %H:%M:%S МСК')
    except (ValueError, TypeError, OverflowError, OSError):
        return 'дата недоступна'


def size(value):
    if value is None: return 'размер недоступен'
    for unit in ('Б','КиБ','МиБ','ГиБ','ТиБ'):
        if value < 1024 or unit == 'ТиБ': return f'{value:.1f} {unit}' if unit != 'Б' else f'{value} Б'
        value /= 1024


def change_kind(item):
    flag = item.get('status','??')
    if 'D' in flag: return 'Удалён'
    if 'R' in flag: return 'Переименован'
    if flag == '??' or 'A' in flag: return 'Новый'
    return 'Изменён'


def display_path(item):
    return (item.get('display_path') or item['path']).replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def file_metadata(conn, files):
    """Batch GNU stat on Unix servers; never read file content or follow symlinks."""
    result = []
    for offset in range(0,len(files),100):
        batch = files[offset:offset+100]
        paths = ' '.join(gitops.quote_path(item) for item in batch)
        command = ('for devlin_file in '+paths+'; do stat -c \'%s %Y\' -- "$devlin_file" 2>/dev/null || printf \'?\\n\'; done')
        try:
            rows = conn.text(command,timeout=90).splitlines()
        except Exception:
            rows = []
        for index,item in enumerate(batch):
            entry = dict(item, size_bytes=None, modified_at=None)
            try:
                length,stamp = map(int,rows[index].split())
                if length >= 0: entry.update(size_bytes=length,modified_at=stamp)
            except (IndexError,ValueError): pass
            result.append(entry)
    return result


def file_messages(name, git, previous):
    files = git.get('files',[])
    before = {f['path']:f for f in previous.get('files',[])}
    changed = [f for f in files if f != before.get(f['path'])]
    if not changed and git.get('snapshot') != previous.get('snapshot'): changed = files
    if not changed: return ''
    total = git.get('total_files',len(files))
    lines = [f'📝 {name}: файлов с изменениями — {total}',f'Новые или обновившиеся с прошлой проверки: {len(changed)}']
    if git.get('truncated'): lines.append(f'Подробности доступны для первых {len(files)} из {total} изменений Git. Исключите ненужные папки через .gitignore.')
    for f in changed:
        line = f"{change_kind(f)}: {display_path(f)}"
        if f.get('old_path'): line += '\nРанее: ' + (f.get('display_old_path') or f['old_path'])
        if 'D' in f.get('status',''): line += '\nФайл отсутствует; дата и размер недоступны.'
        else: line += f"\nИзменён: {date(f.get('modified_at'))} · Размер: {size(f.get('size_bytes'))}"
        lines.append(line)
    return '\n\n'.join(lines)


def chunks(text, limit=3900):
    """Telegram limits UTF-16 units; preserve every character, including long paths."""
    chunk=[]; count=0
    for char in text:
        units=2 if ord(char)>0xffff else 1
        if count+units>limit:
            yield ''.join(chunk); chunk=[]; count=0
        chunk.append(char); count+=units
    if chunk: yield ''.join(chunk)


def daily_text(projects, states, moment, counts):
    lines=[f'📊 Devlin Desk — ежедневный отчёт\n{local_time(moment).strftime("%d.%m.%Y %H:%M")} МСК · расписание 09:00 МСК',
        f'Проектов: {len(projects)} · Мониторинг включён: {sum(bool(p["enabled"]) for p in projects)}',
        f'Активные проекты: в порядке {sum(p["enabled"] and states.get(p["id"],{}).get("status")=="ok" for p in projects)}, с проблемами {sum(p["enabled"] and states.get(p["id"],{}).get("status")=="error" for p in projects)}',
        f'События журнала за 24 часа: ошибок {counts.get("error",0)}, предупреждений {counts.get("warning",0)}, успешных {counts.get("success",0)} (по сохранённому журналу).']
    for p in projects:
        s=states.get(p['id'],{}); web=s.get('web',{}); disk=s.get('disk',{}); g=s.get('git',{}); h=s.get('git_history',{})
        part=[f'📁 {p["name"]} — '+({'ok':'в порядке','error':'внимание','unconfigured':'не настроен'}.get(s.get('status'),'нет данных') if p['enabled'] else 'мониторинг на паузе'),
            'Проверка: '+date(s.get('checked_at'))]
        if s.get('report_error'): part.append(s['report_error'])
        if p.get('url'): part += [p['url'],f'Сайт: HTTP {web.get("code") or "нет ответа"} · {web.get("latency", "—")} мс']
        if p.get('ssh_host'): part += ['SSH: '+('подключён' if s.get('ssh',{}).get('ok') else 'не подключён'), 'Папка: '+p.get('ssh_path','')]
        if disk.get('free') is not None:
            part += [f'Диск: свободно {size(disk["free"])} из {size(disk["total"])} · {disk["free_percent"]}%',f'Источник: {disk.get("source","—")}' + (' · оценка тарифа хостинга' if disk.get('estimated') else '')]
        elif p.get('disk_mode') != 'off': part += ['Диск: '+disk.get('error','нет данных')]
        if p.get('git_monitor'):
            files=g.get('files',[]); types={k:sum(change_kind(f)==k for f in files) for k in ('Новый','Изменён','Удалён','Переименован')}
            part += [f'Git: {g.get("branch") or "не подключён"} · {g.get("head") or "нет коммитов"}',f'Изменений: {g.get("total_files",len(files))} · новые {types["Новый"]}, изменены {types["Изменён"]}, удалены {types["Удалён"]}, переименованы {types["Переименован"]}' + (' (типы по первым 2000 файлам)' if g.get('truncated') else '')]
            if h.get('outgoing_known'): part += [f'Коммитов на отправку: {h["ahead"]} · новых на origin: {h["behind"]} (по локальной копии origin)']
            if h.get('error'): part += ['История Git: '+h['error']]
        part += ['Проблемы: '+('\n'.join(s.get('issues',[])) or 'не обнаружены')]
        lines.append('\n'.join(part))
    return '\n\n'.join(lines)
