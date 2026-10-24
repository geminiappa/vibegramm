import http.server
import socketserver
import json
import time
import socket
import os
import base64
import threading
from urllib.parse import urlparse, parse_qs

# Render and other hosting platforms provide the listening port via PORT.
PORT = int(os.environ.get('PORT', '8000'))

# Files are kept next to this script. In production, PostgreSQL is used when
# DATABASE_URL is configured; JSON remains a local-development fallback.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
DB_FILE = os.path.join(BASE_DIR, 'vibegram_db.json')
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
DB_LOCK = threading.RLock()
os.makedirs(UPLOAD_DIR, exist_ok=True)

if DATABASE_URL:
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeError("PostgreSQL is configured but psycopg is missing. Add 'psycopg[binary]' to requirements.txt") from exc

USERS = {}          # username -> профиль пользователя или канала
MESSAGES = []       # список всех сообщений и постов
PINS = {}           # закрепленные сообщения
VERIFY_BOT_ADMIN = None  # username создателя, привязавшего бота по ID 1

def get_chat_key(u1, u2):
    a = u1.strip().lower().lstrip('@')
    b = u2.strip().lower().lstrip('@')
    if a in USERS and USERS[a].get('is_channel'):
        return f"channel:{a}"
    if b in USERS and USERS[b].get('is_channel'):
        return f"channel:{b}"
    return ":".join(sorted([a, b]))

def ensure_system_bot():
    if 'verify_bot' not in USERS:
        USERS['verify_bot'] = {
            'id': 0,
            'username': 'verify_bot',
            'name': 'Vibegram Verification',
            'bio': 'Общедоступный бот верификации Vibegram. Для входа в панель Создателя отправьте в чат свой ID (1).',
            'color': '#0abde3',
            'avatar': '',
            'privacy': 'everyone',
            'is_premium': True,
            'sticker_id': ' tg_Verify ',
            'is_verified': True,
            'verify_category': 'Официальный бот Vibegram',
            'verify_reason': 'Системный бот для выдачи галочек верификации аккаунтам и каналам.',
            'is_bot': True,
            'is_channel': False,
            'last_seen': int(time.time() * 1000)
        }

def assign_user_id(username):
    u = username.strip().lower().lstrip('@')
    # У создателя (@paveldurov или @vibegram) всегда ID = 1
    if u in ('paveldurov', 'vibegram'):
        return 1
    # Если еще ни у кого нет ID = 1 среди реальных юзеров, первому зарегистрированному тоже можно дать ID = 1
    real_users = [obj for k, obj in USERS.items() if not obj.get('is_channel') and not obj.get('is_bot')]
    if not any(obj.get('id') == 1 for obj in real_users):
        return 1
    max_id = 1000
    for obj in real_users:
        uid = int(obj.get('id') or 0)
        if uid > max_id:
            max_id = uid
    return max_id + 1

def _local_json_state():
    """Read the legacy JSON database, if one exists, for first-time migration."""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {
                        'users': data.get('users', {}),
                        'messages': data.get('messages', []),
                        'pins': data.get('pins', {}),
                        'verify_bot_admin': data.get('verify_bot_admin')
                    }
        except Exception as e:
            print("Ошибка чтения локальной JSON-БД:", e)
    return None


def _connect_postgres():
    # Supabase connection strings should include sslmode=require.
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


def load_db():
    global USERS, MESSAGES, PINS, VERIFY_BOT_ADMIN

    if DATABASE_URL:
        try:
            with _connect_postgres() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vibegram_state (
                        id SMALLINT PRIMARY KEY CHECK (id = 1),
                        data JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                row = conn.execute(
                    'SELECT data FROM vibegram_state WHERE id = 1'
                ).fetchone()
                if row:
                    data = row[0]
                else:
                    # Migrate existing local JSON data into PostgreSQL once.
                    data = _local_json_state() or {
                        'users': {}, 'messages': [], 'pins': {}, 'verify_bot_admin': None
                    }
                    conn.execute(
                        "INSERT INTO vibegram_state (id, data) VALUES (1, %s) "
                        "ON CONFLICT (id) DO NOTHING",
                        (Jsonb(data),)
                    )
            USERS = data.get('users', {})
            MESSAGES = data.get('messages', [])
            PINS = data.get('pins', {})
            VERIFY_BOT_ADMIN = data.get('verify_bot_admin')
            print('Vibegram: данные загружены из PostgreSQL.')
        except Exception as e:
            raise RuntimeError(
                'Не удалось подключиться к PostgreSQL. Проверь DATABASE_URL и доступность базы.'
            ) from e
    else:
        data = _local_json_state() or {}
        USERS = data.get('users', {})
        MESSAGES = data.get('messages', [])
        PINS = data.get('pins', {})
        VERIFY_BOT_ADMIN = data.get('verify_bot_admin')
        print('Vibegram: DATABASE_URL не задан, используется локальный JSON-файл.')

    ensure_system_bot()
    # Persist the system bot / initial state in PostgreSQL as well.
    if DATABASE_URL:
        save_db()


def save_db():
    data = {
        'users': USERS,
        'messages': MESSAGES,
        'pins': PINS,
        'verify_bot_admin': VERIFY_BOT_ADMIN
    }
    with DB_LOCK:
        if DATABASE_URL:
            try:
                with _connect_postgres() as conn:
                    conn.execute("""
                        INSERT INTO vibegram_state (id, data, updated_at)
                        VALUES (1, %s, NOW())
                        ON CONFLICT (id) DO UPDATE
                        SET data = EXCLUDED.data, updated_at = NOW()
                    """, (Jsonb(data),))
            except Exception as e:
                print('Ошибка сохранения в PostgreSQL:', e)
                raise
        else:
            try:
                with open(DB_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print("Ошибка сохранения JSON-БД:", e)
                raise

load_db()

class VibegramHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/api/sync':
            qs = parse_qs(parsed.query)
            me = qs.get('me', [''])[0].strip().lower().lstrip('@')
            now = int(time.time() * 1000)

            ensure_system_bot()
            USERS['verify_bot']['last_seen'] = now

            if me and me in USERS and not USERS[me].get('is_channel'):
                # /api/sync is the heartbeat: if a client stops polling, its
                # last_seen naturally expires and other clients show it offline.
                USERS[me]['last_seen'] = now
                USERS[me]['online'] = True

            # Expire typing indicators and mark stale accounts offline in memory.
            for username, user_obj in USERS.items():
                last_seen = int(user_obj.get('last_seen') or 0)
                user_obj['online'] = bool(not user_obj.get('is_channel') and now - last_seen < 6500)
                if now - int(user_obj.get('typing_time') or 0) >= 5000:
                    user_obj['typing_to'] = ''

            channels_set = {u for u, obj in USERS.items() if obj.get('is_channel')}
            my_messages = [
                m for m in MESSAGES
                if m['sender'] == me or m['to'] == me or m['to'] in channels_set or m['sender'] in channels_set
            ]
            self.send_json({
                'users': USERS,
                'messages': my_messages,
                'pins': PINS,
                'verifyBotAdmin': VERIFY_BOT_ADMIN,
                'serverTime': now
            })
            return

        return super().do_GET()

    def do_POST(self):
        global VERIFY_BOT_ADMIN
        parsed = urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length).decode('utf-8')) if length > 0 else {}
        now = int(time.time() * 1000)

        # Регистрация / сохранение личного профиля с проверкой уникальности @username и выдачей ID
        if parsed.path == '/api/profile':
            old_u = str(body.get('oldUsername') or '').strip().lower().lstrip('@')
            u = str(body.get('username') or '').strip().lower().lstrip('@')
            email = str(body.get('email') or '').strip().lower()
            phone = str(body.get('phone') or '').strip()

            if not u or u == 'verify_bot':
                return self.send_json({'error': 'Некорректный @username'}, 400)

            # Проверка: не занят ли @username каналом или другим человеком
            if u in USERS:
                existing_target = USERS[u]
                if existing_target.get('is_channel'):
                    return self.send_json({'error': f'Юзернейм @{u} уже занят каналом! Выберите другой.'}, 400)
                existing_email = str(existing_target.get('email') or '').strip().lower()
                # Если старый юзернейм не совпадает с новым и почта другая — значит юзернейм чужой!
                if old_u != u and existing_email and email and existing_email != email:
                    return self.send_json({'error': f'Юзернейм @{u} уже занят другим пользователем!'}, 400)

            if old_u and old_u != u and old_u in USERS and not USERS[old_u].get('is_channel'):
                USERS[u] = USERS.pop(old_u)
                if VERIFY_BOT_ADMIN == old_u:
                    VERIFY_BOT_ADMIN = u
                for m in MESSAGES:
                    if m['sender'] == old_u: m['sender'] = u
                    if m['to'] == old_u: m['to'] = u
                for k, obj in USERS.items():
                    if obj.get('is_channel'):
                        if obj.get('owner') == old_u:
                            obj['owner'] = u
                        subs = obj.get('subscribers', [])
                        if old_u in subs:
                            subs[subs.index(old_u)] = u
                        admins = obj.get('admins', {})
                        if old_u in admins:
                            admins[u] = admins.pop(old_u)

            existing = USERS.get(u, {})
            user_id = existing.get('id')
            if not user_id:
                user_id = assign_user_id(u)
            if u in ('paveldurov', 'vibegram'):
                user_id = 1

            USERS[u] = {
                'id': user_id,
                'username': u,
                'name': str(body.get('name') or existing.get('name') or u).strip() or u,
                'bio': str(body.get('bio') or existing.get('bio') or 'В Vibegram').strip(),
                'phone': phone or existing.get('phone', '+375 (29) 000-00-00'),
                'email': email or existing.get('email', ''),
                'color': body.get('color') or existing.get('color', '#6c5ce7'),
                'avatar': body.get('avatar') if 'avatar' in body else existing.get('avatar', ''),
                'privacy': body.get('privacy') or existing.get('privacy', 'everyone'),
                'is_premium': bool(body.get('is_premium', existing.get('is_premium', False))),
                'sticker_id': body.get('sticker_id') or existing.get('sticker_id', 'star'),
                'is_verified': bool(body.get('is_verified', existing.get('is_verified', False))),
                'verify_category': body.get('verify_category') or existing.get('verify_category', 'none'),
                'verify_reason': body.get('verify_reason') if 'verify_reason' in body else existing.get('verify_reason', ''),
                'is_channel': False,
                'last_seen': now,
                'typing_to': existing.get('typing_to', ''),
                'typing_time': existing.get('typing_time', 0)
            }
            save_db()
            return self.send_json({'ok': True, 'profile': USERS[u]})

        # Создание или редактирование Telegram-канала (исправлена валидация и права админов)
        if parsed.path == '/api/channel/save':
            actor = str(body.get('actor') or body.get('owner') or '').strip().lower().lstrip('@')
            old_handle = str(body.get('oldUsername') or '').strip().lower().lstrip('@')
            handle = str(body.get('username') or '').strip().lower().lstrip('@')
            name = str(body.get('name') or '').strip()
            bio = str(body.get('bio') or '').strip()
            color = body.get('color') or '#039be5'
            avatar = body.get('avatar') or ''
            comments_enabled = bool(body.get('comments_enabled', True))
            sign_messages = bool(body.get('sign_messages', True))
            admins = body.get('admins') if isinstance(body.get('admins'), dict) else None

            if not handle or not name:
                return self.send_json({'error': 'Укажите Название канала и @username'}, 400)

            if not actor:
                actor = 'user'

            if handle in USERS and not USERS[handle].get('is_channel'):
                return self.send_json({'error': f'Юзернейм @{handle} уже занят пользователем!'}, 400)

            if old_handle and old_handle in USERS and USERS[old_handle].get('is_channel'):
                ch_obj = USERS[old_handle]
                ch_owner = ch_obj.get('owner', actor)
                ch_admins = ch_obj.get('admins', {})
                actor_perms = ch_admins.get(actor, {})
                if actor != ch_owner and not actor_perms.get('can_edit') and not actor_perms.get('can_promote'):
                    return self.send_json({'error': 'Недостаточно прав для редактирования канала'}, 403)

                if old_handle != handle:
                    if handle in USERS:
                        return self.send_json({'error': f'Канал @{handle} уже занят!'}, 400)
                    USERS[handle] = USERS.pop(old_handle)
                    for m in MESSAGES:
                        if m['sender'] == old_handle: m['sender'] = handle
                        if m['to'] == old_handle: m['to'] = handle
            else:
                if handle in USERS:
                    return self.send_json({'error': f'Канал @{handle} уже существует! Выберите другой @username.'}, 400)

            existing = USERS.get(handle, {})
            owner_final = existing.get('owner') or actor
            subs = existing.get('subscribers', [owner_final])
            if owner_final not in subs:
                subs.append(owner_final)

            final_admins = admins if admins is not None else existing.get('admins', {})
            for adm_u in final_admins.keys():
                if adm_u not in subs:
                    subs.append(adm_u)

            ch_id = existing.get('id') or (2000 + len(USERS))

            USERS[handle] = {
                'id': ch_id,
                'username': handle,
                'name': name,
                'bio': bio or 'Официальный канал в Vibegram',
                'color': color,
                'avatar': avatar,
                'is_channel': True,
                'owner': owner_final,
                'admins': final_admins,
                'subscribers': subs,
                'comments_enabled': comments_enabled,
                'sign_messages': sign_messages,
                'is_verified': bool(existing.get('is_verified', False)),
                'verify_category': existing.get('verify_category', 'Официальный канал'),
                'verify_reason': existing.get('verify_reason', 'Подлинность канала подтверждена модерацией Vibegram.'),
                'verify_pending': bool(existing.get('verify_pending', False)),
                'last_seen': now
            }
            save_db()
            return self.send_json({'ok': True, 'channel': USERS[handle]})

        # Подписка / отписка от канала
        if parsed.path == '/api/channel/subscribe':
            user = str(body.get('user') or '').strip().lower().lstrip('@')
            channel = str(body.get('channel') or '').strip().lower().lstrip('@')
            if channel not in USERS or not USERS[channel].get('is_channel'):
                return self.send_json({'error': 'Канал не найден'}, 404)

            subs = USERS[channel].setdefault('subscribers', [])
            if user in subs:
                subs.remove(user)
                subscribed = False
            else:
                subs.append(user)
                subscribed = True
            save_db()
            return self.send_json({'ok': True, 'subscribed': subscribed, 'subscribers': subs})

        # Добавление комментария к посту канала (от имени своего аккаунта или от имени канала)
        if parsed.path == '/api/channel/comment':
            post_id = body.get('post_id', '')
            user = str(body.get('user') or '').strip().lower().lstrip('@')
            text = str(body.get('text') or '').strip()
            if not post_id or not user or not text:
                return self.send_json({'error': 'Invalid fields'}, 400)

            user_obj = USERS.get(user, {})
            for m in MESSAGES:
                if m['id'] == post_id:
                    comments = m.setdefault('comments', [])
                    c_obj = {
                        'id': f"c_{now}_{len(comments)}",
                        'sender': user,
                        'name': user_obj.get('name', user),
                        'avatar': user_obj.get('avatar', ''),
                        'color': user_obj.get('color', '#6c5ce7'),
                        'is_channel': bool(user_obj.get('is_channel', False)),
                        'is_premium': bool(user_obj.get('is_premium', False)),
                        'sticker_id': user_obj.get('sticker_id', 'star'),
                        'is_verified': bool(user_obj.get('is_verified', False)),
                        'text': text,
                        'time': now
                    }
                    comments.append(c_obj)
                    save_db()
                    return self.send_json({'ok': True, 'comments': comments})
            return self.send_json({'error': 'Post not found'}, 404)

        # Подача заявки на верификацию (аккаунта или канала) в общедоступный бот @verify_bot
        if parsed.path == '/api/verification/request':
            applicant = str(body.get('applicant') or '').strip().lower().lstrip('@')
            target = str(body.get('target') or '').strip().lower().lstrip('@')
            category = str(body.get('category') or 'Официальный проект').strip()
            reason = str(body.get('reason') or '').strip() or 'Подтвержденный профиль Vibegram'

            if target not in USERS:
                return self.send_json({'error': 'Профиль или канал не найден'}, 404)

            target_obj = USERS[target]
            target_obj['verify_pending'] = True
            target_obj['verify_category'] = category
            target_obj['verify_reason'] = reason

            # 1. Подтверждение самому заявителю от бота
            MESSAGES.append({
                'id': f"msg_{now}_{len(MESSAGES)}_ack",
                'sender': 'verify_bot',
                'to': applicant,
                'text': f"📨 Ваша заявка на верификацию «{target_obj.get('name')}» (@{target}) принята!\nКатегория: {category}\nОснование: {reason}\n\nЕсли вы Создатель (ID: 1), отправьте в этот чат цифру 1, чтобы управлять заявками.",
                'time': now,
                'read': False,
                'reactions': {}
            })

            # 2. Если Создатель уже привязал бота (или есть юзер с ID 1), отправляем карточку с кнопками ему
            admin_recipient = VERIFY_BOT_ADMIN
            if not admin_recipient:
                for uk, uo in USERS.items():
                    if uo.get('id') == 1 and not uo.get('is_channel'):
                        admin_recipient = uk
                        break
            if not admin_recipient:
                admin_recipient = applicant

            MESSAGES.append({
                'id': f"msg_{now}_{len(MESSAGES)}_req",
                'sender': 'verify_bot',
                'to': admin_recipient,
                'text': f"📋 ЗАЯВКА НА ВЕРИФИКАЦИЮ\n\nОбъект: {target_obj.get('name')} (@{target})\nПодал: @{applicant} (ID: {USERS.get(applicant, {}).get('id', '?')})\nТип галочки: {category}\nЗа что выдается: {reason}",
                'time': now + 1,
                'read': False,
                'verification_request': {
                    'target': target,
                    'target_name': target_obj.get('name', target),
                    'applicant': applicant,
                    'category': category,
                    'reason': reason,
                    'status': 'pending'
                },
                'reactions': {}
            })
            save_db()
            return self.send_json({'ok': True})

        # Решение Создателя в боте @verify_bot: Выдать галочку или Отказать
        if parsed.path == '/api/verification/decide':
            msg_id = body.get('msg_id', '')
            decision = body.get('decision', '')  # 'approve' | 'reject'
            target = str(body.get('target') or '').strip().lower().lstrip('@')

            applicant = ''
            target_name = target
            cat = 'Официальный статус'
            rsn = 'Подлинность подтверждена создателем Vibegram'

            for m in MESSAGES:
                if m['id'] == msg_id and m.get('verification_request'):
                    req = m['verification_request']
                    req['status'] = 'approved' if decision == 'approve' else 'rejected'
                    applicant = req.get('applicant', '')
                    target_name = req.get('target_name', target)
                    cat = req.get('category', cat)
                    rsn = req.get('reason', rsn)

            if target in USERS:
                USERS[target]['verify_pending'] = False
                USERS[target]['is_verified'] = (decision == 'approve')
                if decision == 'approve':
                    USERS[target]['verify_category'] = cat
                    USERS[target]['verify_reason'] = rsn

            if applicant:
                notify_text = (
                    f"✅ Верификация одобрена!\nПрофилю/каналу «{target_name}» (@{target}) выдана официальная галочка!\nСтатус: {cat}\nОписание: {rsn}"
                    if decision == 'approve' else
                    f"❌ Заявка на верификацию «{target_name}» (@{target}) была отклонена создателем."
                )
                MESSAGES.append({
                    'id': f"msg_{now}_{len(MESSAGES)}",
                    'sender': 'verify_bot',
                    'to': applicant,
                    'text': notify_text,
                    'time': now,
                    'read': False,
                    'reactions': {}
                })

            save_db()
            return self.send_json({'ok': True})

        # Статус "печатает..."
        if parsed.path == '/api/typing':
            sender = str(body.get('from') or '').strip().lower().lstrip('@')
            to = str(body.get('to') or '').strip().lower().lstrip('@')
            active = bool(body.get('active', True))
            if sender in USERS and not USERS[sender].get('is_channel'):
                # Only announce typing to a known recipient; a blank/false
                # activity signal clears the transient state immediately.
                if active and to and to in USERS:
                    USERS[sender]['typing_to'] = to
                    USERS[sender]['typing_time'] = now
                else:
                    USERS[sender]['typing_to'] = ''
                    USERS[sender]['typing_time'] = 0
            return self.send_json({'ok': True})

        # Загрузка медиа
        if parsed.path == '/api/upload':
            data_url = body.get('data', '')
            ext = str(body.get('ext') or 'jpg').strip().lower()
            if not data_url or ',' not in data_url:
                return self.send_json({'error': 'Invalid media data'}, 400)
            try:
                _, encoded = data_url.split(',', 1)
                binary = base64.b64decode(encoded)
                if len(binary) > 25 * 1024 * 1024:
                    return self.send_json({'error': 'Файл больше 25 МБ'}, 413)
                filename = f"media_{now}_{len(MESSAGES)}.{ext}"
                filepath = os.path.join(UPLOAD_DIR, filename)
                with open(filepath, 'wb') as f:
                    f.write(binary)
                return self.send_json({'ok': True, 'url': f"/uploads/{filename}"})
            except Exception as e:
                return self.send_json({'error': str(e)}, 500)

        # Отправка сообщения (ЛС, Бот @verify_bot или Пост в канал)
        if parsed.path == '/api/send':
            sender = str(body.get('from') or '').strip().lower().lstrip('@')
            to = str(body.get('to') or '').strip().lower().lstrip('@')
            text = str(body.get('text') or '').strip()
            reply_to = body.get('reply_to', None)
            media = body.get('media', None)
            forwarded_from = body.get('forwarded_from', None)
            post_as = str(body.get('post_as') or sender).strip().lower().lstrip('@')

            if not sender or not to or (not text and not media):
                return self.send_json({'error': 'Invalid fields'}, 400)

            if sender in USERS:
                USERS[sender]['typing_to'] = ''
                USERS[sender]['typing_time'] = 0

            # 1. Если пользователь пишет общедоступному боту @verify_bot
            if to == 'verify_bot':
                user_msg = {
                    'id': f"msg_{now}_{len(MESSAGES)}",
                    'sender': sender,
                    'to': 'verify_bot',
                    'text': text,
                    'time': now,
                    'read': True,
                    'reply_to': reply_to,
                    'media': media,
                    'reactions': {}
                }
                MESSAGES.append(user_msg)

                sender_obj = USERS.get(sender, {})
                sender_id = int(sender_obj.get('id') or 0)
                clean_cmd = text.strip().lower()

                # Управление Vibegram Premium через общедоступного бота.
                # Создатель пишет: /premium @username on  или  /premium @username off
                is_creator = (sender == VERIFY_BOT_ADMIN or sender_id == 1 or sender in ('paveldurov', 'vibegram'))
                parts = clean_cmd.split()
                if parts and parts[0] in ('/premium', '/vip'):
                    if len(parts) == 2 and parts[1] == 'request':
                        bot_reply = (
                            f"⭐ Запрос Vibegram Premium принят для @{sender}.\n"
                            "Premium выдаётся только Создателем через этот бот. Ожидайте решения."
                        )
                        creator_username = VERIFY_BOT_ADMIN
                        if not creator_username:
                            creator_username = next((name for name, obj in USERS.items()
                                if not obj.get('is_channel') and not obj.get('is_bot') and int(obj.get('id') or obj.get('uid') or 0) == 1), None)
                        if creator_username and creator_username in USERS:
                            MESSAGES.append({
                                'id': f"msg_{now + 1}_{len(MESSAGES)}_premium_request",
                                'sender': 'verify_bot', 'to': creator_username,
                                'text': f"⭐ ЗАПРОС VIBEGRAM PREMIUM\nПользователь: @{sender} (ID: {sender_id})\nДля выдачи: /premium @{sender} on\nДля отказа: /premium @{sender} off",
                                'time': now + 1, 'read': False, 'reactions': {},
                                'premium_request': {'applicant': sender, 'status': 'pending'}
                            })
                        save_db()
                    elif len(parts) == 3 and parts[1].lstrip('@') in USERS and parts[2] in ('on', 'off'):
                        target_premium = parts[1].lstrip('@')
                        if not is_creator:
                            bot_reply = '⛔ Выдавать или снимать Vibegram Premium может только Создатель (ID: 1).'
                        else:
                            enabled = parts[2] == 'on'
                            USERS[target_premium]['is_premium'] = enabled
                            if enabled:
                                USERS[target_premium]['sticker'] = USERS[target_premium].get('sticker') or 'star'
                            bot_reply = f"{'✅ Vibegram Premium выдан' if enabled else 'ℹ️ Vibegram Premium снят'} для @{target_premium}."
                            MESSAGES.append({
                                'id': f"msg_{now + 1}_{len(MESSAGES)}_premium_notice",
                                'sender': 'verify_bot', 'to': target_premium,
                                'text': f"{'⭐ Вам выдан Vibegram Premium!' if enabled else 'Vibegram Premium для вашего аккаунта отключён.'}",
                                'time': now + 1, 'read': False, 'reactions': {}
                            })
                            save_db()
                    else:
                        bot_reply = ('Использование Premium-команд:\n/premium request — запросить Premium\n'
                                     '/premium @username on — выдать (только Создатель)\n'
                                     '/premium @username off — снять (только Создатель)')
                # Авторизация Создателя в боте по вводу ID "1" или "/id 1"
                elif clean_cmd in ('1', 'id 1', '/id 1', '/auth 1', 'id: 1'):
                    if sender_id == 1 or sender in ('paveldurov', 'vibegram'):
                        sender_obj['id'] = 1
                        VERIFY_BOT_ADMIN = sender
                        # Перенаправляем все нерассмотренные заявки этому аккаунту
                        pending_count = 0
                        for m in MESSAGES:
                            if m.get('verification_request') and m['verification_request'].get('status') == 'pending':
                                m['to'] = sender
                                pending_count += 1
                        bot_reply = (
                            f"👑 Доступ Создателя подтвержден!\n"
                            f"Бот верификации официально привязан к вашему аккаунту @{sender} (ID: 1).\n"
                            f"Активных заявок на рассмотрении: {pending_count}.\n"
                            f"Теперь все новые заявки на верификацию каналов и профилей приходят прямо сюда с кнопками «Выдать галочку» / «Отказать»."
                        )
                    else:
                        bot_reply = (
                            f"⛔ Ошибка доступа!\n"
                            f"Ваш личный ID: {sender_id}. Пароль «1» принадлежит только Создателю (@paveldurov / ID: 1)."
                        )
                else:
                    bot_reply = (
                        f"🤖 Общедоступный бот верификации Vibegram\n\n"
                        f"• Ваш профиль: @{sender} (Ваш уникальный ID: {sender_id})\n"
                        f"• Статус управляющего ботом: {('@' + VERIFY_BOT_ADMIN + ' (ID: 1)') if VERIFY_BOT_ADMIN else 'Не привязан'}\n\n"
                        f"🔑 Если вы Создатель (ID: 1), отправьте в этот чат цифру «1», чтобы привязать панель выдачи галочек к своему аккаунту."
                    )

                MESSAGES.append({
                    'id': f"msg_{now + 2}_{len(MESSAGES)}_bot",
                    'sender': 'verify_bot',
                    'to': sender,
                    'text': bot_reply,
                    'time': now + 2,
                    'read': False,
                    'reactions': {}
                })
                save_db()
                return self.send_json({'ok': True, 'message': user_msg})

            # 2. Если это публикация поста в канал (проверяем права владельца или администратора)
            is_channel_post = False
            post_author_signature = ''
            if to in USERS and USERS[to].get('is_channel'):
                ch_obj = USERS[to]
                ch_owner = ch_obj.get('owner', '')
                ch_admins = ch_obj.get('admins', {})
                adm_perms = ch_admins.get(sender, {})
                if sender != ch_owner and not adm_perms.get('can_post'):
                    return self.send_json({'error': 'У вас нет прав на публикацию постов в этом канале'}, 403)
                is_channel_post = True
                if ch_obj.get('sign_messages', True):
                    as_obj = USERS.get(post_as) or USERS.get(sender) or {}
                    post_author_signature = as_obj.get('name', sender)

            msg = {
                'id': f"msg_{now}_{len(MESSAGES)}",
                'sender': to if is_channel_post else sender,
                'to': to,
                'is_channel_post': is_channel_post,
                'author_signature': post_author_signature,
                'views': [sender] if is_channel_post else [],
                'comments': [],
                'text': text,
                'time': now,
                'read': True if (sender == to or is_channel_post) else False,
                'reply_to': reply_to,
                'media': media,
                'forwarded_from': forwarded_from,
                'reactions': {}
            }
            MESSAGES.append(msg)
            save_db()
            return self.send_json({'ok': True, 'message': msg})

        # Закрепление чата в боковом списке пользователя (хранится в PostgreSQL/JSON вместе с PINS)
        if parsed.path == '/api/chat-pin':
            me = str(body.get('me') or '').strip().lower().lstrip('@')
            peer = str(body.get('peer') or '').strip().lower().lstrip('@')
            pinned = bool(body.get('pinned'))
            if not me or not peer:
                return self.send_json({'error': 'Invalid users'}, 400)
            key = f'chatlist:{me}'
            current = PINS.get(key, [])
            if not isinstance(current, list):
                current = []
            if pinned and peer not in current:
                current.insert(0, peer)
            elif not pinned and peer in current:
                current.remove(peer)
            PINS[key] = current
            save_db()
            return self.send_json({'ok': True, 'pinned': current})

        # Закрепление / открепление сообщения
        if parsed.path == '/api/pin':
            u1 = str(body.get('me') or '').strip().lower().lstrip('@')
            u2 = str(body.get('peer') or '').strip().lower().lstrip('@')
            msg_data = body.get('message', None)
            if not u1 or not u2:
                return self.send_json({'error': 'Invalid users'}, 400)

            key = get_chat_key(u1, u2)
            if msg_data is None:
                PINS.pop(key, None)
            else:
                PINS[key] = msg_data
            save_db()
            return self.send_json({'ok': True, 'pins': PINS})

        # Реакция на сообщение
        if parsed.path == '/api/react':
            msg_id = body.get('id', '')
            user = str(body.get('user') or '').strip().lower().lstrip('@')
            emoji = str(body.get('emoji') or '').strip()
            if not msg_id or not user or not emoji:
                return self.send_json({'error': 'Invalid fields'}, 400)

            for m in MESSAGES:
                if m['id'] == msg_id:
                    reactions = m.setdefault('reactions', {})
                    users_list = reactions.get(emoji, [])
                    if user in users_list:
                        users_list.remove(user)
                        if not users_list:
                            del reactions[emoji]
                    else:
                        users_list.append(user)
                        reactions[emoji] = users_list
                    save_db()
                    return self.send_json({'ok': True, 'reactions': reactions})
            return self.send_json({'error': 'Message not found'}, 404)

        # Прочтение сообщений + просмотры постов
        if parsed.path == '/api/read':
            reader = str(body.get('reader') or '').strip().lower().lstrip('@')
            peer = str(body.get('peer') or '').strip().lower().lstrip('@')
            changed = False
            for m in MESSAGES:
                if m.get('is_channel_post') and m['to'] == peer:
                    views = m.setdefault('views', [])
                    if reader and reader not in views:
                        views.append(reader)
                        changed = True
                elif m['sender'] == peer and m['to'] == reader and not m['read']:
                    m['read'] = True
                    changed = True
            if changed:
                save_db()
            return self.send_json({'ok': True})

        # Удаление одного сообщения
        if parsed.path == '/api/delete_msg':
            msg_id = body.get('id', '')
            MESSAGES[:] = [m for m in MESSAGES if m['id'] != msg_id]
            for k in list(PINS.keys()):
                if PINS[k] and PINS[k].get('id') == msg_id:
                    del PINS[k]
            save_db()
            return self.send_json({'ok': True})

        # Полный сброс чатов и старых каналов (с сохранением зарегистрированных юзеров)
        if parsed.path == '/api/wipe_all':
            MESSAGES.clear()
            PINS.clear()
            for k in list(USERS.keys()):
                if USERS[k].get('is_channel'):
                    del USERS[k]
            ensure_system_bot()
            save_db()
            return self.send_json({'ok': True})

        # Удаление переписки или канала
        if parsed.path == '/api/delete_chat':
            me = str(body.get('me') or '').strip().lower().lstrip('@')
            peer = str(body.get('peer') or '').strip().lower().lstrip('@')
            if peer in USERS and USERS[peer].get('is_channel'):
                if USERS[peer].get('owner') == me:
                    del USERS[peer]
                    MESSAGES[:] = [m for m in MESSAGES if m['to'] != peer and m['sender'] != peer]
                else:
                    subs = USERS[peer].get('subscribers', [])
                    if me in subs:
                        subs.remove(me)
            else:
                MESSAGES[:] = [
                    m for m in MESSAGES
                    if not ((m['sender'] == me and m['to'] == peer) or (m['sender'] == peer and m['to'] == me))
                ]
            PINS.pop(get_chat_key(me, peer), None)
            save_db()
            return self.send_json({'ok': True})

        self.send_json({'error': 'Not found'}, 404)

class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

if __name__ == '__main__':
    ip = get_local_ip()
    with ThreadedHTTPServer(("0.0.0.0", PORT), VibegramHandler) as httpd:
        print("=" * 68)
        print(" ⚡ СЕРВЕР VIBEGRAM ЗАПУЩЕН (УНИКАЛЬНЫЕ ID, БОТ ВЕРИФИКАЦИИ ПО ID 1)!")
        print(f" 💻 ПК:      http://localhost:{PORT}")
        print(f" 📱 Телефон: http://{ip}:{PORT}")
        print("=" * 68)
        httpd.serve_forever()