"""Telegram bot checks and recipient lookup without consuming updates."""
import httpx
from .store import now


class TelegramError(Exception):
    pass


async def request(http, token, method, payload=None):
    try:
        response = await http.post('https://api.telegram.org/bot' + token + '/' + method,
                                   json=payload or {}, follow_redirects=False)
        data = response.json()
        if response.status_code == 200 and data.get('ok') is True:
            return data.get('result')
        code = data.get('error_code', response.status_code)
        if code == 401 or code == 404:
            raise TelegramError('Telegram не принял токен бота. Проверьте токен из BotFather.')
        if code == 409:
            raise TelegramError('Бот использует webhook или другой процесс getUpdates. Укажите личный Chat ID; приложение не отключает webhook и не подтверждает чужие обновления.')
        if code == 403:
            raise TelegramError('Боту запрещён доступ: он заблокирован пользователем или не имеет прав в группе/канале.')
        if code == 429:
            raise TelegramError('Telegram ограничил частоту запросов. Повторите проверку позже.')
        raise TelegramError('Telegram не подтвердил получателя или операцию. Проверьте адрес, диалог с ботом и права отправки.')
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        raise TelegramError('Telegram недоступен или вернул неверный ответ. Проверьте сеть и повторите проверку.') from exc


async def recipient(http, token, target):
    try:
        chat = await request(http, token, 'getChat', {'chat_id': target})
        if isinstance(chat, dict) and isinstance(chat.get('id'), int) and chat.get('type') in ('private','group','supergroup','channel'):
            if not target.startswith('@') or str(chat.get('username', '')).casefold() == target[1:].casefold():
                return chat['id'], chat.get('title') or chat.get('first_name') or target
    except TelegramError:
        if not target.startswith('@'):
            raise
    if not target.startswith('@'):
        raise TelegramError('Получатель не найден. Откройте диалог с ботом и отправьте /start.')
    updates = await request(http, token, 'getUpdates', {'timeout': 0, 'limit': 100})
    if not isinstance(updates, list):
        raise TelegramError('Telegram вернул неверный список сообщений.')
    for update in reversed(updates):
        if not isinstance(update, dict):
            continue
        message = update.get('message') or update.get('edited_message') or {}
        if not isinstance(message, dict):
            continue
        chat, sender = message.get('chat', {}), message.get('from', {})
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            continue
        if (chat.get('type') == 'private' and isinstance(chat.get('id'), int) and
                chat['id'] == sender.get('id') and sender.get('is_bot') is not True and
                str(sender.get('username', '')).casefold() == target[1:].casefold()):
            return chat['id'], sender.get('first_name') or target
    raise TelegramError('Личный ник не найден среди доступных сообщений бота. Отправьте /start этому боту, затем нажмите «Проверить бота». Если сообщения уже забирает другой сервис, используйте Chat ID. Бот не может первым написать по личному нику.')


async def verify(store, http):
    settings = store.settings()
    token = store.decrypt(settings.get('telegram_token', ''))
    status = {'state': 'unconfigured', 'message': 'Токен бота не указан.', 'checked_at': now()}
    if token:
        try:
            bot = await request(http, token, 'getMe')
            if not isinstance(bot, dict) or bot.get('is_bot') is not True or not isinstance(bot.get('id'), int):
                raise TelegramError('Telegram не подтвердил аккаунт бота.')
            status.update(state='token_ok', bot_username=str(bot.get('username', '')),
                          message='Токен действителен. Укажите получателя для уведомлений.')
            if settings.get('telegram_chat'):
                try:
                    chat_id, label = await recipient(http, token, settings['telegram_chat'])
                    status.update(state='ready', chat_id=chat_id, recipient_label=str(label),
                                  message='Бот доступен, получатель найден. Доставку и права записи проверяет «Отправить тест».')
                except TelegramError as exc:
                    status.update(state='recipient_error', message=str(exc))
        except TelegramError as exc:
            status.update(state='error', message=str(exc))
    with store.lock:
        latest = store.settings()
        if (latest.get('telegram_token'), latest.get('telegram_chat')) != (settings.get('telegram_token'), settings.get('telegram_chat')):
            return {'state': 'pending', 'message': 'Настройки изменились во время проверки. Проверьте бота заново.'}
        # Merge with latest settings so a concurrent PIN/settings edit is preserved.
        latest['telegram_status'] = status
        store.save_settings(latest)
    return status
