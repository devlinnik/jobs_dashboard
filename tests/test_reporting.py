import asyncio
from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from dashboard import reporting
from dashboard.app import create_app
from dashboard.models import ProjectInput
from dashboard.monitor import Monitor
from dashboard.store import Store
from tests.test_git import LocalConnection, POSIX_ONLY


@POSIX_ONLY
def test_real_server_file_metadata_and_notifications_include_paths_date_and_sizes(tmp_path):
    (tmp_path/'assets').mkdir()
    (tmp_path/'assets/new.txt').write_text('abc')
    (tmp_path/'changed.txt').write_text('larger contents')
    strange = 'odd\n$(touch BAD).txt'; (tmp_path/strange).write_text('safe')
    (tmp_path/'link').symlink_to('/nonexistent')
    files=[{'path':'assets/new.txt','status':'??'}, {'path':'changed.txt','status':' M'}, {'path':'gone.txt','status':' D'}, {'path':strange,'status':'??'}, {'path':'link','status':'??'}]
    details = reporting.file_metadata(LocalConnection(tmp_path),files)
    assert [f['size_bytes'] for f in details[:3]] == [3,15,None]
    assert details[-1]['size_bytes'] == len('/nonexistent')
    assert not (tmp_path/'BAD').exists()
    state={'files':details,'snapshot':'one','total_files':5}
    text=reporting.file_messages('Site',state,{})
    assert 'Новый: assets/new.txt' in text and 'Изменён: changed.txt' in text
    assert 'Размер: 3 Б' in text and 'Размер: 15 Б' in text and 'МСК' in text
    assert 'Удалён: gone.txt' in text and 'Файл отсутствует' in text
    assert 'odd\\n$(touch BAD).txt' in text
    assert reporting.file_messages('Site',state,state)==''
    updated={'files':[dict(details[1],size_bytes=90,modified_at=details[1]['modified_at']+10)],'snapshot':'two'}
    text=reporting.file_messages('Site',updated,state)
    assert 'Размер: 90 Б' in text and 'assets/new.txt' not in text


def test_utf16_chunks_and_truncated_totals_keep_all_text():
    text='📝файл' * 4000
    parts=list(reporting.chunks(text))
    assert len(parts)>1 and ''.join(parts)==text
    assert all(len(p.encode('utf-16-le'))//2 <=3900 for p in parts)
    message=reporting.file_messages('Site',{'files':[{'path':'a','status':'??'}],'snapshot':'one','truncated':True,'total_files':75540},{})
    assert '75540' in message and 'первых 1' in message


def project(store, pid, **options):
    value=ProjectInput(name=pid,**options).model_dump()|{'id':pid}
    store.save_project(value); return value


def test_daily_report_moscow_schedule_restart_failed_send_manual_and_read_only_checks(tmp_path):
    async def scenario():
        store=Store(tmp_path)
        store.save_settings(store.settings()|{'telegram_token':store.encrypt('token'),'telegram_chat':'123'})
        project(store,'active',ssh_host='host',ssh_user='user',ssh_path='/srv/site',auto_sync=True,auto_push=True,notify=False)
        project(store,'paused',enabled=False,notify=False)
        store.save_state('paused',{'status':'ok','checked_at':'2026-10-07T10:00:00+00:00'})
        monitor=Monitor(store,{}); deliveries=[]; calls=[]
        def server(p):
            calls.append(p)
            assert p['auto_sync'] is False and p['_daily_report']
            return {'ssh':{'ok':True},'disk':{'free':1024,'total':2048,'free_percent':50,'estimated':True,'source':'SSH'},
                'git':{'connected':True,'files':[],'branch':'main','head':'abc','total_files':0},
                'git_history':{'outgoing_known':True,'ahead':3,'behind':1}}
        monitor.server_check=server
        async def send(text): deliveries.append(text); return len(deliveries)>1
        monitor.telegram=send
        before=datetime(2026,10,8,5,59,tzinfo=timezone.utc)
        due=datetime(2026,10,8,6,0,tzinfo=timezone.utc)
        assert not monitor.report_due(before) and monitor.report_due(due)
        assert not await monitor.daily_report(moment=due)
        assert not store.settings().get('daily_report_sent_date')
        assert await monitor.daily_report(moment=due)
        assert store.settings()['daily_report_sent_date']=='2026-10-08'
        assert not monitor.report_due(due) and not await monitor.daily_report(moment=due)
        text=deliveries[-1]
        assert '09:00 МСК' in text and 'paused — мониторинг на паузе' in text
        assert 'оценка тарифа' in text and 'Коммитов на отправку: 3' in text and 'новых на origin: 1' in text
        assert len(calls)==2
        await monitor.close()
        second=Monitor(store,{})
        assert not second.report_due(due)
        assert second.report_due(datetime(2026,10,9,6,tzinfo=timezone.utc))
        second.server_check=server; second.telegram=send
        assert await second.daily_report(manual=True,moment=due)
        assert store.settings()['daily_report_sent_date']=='2026-10-08'
        store.save_settings(store.settings()|{'daily_report':False})
        assert not second.report_due(datetime(2026,10,9,7,tzinfo=timezone.utc))
        await second.close(); store.close()
    asyncio.run(scenario())


def test_monitor_notifies_changed_file_metadata_even_when_git_status_count_is_same(tmp_path):
    async def scenario():
        store=Store(tmp_path); project(store,'one',ssh_host='host',ssh_user='user',ssh_path='/srv/site')
        monitor=Monitor(store,{}); messages=[]; size=3
        def server(p): return {'ssh':{'ok':True},'git':{'files':[{'path':'index.php','status':' M','size_bytes':size,'modified_at':100}], 'snapshot':'same','total_files':1}}
        monitor.server_check=server
        async def send(text): messages.append(text); return True
        monitor.telegram=send
        await monitor.check('one'); await monitor.check('one')
        assert len(messages)==1 and 'Размер: 3 Б' in messages[0]
        size=7; await monitor.check('one')
        assert len(messages)==2 and 'Размер: 7 Б' in messages[1]
        await monitor.close(); store.close()
    asyncio.run(scenario())


def test_manual_report_api_authentication_settings_and_delivery(tmp_path):
    with TestClient(create_app(tmp_path,start_monitor=False),headers={'X-App-Request':'1'}) as client:
        assert client.post('/api/telegram/report').status_code==401
        client.post('/api/unlock',json={})
        assert client.get('/api/settings').json()['daily_report'] is True
        assert client.put('/api/settings',json={'daily_report':False}).json()['daily_report'] is False
        messages=[]
        async def send(text): messages.append(text); return True
        client.app.state.monitor.telegram=send
        assert client.post('/api/telegram/report').status_code==200
        assert 'Проектов: 0' in messages[-1]
        assert not client.app.state.store.settings().get('daily_report_sent_date')
