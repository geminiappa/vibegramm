import http.server
import socketserver
import json
import time
import socket
import os
import base64
import threading
import re
import hmac
import hashlib
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
PUSH_SUBSCRIPTIONS = {}  # username -> [{subscription: Web Push subscription, created_at: ms}]
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
            'is_premium': False,
            'is_team': False,
            'sticker_id': 'tg_Verify',
            'is_verified': True,
            'verify_category': 'Официальный бот Vibegram',
            'verify_reason': 'Системный бот для выдачи галочек верификации аккаунтам и каналам.',
            'is_bot': True,
            'is_channel': False,
            'last_seen': int(time.time() * 1000)
        }

def assign_user_id(username):
    u = username.strip().lower().lstrip('@')
    # ID 1 is reserved for creator identities; ordinary first-time users do not
    # become moderators only because they happened to register first.
    if u in ('paveldurov', 'vibegram'):
        return 1
    real_users = [obj for obj in USERS.values() if not obj.get('is_channel') and not obj.get('is_bot')]
    max_id = 1000
    for obj in real_users:
        uid = int(obj.get('id') or obj.get('uid') or 0)
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
                        'verify_bot_admin': data.get('verify_bot_admin'),
                        'push_subscriptions': data.get('push_subscriptions', {})
                    }
        except Exception as e:
            print("Ошибка чтения локальной JSON-БД:", e)
    return None


def _connect_postgres():
    # Supabase connection strings should include sslmode=require.
    return psycopg.connect(DATABASE_URL, connect_timeout=10)



def _push_configured():
    return bool(os.environ.get('VAPID_PUBLIC_KEY', '').strip() and
                os.environ.get('VAPID_PRIVATE_KEY', '').strip())


def _avatar_mime_and_bytes(value):
    """Decode a server-stored avatar data URL or a legacy local /uploads file."""
    value = str(value or '').strip()
    if value.startswith('data:image/') and ',' in value:
        header, encoded = value.split(',', 1)
        mime = header[5:].split(';', 1)[0].lower()
        allowed = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
        if mime not in allowed or 'base64' not in header.lower():
            return None, None
        try:
            binary = base64.b64decode(encoded, validate=True)
        except Exception:
            return None, None
        if not binary or len(binary) > 1024 * 1024:
            return None, None
        # Verify that the bytes actually look like the claimed image format.
        signatures = {
            'image/jpeg': binary.startswith(b'\xff\xd8\xff'),
            'image/png': binary.startswith(b'\x89PNG\r\n\x1a\n'),
            'image/webp': len(binary) >= 12 and binary[:4] == b'RIFF' and binary[8:12] == b'WEBP',
            'image/gif': binary.startswith((b'GIF87a', b'GIF89a')),
        }
        if not signatures.get(mime, False):
            return None, None
        return mime, binary
    if value.startswith('/uploads/'):
        filename = os.path.basename(urlparse(value).path)
        if not filename or filename in ('.', '..'):
            return None, None
        filepath = os.path.join(UPLOAD_DIR, filename)
        try:
            if os.path.isfile(filepath) and os.path.getsize(filepath) <= 1024 * 1024:
                with open(filepath, 'rb') as stream:
                    binary = stream.read()
                ext = os.path.splitext(filename)[1].lower()
                mime = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp', '.gif': 'image/gif'}.get(ext)
                if mime and binary:
                    return mime, binary
        except OSError:
            pass
    return None, None


def _store_avatar_value(candidate, existing='', max_bytes=1024 * 1024):
    """Store avatars in the shared DB as bounded data URLs so they survive Render restarts."""
    candidate = str(candidate or '').strip()
    existing = str(existing or '').strip()
    if not candidate:
        return existing
    mime, binary = _avatar_mime_and_bytes(candidate)
    if binary:
        if len(binary) > max_bytes:
            raise ValueError(f'Аватар слишком большой. Максимум {max_bytes // 1024} КБ.')
        return f'data:{mime};base64,{base64.b64encode(binary).decode("ascii")}'
    # Preserve a previous good avatar if a new client sends an invalid/broken image.
    if candidate.startswith('data:image/') or candidate.startswith('/uploads/'):
        if existing:
            return existing
        return candidate if candidate.startswith('/uploads/') else ''
    # External HTTPS avatars are retained for compatibility with previously imported profiles.
    if candidate.startswith('https://') and len(candidate) <= 2048:
        return candidate
    return existing


def _avatar_public_path(username, avatar_value):
    handle = re.sub(r'[^a-z0-9_]', '', str(username or '').lower().lstrip('@'))
    if not handle or not str(avatar_value or '').strip():
        return ''
    version = hashlib.sha1(str(avatar_value).encode('utf-8', errors='ignore')).hexdigest()[:12]
    return f'/api/avatar?username={handle}&v={version}'


def _public_user(username, user_obj):
    """Return a lightweight profile: avatar bytes stay in DB, sync sends only a URL."""
    result = dict(user_obj or {})
    avatar_value = str(result.get('avatar') or '').strip()
    if avatar_value:
        result['avatar'] = _avatar_public_path(username, avatar_value)
    else:
        result['avatar'] = ''
    return result


def _absolute_public_url(path_or_url, base_url, username=''):
    value = str(path_or_url or '').strip()
    handle = re.sub(r'[^a-z0-9_]', '', str(username or '').lower().lstrip('@'))
    if value and handle:
        # Versioned DB-backed endpoint keeps notification icons stable and refreshable.
        return base_url.rstrip('/') + _avatar_public_path(handle, value)
    if value.startswith('https://') or value.startswith('http://'):
        return value
    if value.startswith('/uploads/'):
        return base_url.rstrip('/') + value
    return base_url.rstrip('/') + '/facuicon.jpeg'


def _webpush_send_worker(username, records, payload):
    private_key = os.environ.get('VAPID_PRIVATE_KEY', '').strip()
    if not private_key or not os.environ.get('VAPID_PUBLIC_KEY', '').strip():
        return
    try:
        from pywebpush import webpush, WebPushException
    except Exception as exc:
        print(f'[Vibegram WebPush] pywebpush недоступен: {exc}', flush=True)
        return

    expired_endpoints = set()
    subject = os.environ.get('VAPID_SUBJECT', 'mailto:admin@vibegramm.onrender.com').strip()
    for record in records:
        subscription = record.get('subscription') if isinstance(record, dict) else None
        if not isinstance(subscription, dict):
            continue
        endpoint = str(subscription.get('endpoint') or '')
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                vapid_private_key=private_key,
                vapid_claims={'sub': subject},
                ttl=60,
                timeout=8
            )
        except Exception as exc:
            response = getattr(exc, 'response', None)
            status_code = getattr(response, 'status_code', None)
            if status_code in (404, 410) and endpoint:
                expired_endpoints.add(endpoint)
            else:
                print(f'[Vibegram WebPush] delivery failed for @{username}: {str(exc)[:240]}', flush=True)

    if expired_endpoints:
        with DB_LOCK:
            current = PUSH_SUBSCRIPTIONS.get(username, [])
            PUSH_SUBSCRIPTIONS[username] = [
                row for row in current
                if (row.get('subscription') or {}).get('endpoint') not in expired_endpoints
            ]
            if not PUSH_SUBSCRIPTIONS[username]:
                PUSH_SUBSCRIPTIONS.pop(username, None)
            save_db()


def send_web_push(username, payload):
    user = str(username or '').strip().lower().lstrip('@')
    if not user or not _push_configured():
        return
    with DB_LOCK:
        records = list(PUSH_SUBSCRIPTIONS.get(user, []))
    if not records:
        return
    thread = threading.Thread(target=_webpush_send_worker, args=(user, records, payload), daemon=True)
    thread.start()


def dispatch_push_for_messages(messages, base_url):
    """Queue push delivery for server-created messages without exposing subscriptions."""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = str(msg.get('text') or '').strip()
        media = msg.get('media') if isinstance(msg.get('media'), dict) else None
        if not text:
            media_type = str((media or {}).get('type') or '').lower()
            text = {'image': '📷 Фото', 'video': '🎬 Видео', 'voice': '🎙 Голосовое сообщение',
                    'video_note': '⭕ Видеосообщение', 'sticker': 'Стикер', 'file': '📎 Файл'}.get(media_type, 'Новое сообщение')
        text = text[:240]

        if msg.get('is_channel_post'):
            channel_name = str(msg.get('to') or '').strip().lower().lstrip('@')
            channel = USERS.get(channel_name, {})
            author = str(((msg.get('views') or [''])[0]) or '').lower().lstrip('@')
            icon = _absolute_public_url(channel.get('avatar'), base_url, channel_name)
            payload = {
                'title': str(channel.get('name') or channel_name or 'Новый пост')[:100],
                'body': text,
                'icon': icon,
                'badge': base_url.rstrip('/') + '/pwa-192.png',
                'username': channel_name,
                'url': base_url.rstrip('/') + '/?openChat=' + channel_name,
                'tag': 'vibegram-channel-' + channel_name
            }
            subscribers = channel.get('subscribers', [])
            if isinstance(subscribers, list):
                for subscriber in set(str(s or '').lower().lstrip('@') for s in subscribers):
                    if subscriber and subscriber != author:
                        send_web_push(subscriber, payload)
            continue

        recipient = str(msg.get('to') or '').strip().lower().lstrip('@')
        sender = str(msg.get('sender') or '').strip().lower().lstrip('@')
        if not recipient or recipient == sender or recipient == 'verify_bot':
            continue
        sender_profile = USERS.get(sender, {})
        icon = _absolute_public_url(sender_profile.get('avatar'), base_url, sender)
        payload = {
            'title': str(sender_profile.get('name') or sender or 'Новое сообщение')[:100],
            'body': text,
            'icon': icon,
            'badge': base_url.rstrip('/') + '/pwa-192.png',
            'username': sender,
            'url': base_url.rstrip('/') + '/?openChat=' + sender,
            'tag': 'vibegram-chat-' + sender
        }
        send_web_push(recipient, payload)

def load_db():
    global USERS, MESSAGES, PINS, VERIFY_BOT_ADMIN, PUSH_SUBSCRIPTIONS

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
            PUSH_SUBSCRIPTIONS = data.get('push_subscriptions', {}) if isinstance(data.get('push_subscriptions', {}), dict) else {}
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
        PUSH_SUBSCRIPTIONS = data.get('push_subscriptions', {}) if isinstance(data.get('push_subscriptions', {}), dict) else {}
        print('Vibegram: DATABASE_URL не задан, используется локальный JSON-файл.')

    ensure_system_bot()
    # Migrate any old client-forged checkmarks away. Only retain a verified flag
    # when the shared database contains a recorded approved @verify_bot request.
    approved_targets = {
        str((m.get('verification_request') or {}).get('target') or '').strip().lower().lstrip('@')
        for m in MESSAGES
        if m.get('verification_request') and (m.get('verification_request') or {}).get('status') == 'approved'
    }
    state_changed = False
    # Migrate legacy avatar files into the shared DB when the old file is still
    # present. This repairs cross-device display and avoids future dependence on
    # Render's ephemeral filesystem for profile/channel avatars.
    for username, user_obj in USERS.items():
        avatar_value = str(user_obj.get('avatar') or '').strip()
        if avatar_value.startswith('/uploads/'):
            mime, binary = _avatar_mime_and_bytes(avatar_value)
            if mime and binary and len(binary) <= 1024 * 1024:
                user_obj['avatar'] = f'data:{mime};base64,{base64.b64encode(binary).decode("ascii")}'
                state_changed = True
        # Legacy Premium flags did not record an auditable server-side grant.
        # Remove them once on migration; grants issued below carry versioned metadata.
        if user_obj.get('is_premium') and not (
            user_obj.get('premium_grant_version') == 1 and
            user_obj.get('premium_granted_by') == 'admin_token'
        ):
            user_obj['is_premium'] = False
            user_obj.pop('premium_granted_by', None)
            user_obj.pop('premium_granted_at', None)
            user_obj.pop('premium_grant_version', None)
            state_changed = True
        if user_obj.get('is_team') and not (
            user_obj.get('team_grant_version') == 1 and
            user_obj.get('team_granted_by') == 'admin_token'
        ):
            user_obj['is_team'] = False
            user_obj.pop('team_granted_by', None)
            user_obj.pop('team_granted_at', None)
            user_obj.pop('team_grant_version', None)
            state_changed = True
        if user_obj.get('is_verified') and not user_obj.get('is_bot') and username not in approved_targets:
            user_obj['is_verified'] = False
            state_changed = True
    # Persist the system bot, migrated badge states and initial state.
    if DATABASE_URL or state_changed:
        save_db()


def save_db():
    data = {
        'users': USERS,
        'messages': MESSAGES,
        'pins': PINS,
        'verify_bot_admin': VERIFY_BOT_ADMIN,
        'push_subscriptions': PUSH_SUBSCRIPTIONS
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
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        '.webmanifest': 'application/manifest+json',
        '.js': 'text/javascript; charset=utf-8',
    }

    def log_message(self, format, *args):
        pass

    def _public_base_url(self):
        configured = os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')
        if configured:
            return configured
        host = str(self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or 'vibegramm.onrender.com').split(',')[0].strip()
        proto = str(self.headers.get('X-Forwarded-Proto') or 'https').split(',')[0].strip()
        if proto not in ('http', 'https'):
            proto = 'https'
        return f'{proto}://{host}'

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

        if parsed.path == '/api/push/public-key':
            public_key = os.environ.get('VAPID_PUBLIC_KEY', '').strip()
            if not public_key or not os.environ.get('VAPID_PRIVATE_KEY', '').strip():
                return self.send_json({'ok': False, 'enabled': False, 'error': 'Web Push не настроен на сервере.'}, 503)
            return self.send_json({'ok': True, 'enabled': True, 'publicKey': public_key})

        if parsed.path == '/api/avatar':
            qs = parse_qs(parsed.query)
            handle = str(qs.get('username', [''])[0] or '').strip().lower().lstrip('@')
            user_obj = USERS.get(handle, {})
            avatar_value = str(user_obj.get('avatar') or '').strip()
            mime, binary = _avatar_mime_and_bytes(avatar_value)
            if not binary and avatar_value.startswith('https://'):
                self.send_response(302)
                self.send_header('Location', avatar_value)
                self.send_header('Cache-Control', 'public, max-age=300')
                self.end_headers()
                return
            if not binary:
                fallback_path = os.path.join(BASE_DIR, 'pwa-192.png')
                try:
                    with open(fallback_path, 'rb') as image_file:
                        binary = image_file.read()
                    mime = 'image/png'
                except OSError:
                    binary, mime = b'', 'application/octet-stream'
            self.send_response(200 if binary else 404)
            self.send_header('Content-Type', mime or 'application/octet-stream')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'public, max-age=300')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(binary)))
            self.end_headers()
            if binary:
                self.wfile.write(binary)
            return

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
                'users': {name: _public_user(name, user_obj) for name, user_obj in USERS.items()},
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

        if parsed.path == '/api/push/subscribe':
            username = str(body.get('username') or '').strip().lower().lstrip('@')
            subscription = body.get('subscription')
            if not username or username not in USERS or USERS[username].get('is_channel') or USERS[username].get('is_bot'):
                return self.send_json({'error': 'Аккаунт не найден. Сначала открой Vibegram под своим профилем.'}, 400)
            if not isinstance(subscription, dict):
                return self.send_json({'error': 'Некорректная push-подписка.'}, 400)
            endpoint = str(subscription.get('endpoint') or '').strip()
            keys = subscription.get('keys') if isinstance(subscription.get('keys'), dict) else {}
            p256dh = str(keys.get('p256dh') or '').strip()
            auth = str(keys.get('auth') or '').strip()
            if not endpoint.startswith('https://') or len(endpoint) > 4096 or not p256dh or not auth:
                return self.send_json({'error': 'Подписка содержит некорректные endpoint/keys.'}, 400)
            subscription = {'endpoint': endpoint, 'keys': {'p256dh': p256dh, 'auth': auth}}
            with DB_LOCK:
                # An endpoint belongs to one current account on this browser/device.
                for owner_name, owner_records in list(PUSH_SUBSCRIPTIONS.items()):
                    if owner_name == username or not isinstance(owner_records, list):
                        continue
                    PUSH_SUBSCRIPTIONS[owner_name] = [
                        row for row in owner_records
                        if (row.get('subscription') or {}).get('endpoint') != endpoint
                    ]
                    if not PUSH_SUBSCRIPTIONS[owner_name]:
                        PUSH_SUBSCRIPTIONS.pop(owner_name, None)
                records = PUSH_SUBSCRIPTIONS.setdefault(username, [])
                records = [row for row in records if (row.get('subscription') or {}).get('endpoint') != endpoint]
                if len(records) >= 10:
                    return self.send_json({'error': 'Для этого аккаунта уже сохранено 10 устройств. Удали старую подписку.'}, 429)
                records.append({'subscription': subscription, 'created_at': now})
                PUSH_SUBSCRIPTIONS[username] = records
                save_db()
            return self.send_json({'ok': True, 'subscribed': True, 'devices': len(PUSH_SUBSCRIPTIONS.get(username, []))})

        if parsed.path == '/api/push/unsubscribe':
            username = str(body.get('username') or '').strip().lower().lstrip('@')
            endpoint = str(body.get('endpoint') or '').strip()
            if not endpoint:
                return self.send_json({'error': 'Не указан endpoint подписки.'}, 400)
            removed = 0
            with DB_LOCK:
                target_users = [username] if username else list(PUSH_SUBSCRIPTIONS.keys())
                for owner_name in target_users:
                    current = PUSH_SUBSCRIPTIONS.get(owner_name, [])
                    filtered = [row for row in current if (row.get('subscription') or {}).get('endpoint') != endpoint]
                    removed += len(current) - len(filtered)
                    if filtered:
                        PUSH_SUBSCRIPTIONS[owner_name] = filtered
                    else:
                        PUSH_SUBSCRIPTIONS.pop(owner_name, None)
                save_db()
            return self.send_json({'ok': True, 'removed': removed})

        # Регистрация / сохранение личного профиля с проверкой уникальности @username и выдачей ID
        if parsed.path == '/api/profile':
            old_u = str(body.get('oldUsername') or '').strip().lower().lstrip('@')
            u = str(body.get('username') or '').strip().lower().lstrip('@')
            email = str(body.get('email') or '').strip().lower()
            phone = str(body.get('phone') or '').strip()
            phone_digits = re.sub(r'\D', '', phone)
            if phone and not 7 <= len(phone_digits) <= 15:
                return self.send_json({'error': 'Телефон должен содержать от 7 до 15 цифр.'}, 400)

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
                if old_u in PUSH_SUBSCRIPTIONS:
                    moved_records = list(PUSH_SUBSCRIPTIONS.pop(old_u, []))
                    existing_records = PUSH_SUBSCRIPTIONS.get(u, [])
                    seen_endpoints = {((row.get('subscription') or {}).get('endpoint')) for row in existing_records}
                    existing_records.extend(row for row in moved_records if ((row.get('subscription') or {}).get('endpoint')) not in seen_endpoints)
                    PUSH_SUBSCRIPTIONS[u] = existing_records
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
            try:
                stored_avatar = _store_avatar_value(body.get('avatar'), existing.get('avatar', ''), max_bytes=512 * 1024)
            except ValueError as exc:
                return self.send_json({'error': str(exc)}, 413)
            user_id = existing.get('id')
            if not user_id:
                user_id = assign_user_id(u)
            if u in ('paveldurov', 'vibegram'):
                user_id = 1

            # Public profile channel links are persisted server-side. Accept only
            # channels the profile follows or can manage; never trust arbitrary handles.
            requested_channels = body.get('profile_channels')
            if not isinstance(requested_channels, list):
                requested_channels = existing.get('profile_channels', [])
            eligible_channels = set()
            for channel_handle, channel_obj in USERS.items():
                if not channel_obj.get('is_channel'):
                    continue
                subscribers = channel_obj.get('subscribers', [])
                admins = channel_obj.get('admins', {}) if isinstance(channel_obj.get('admins'), dict) else {}
                if channel_obj.get('owner') == u or u in subscribers or u in admins:
                    eligible_channels.add(channel_handle)
            profile_channels = []
            for channel_handle in requested_channels[:50]:
                handle = str(channel_handle or '').strip().lower().lstrip('@')
                if handle in eligible_channels and handle not in profile_channels:
                    profile_channels.append(handle)

            USERS[u] = {
                'id': user_id,
                'username': u,
                'name': str(body.get('name') or existing.get('name') or u).strip() or u,
                'bio': str(body.get('bio') or existing.get('bio') or 'В Vibegram').strip(),
                'phone': phone or existing.get('phone', '+375 (29) 000-00-00'),
                'email': email or existing.get('email', ''),
                'color': body.get('color') or existing.get('color', '#6c5ce7'),
                # Avatar is server-owned and synced across devices. A newly opened
                # client can submit an empty avatar before it has downloaded the saved one;
                # never let that blank value erase the existing image.
                'avatar': stored_avatar,
                'privacy': body.get('privacy') or existing.get('privacy', 'everyone'),
                'profile_channels': profile_channels,
                'is_premium': bool(existing.get('is_premium', False)),
                'premium_grant_version': existing.get('premium_grant_version'),
                'premium_granted_by': existing.get('premium_granted_by'),
                'premium_granted_at': existing.get('premium_granted_at'),
                'is_team': bool(existing.get('is_team', False)),
                'team_grant_version': existing.get('team_grant_version'),
                'team_granted_by': existing.get('team_granted_by'),
                'team_granted_at': existing.get('team_granted_at'),
                'sticker_id': body.get('sticker_id') or existing.get('sticker_id', 'star'),
                # Verified state may ONLY change in /api/verification/decide after bot review.
                # Ignore any is_verified value supplied by a browser client.
                'is_verified': bool(existing.get('is_verified', False)),
                'verify_category': existing.get('verify_category', 'none'),
                'verify_reason': existing.get('verify_reason', ''),
                'is_channel': False,
                'last_seen': now,
                'typing_to': existing.get('typing_to', ''),
                'typing_time': existing.get('typing_time', 0)
            }
            save_db()
            return self.send_json({'ok': True, 'profile': _public_user(u, USERS[u])})

        # Создание или редактирование Telegram-канала (исправлена валидация и права админов)
        if parsed.path == '/api/channel/save':
            actor = str(body.get('actor') or body.get('owner') or '').strip().lower().lstrip('@')
            old_handle = str(body.get('oldUsername') or '').strip().lower().lstrip('@')
            handle = str(body.get('username') or '').strip().lower().lstrip('@')
            name = str(body.get('name') or '').strip()
            bio = str(body.get('bio') or '').strip()
            color = body.get('color') or '#039be5'
            avatar_input = str(body.get('avatar') or '').strip()
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
                    for profile in USERS.values():
                        linked = profile.get('profile_channels', [])
                        if isinstance(linked, list) and old_handle in linked:
                            profile['profile_channels'] = [handle if item == old_handle else item for item in linked]
                    for m in MESSAGES:
                        if m['sender'] == old_handle: m['sender'] = handle
                        if m['to'] == old_handle: m['to'] = handle
            else:
                if handle in USERS:
                    return self.send_json({'error': f'Канал @{handle} уже существует! Выберите другой @username.'}, 400)

            existing = USERS.get(handle, {})
            try:
                avatar = _store_avatar_value(avatar_input, existing.get('avatar', ''), max_bytes=1024 * 1024)
            except ValueError as exc:
                return self.send_json({'error': str(exc)}, 413)
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
                'is_premium': bool(existing.get('is_premium', False)),
                'premium_grant_version': existing.get('premium_grant_version'),
                'premium_granted_by': existing.get('premium_granted_by'),
                'premium_granted_at': existing.get('premium_granted_at'),
                'is_team': bool(existing.get('is_team', False)),
                'team_grant_version': existing.get('team_grant_version'),
                'team_granted_by': existing.get('team_granted_by'),
                'team_granted_at': existing.get('team_granted_at'),
                'sticker_id': existing.get('sticker_id', 'star'),
                'is_verified': bool(existing.get('is_verified', False)),
                'verify_category': existing.get('verify_category', 'Официальный канал'),
                'verify_reason': existing.get('verify_reason', 'Подлинность канала подтверждена модерацией Vibegram.'),
                'verify_pending': bool(existing.get('verify_pending', False)),
                'last_seen': now
            }
            save_db()
            return self.send_json({'ok': True, 'channel': _public_user(handle, USERS[handle])})

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
                        'avatar': _avatar_public_path(user, user_obj.get('avatar', '')),
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
            if applicant not in USERS or USERS[applicant].get('is_channel'):
                return self.send_json({'error': 'Не удалось определить заявителя'}, 400)

            target_obj = USERS[target]
            # Only the account owner can request a personal badge. A channel owner
            # or admin with promote permission can request a badge for that channel.
            if target_obj.get('is_channel'):
                channel_admin = (target_obj.get('admins') or {}).get(applicant, {})
                if applicant != target_obj.get('owner') and not channel_admin.get('can_promote'):
                    return self.send_json({'error': 'Подать заявку на канал может только владелец или администратор с правом управления'}, 403)
            elif applicant != target:
                return self.send_json({'error': 'Подать заявку на личный аккаунт может только его владелец'}, 403)

            if target_obj.get('verify_pending'):
                return self.send_json({'error': 'Заявка уже ожидает решения в @verify_bot'}, 409)

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
            message_start_index = len(MESSAGES)
            msg_id = str(body.get('msg_id') or '')
            decision = str(body.get('decision') or '')  # 'approve' | 'reject'
            target = str(body.get('target') or '').strip().lower().lstrip('@')
            actor = str(body.get('actor') or '').strip().lower().lstrip('@')

            # The server checks the creator binding; localStorage unlock flags are
            # only a visual convenience and are not accepted as authorization.
            creator = USERS.get(VERIFY_BOT_ADMIN or '', {})
            if decision not in ('approve', 'reject'):
                return self.send_json({'error': 'Недопустимое решение'}, 400)
            if not VERIFY_BOT_ADMIN or actor != VERIFY_BOT_ADMIN or int(creator.get('id') or 0) != 1:
                return self.send_json({'error': 'Рассматривать заявки может только создатель, привязавший @verify_bot с ID 1'}, 403)

            request_message = next((m for m in MESSAGES if str(m.get('id')) == msg_id and m.get('verification_request')), None)
            if not request_message:
                return self.send_json({'error': 'Заявка не найдена'}, 404)
            req = request_message['verification_request']
            if str(req.get('target') or '').lower() != target:
                return self.send_json({'error': 'Цель заявки не совпадает'}, 400)
            if req.get('status') != 'pending':
                return self.send_json({'error': 'Эта заявка уже рассмотрена'}, 409)
            if str(request_message.get('to') or '').lower() != actor:
                return self.send_json({'error': 'Эта заявка не назначена вашему аккаунту в @verify_bot'}, 403)
            if target not in USERS:
                return self.send_json({'error': 'Профиль или канал не найден'}, 404)

            applicant = req.get('applicant', '')
            req['status'] = 'approved' if decision == 'approve' else 'rejected'
            target_name = req.get('target_name', target)
            cat = req.get('category', 'Официальный статус')
            rsn = req.get('reason', 'Подлинность подтверждена создателем Vibegram')

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
            dispatch_push_for_messages(MESSAGES[message_start_index:], self._public_base_url())
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
            message_start_index = len(MESSAGES)
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
                # The admin secret may be supplied only to a privileged bot command.
                # Never persist it in message history or return it to the browser.
                raw_parts = text.strip().split()
                command_name = raw_parts[0].lower() if raw_parts else ''
                if (command_name in ('/premium', '/vip', '/team') and len(raw_parts) >= 4
                        and raw_parts[2].lower() in ('on', 'off')):
                    user_msg['text'] = f"{raw_parts[0]} {raw_parts[1]} {raw_parts[2]} [ключ скрыт]"
                MESSAGES.append(user_msg)

                sender_obj = USERS.get(sender, {})
                sender_id = int(sender_obj.get('id') or 0)
                clean_cmd = text.strip().lower()
                parts = raw_parts
                command = parts[0].lower() if parts else ''
                configured_admin_token = os.environ.get('VIBEGRAM_ADMIN_TOKEN', '').strip()
                supplied_admin_token = parts[3] if len(parts) == 4 else ''
                is_admin_token_valid = bool(configured_admin_token and supplied_admin_token and
                    hmac.compare_digest(supplied_admin_token, configured_admin_token))

                # Premium is server-managed: an ID, username or localStorage flag cannot grant it.
                if command in ('/premium', '/vip'):
                    if len(parts) == 2 and parts[1].lower() == 'request':
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
                    elif len(parts) in (3, 4) and parts[1].lstrip('@').lower() in USERS and parts[2].lower() in ('on', 'off'):
                        target_premium = parts[1].lstrip('@').lower()
                        if len(parts) != 4 or not configured_admin_token:
                            bot_reply = ('⛔ Выдача Premium заблокирована без серверного ключа. '
                                         'Настрой VIBEGRAM_ADMIN_TOKEN в Render и укажи этот ключ последним аргументом команды.')
                        elif not is_admin_token_valid:
                            bot_reply = '⛔ Неверный ключ администратора. Premium не изменён.'
                        else:
                            enabled = parts[2].lower() == 'on'
                            target_obj = USERS[target_premium]
                            target_obj['is_premium'] = enabled
                            if enabled:
                                target_obj['premium_grant_version'] = 1
                                target_obj['premium_granted_by'] = 'admin_token'
                                target_obj['premium_granted_at'] = now
                                target_obj['sticker'] = target_obj.get('sticker') or 'star'
                            else:
                                target_obj.pop('premium_grant_version', None)
                                target_obj.pop('premium_granted_by', None)
                                target_obj.pop('premium_granted_at', None)
                            bot_reply = f"{'✅ Vibegram Premium выдан' if enabled else 'ℹ️ Vibegram Premium снят'} для @{target_premium}."
                            MESSAGES.append({
                                'id': f"msg_{now + 1}_{len(MESSAGES)}_premium_notice",
                                'sender': 'verify_bot', 'to': target_premium,
                                'text': f"{'⭐ Вам выдан Vibegram Premium!' if enabled else 'Vibegram Premium для вашего аккаунта отключён.'}",
                                'time': now + 1, 'read': False, 'reactions': {}
                            })
                            save_db()
                    else:
                        bot_reply = ('Команды Premium:\n/premium request — запросить Premium\n'
                                     '/premium @username on <ADMIN_TOKEN> — выдать\n'
                                     '/premium @username off <ADMIN_TOKEN> — снять\n'
                                     'Без VIBEGRAM_ADMIN_TOKEN на сервере выдача запрещена.')
                # Отдельная звезда команды Vibegram; доступ только по серверному ключу.
                elif command == '/team':
                    if len(parts) == 2 and parts[1].lower() == 'request':
                        bot_reply = ('⭐ Заявка на звезду команды принята. Выдача возможна только администратором '
                                     'Vibegram через серверный ключ.')
                        creator_username = VERIFY_BOT_ADMIN
                        if not creator_username:
                            creator_username = next((name for name, obj in USERS.items()
                                if not obj.get('is_channel') and not obj.get('is_bot') and int(obj.get('id') or obj.get('uid') or 0) == 1), None)
                        if creator_username and creator_username in USERS:
                            MESSAGES.append({
                                'id': f"msg_{now + 1}_{len(MESSAGES)}_team_request",
                                'sender': 'verify_bot', 'to': creator_username,
                                'text': f"⭐ ЗАЯВКА НА ЗВЕЗДУ КОМАНДЫ\nПользователь: @{sender} (ID: {sender_id})\nКоманда выдачи: /team @{sender} on <ADMIN_TOKEN>",
                                'time': now + 1, 'read': False, 'reactions': {},
                                'team_request': {'applicant': sender, 'status': 'pending'}
                            })
                    elif len(parts) in (3, 4) and parts[1].lstrip('@').lower() in USERS and parts[2].lower() in ('on', 'off'):
                        target_team = parts[1].lstrip('@').lower()
                        if len(parts) != 4 or not configured_admin_token:
                            bot_reply = ('⛔ Выдача звезды команды заблокирована без серверного ключа. '
                                         'Настрой VIBEGRAM_ADMIN_TOKEN в Render и укажи его последним аргументом.')
                        elif not is_admin_token_valid:
                            bot_reply = '⛔ Неверный ключ администратора. Статус команды не изменён.'
                        else:
                            enabled = parts[2].lower() == 'on'
                            target_obj = USERS[target_team]
                            target_obj['is_team'] = enabled
                            if enabled:
                                target_obj['team_grant_version'] = 1
                                target_obj['team_granted_by'] = 'admin_token'
                                target_obj['team_granted_at'] = now
                            else:
                                target_obj.pop('team_grant_version', None)
                                target_obj.pop('team_granted_by', None)
                                target_obj.pop('team_granted_at', None)
                            bot_reply = f"{'✅ Звезда команды Vibegram выдана' if enabled else 'ℹ️ Звезда команды Vibegram снята'} для @{target_team}."
                            MESSAGES.append({
                                'id': f"msg_{now + 1}_{len(MESSAGES)}_team_notice",
                                'sender': 'verify_bot', 'to': target_team,
                                'text': f"{'⭐ Вам выдана отдельная звезда команды Vibegram!' if enabled else 'Звезда команды Vibegram снята.'}",
                                'time': now + 1, 'read': False, 'reactions': {}
                            })
                            save_db()
                    else:
                        bot_reply = ('Команды звезды команды Vibegram:\n/team request — подать заявку\n'
                                     '/team @username on <ADMIN_TOKEN> — выдать\n'
                                     '/team @username off <ADMIN_TOKEN> — снять.')
                # Авторизация Создателя в боте по вводу ID "1" или "/id 1".
                # It remains separate from badge grants; ID alone can no longer grant Premium/team status.
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
                dispatch_push_for_messages(MESSAGES[message_start_index:], self._public_base_url())
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
            dispatch_push_for_messages(MESSAGES[message_start_index:], self._public_base_url())
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