import os
import sqlite3
import time
import threading
import html
import random
import logging
import hashlib
import requests
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

TZ_KG = ZoneInfo("Asia/Bishkek")  # Кыргызстан UTC+6

def now_kg():
    """Текущее время по Кыргызстану (Бишкек)."""
    return datetime.now(TZ_KG)

from flask import Flask, send_from_directory, request, jsonify
import telebot
from telebot import types

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Токен бота
TOKEN = os.environ.get("TOKEN_REF") or os.environ.get("TOKEN")
if not TOKEN or len(TOKEN) < 40:
    logger.error("ТОКЕН НЕ НАЙДЕН!")
    raise SystemExit("No token")

MAIN_ADMIN = 8957913298
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1905")
OPERATOR_ID = int(os.environ.get("OPERATOR_ID", "8992968778"))
SUPPORT = os.environ.get("SUPPORT", "bigpayoper_bot")

CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "BigPayNews")
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@BigPayNews")

# --- ДАННЫЕ КАССЫ И API BIGPAY ---
CASHDESK_ID = int(os.environ.get("CASHDESK_ID", 1008610))
CASHIER_PASS = os.environ.get("CASHIER_PASS", "JetNurs009_")
HASH_KEY = os.environ.get("HASH_KEY", "6aaf15bcc53bd241f4f2eb7cd20aaa4136307fc1019813b853a4ef35fc5fb163")

class CashdeskBotAPI:
    BASE_URL = "https://partners.servcul.com/CashdeskBotAPI"

    def __init__(self, hash_key: str, cashierpass: str, cashdesk_id: int):
        self.hash = hash_key
        self.cashierpass = cashierpass
        self.cashdesk_id = cashdesk_id
        self.session = requests.Session()

    @staticmethod
    def _md5(text: str) -> str:
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def _make_confirm(self, value: str | int) -> str:
        return self._md5(f"{value}:{self.hash}")

    def find_user(self, user_id: int) -> dict:
        confirm = self._make_confirm(user_id)
        part1 = self._sha256(f"hash={self.hash}&userid={user_id}&cashdeskid={self.cashdesk_id}")
        part2 = self._md5(f"userid={user_id}&cashierpass={self.cashierpass}&hash={self.hash}")
        sign = self._sha256(part1 + part2)
        url = f"{self.BASE_URL}/Users/{user_id}"
        response = self.session.get(url, params={"confirm": confirm, "cashdeskId": self.cashdesk_id}, headers={"sign": sign}, timeout=15)
        response.raise_for_status()
        return response.json()

    def deposit(self, user_id: int, amount: float, lng: str = "ru") -> dict:
        confirm = self._make_confirm(user_id)
        part1 = self._sha256(f"hash={self.hash}&lng={lng}&userid={user_id}")
        part2 = self._md5(f"summa={amount}&cashierpass={self.cashierpass}&cashdeskid={self.cashdesk_id}")
        sign = self._sha256(part1 + part2)
        url = f"{self.BASE_URL}/Deposit/{user_id}/Add"
        payload = {"cashdeskId": self.cashdesk_id, "lng": lng, "summa": amount, "confirm": confirm}
        
        response = self.session.post(url, json=payload, headers={"sign": sign, "Content-Type": "application/json"}, timeout=15)
        
        if response.status_code == 401:
            part1_alt = self._sha256(f"hash={self.hash}&lng={lng}&UserId={user_id}")
            sign_alt = self._sha256(part1_alt + part2)
            response = self.session.post(url, json=payload, headers={"sign": sign_alt, "Content-Type": "application/json"}, timeout=15)
            
        response.raise_for_status()
        return response.json()

    def payout(self, user_id: int, code: str, lng: str = "ru") -> dict:
        confirm = self._make_confirm(user_id)
        part1 = self._sha256(f"hash={self.hash}&lng={lng}&userId={user_id}")
        part2 = self._md5(f"code={code}&cashierpass={self.cashierpass}&cashdeskid={self.cashdesk_id}")
        sign = self._sha256(part1 + part2)
        url = f"{self.BASE_URL}/Deposit/{user_id}/Payout"
        payload = {"cashdeskId": self.cashdesk_id, "lng": lng, "code": code, "confirm": confirm}
        response = self.session.post(url, json=payload, headers={"sign": sign, "Content-Type": "application/json"}, timeout=15)
        response.raise_for_status()
        return response.json()

api = CashdeskBotAPI(HASH_KEY, CASHIER_PASS, CASHDESK_ID)

def support_url():
    if SUPPORT and str(SUPPORT).strip():
        u = str(SUPPORT).lstrip("@").strip()
        return f"https://t.me/{u}"
    return f"tg://user?id={OPERATOR_ID}"

BOT_NAME = "BigPay"
MIN_DEPOSIT = 50
MAX_DEPOSIT = 50000
BOT_USERNAME = "bigpayoper_bot"

WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
PAY_LINK = os.environ.get(
    "PAY_LINK",
    "https://api.dengi.o.kg/#00020101021132680012p2p.dengi.kg01048580111258120233520910129965023532261202111302123408%D0%9D%D0%A3%D0%A0%D0%AD%D0%9B%20%D0%9A.5204739953034175401059060%21Bank63044CEE",
)

bot = telebot.TeleBot(TOKEN, parse_mode='HTML')
app = Flask(__name__)

temp_data = {}
payment_timers = {}
DB_NAME = 'bot.db'

# --- ПРЕМИУМ ЭМОДЗИ ---
E_SETTINGS = '<tg-emoji emoji-id="6032742198179532882">⚙️</tg-emoji>'
E_DEPOSIT = '<tg-emoji emoji-id="6039420807900303010">📥</tg-emoji>'
E_WITHDRAW = '<tg-emoji emoji-id="6043874504302661409">📤</tg-emoji>'
E_BELL = '<tg-emoji emoji-id="6039486778597970865">🔔</tg-emoji>'
E_CHECK = '<tg-emoji emoji-id="5429381339851796035">✅</tg-emoji>'
E_CROSS = '<tg-emoji emoji-id="5273914604752216432">❌</tg-emoji>'
E_LIGHTNING = '<tg-emoji emoji-id="5199785165735367039">⚡️</tg-emoji>'
E_LINK = '<tg-emoji emoji-id="5859270552153231910">🔗</tg-emoji>'
E_STORE = '<tg-emoji emoji-id="5854847234054558104">🏪</tg-emoji>'
E_MONEY = '<tg-emoji emoji-id="5863998851584627334">💰</tg-emoji>'
E_CARD = '<tg-emoji emoji-id="5472250091332993630">💳</tg-emoji>'
E_CASH = '<tg-emoji emoji-id="5201873447554145566">💵</tg-emoji>'
E_ROCKET = '<tg-emoji emoji-id="5201691993775818138">🚀</tg-emoji>'
E_SUPPORT = '<tg-emoji emoji-id="5971889748615105853">💻</tg-emoji>'

def safe_html(text):
    return html.escape(str(text)) if text else ""

def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=15, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL;')
    return conn

def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
                        chat_id INTEGER PRIMARY KEY, join_date TEXT, balance REAL DEFAULT 0.0,
                        username TEXT, full_name TEXT, is_banned INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins (chat_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS requisites (id INTEGER PRIMARY KEY AUTOINCREMENT, req_text TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS deposits (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
                        account_id TEXT, photo_id TEXT, status TEXT, date TEXT, timestamp INTEGER, pay_method TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS qr_codes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, file_id TEXT, date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS withdraw_images (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, file_id TEXT, date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS support_tickets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER, username TEXT, message TEXT, photo_id TEXT,
                        status TEXT DEFAULT 'new', created TEXT, updated TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS support_replies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER,
                        from_admin INTEGER, message TEXT, created TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, elqr_photo TEXT,
                        id_photo TEXT, sms_code TEXT, status TEXT, date TEXT)''')
        
        try:
            c.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
        except Exception:
            pass

        try:
            c.execute("ALTER TABLE deposits ADD COLUMN pay_method TEXT")
        except Exception:
            pass

        c.execute('INSERT OR IGNORE INTO admins (chat_id) VALUES (?)', (MAIN_ADMIN,))
        c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES ("bot_active", "True")')
        c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES ("auto_deposit", "True")')
        c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES ("deposits_enabled", "True")')
        c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES ("withdrawals_enabled", "True")')
        
        # Дефолтные реквизиты (Мбанк, О!Банк, Бакай)
        default_reqs = "🏦 РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ:\n\n🔹 MBank (Мбанк): уточняйте в боте\n🔹 O!Bank (О!Банк): по QR-коду\n🔹 Bakai Bank (Бакай): по QR-коду"
        c.execute('INSERT OR IGNORE INTO requisites (id, req_text) VALUES (1, ?)', (default_reqs,))
        
        conn.commit()
    logger.info("База данных готова")

def is_user_banned(user_id):
    with get_db() as conn:
        row = conn.execute('SELECT is_banned FROM users WHERE chat_id = ?', (user_id,)).fetchone()
        return row and row[0] == 1

def set_ban_status(user_id, banned: bool):
    with get_db() as conn:
        conn.execute('UPDATE users SET is_banned = ? WHERE chat_id = ?', (1 if banned else 0, user_id))
        conn.commit()

def is_auto_deposit_active():
    with get_db() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key = "auto_deposit"').fetchone()
        return True if row and row[0] == 'True' else False

def set_auto_deposit_active(status: bool):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES ("auto_deposit", ?)', (str(status),))
        conn.commit()

def is_deposits_enabled():
    with get_db() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key = "deposits_enabled"').fetchone()
        return True if row is None or row[0] == 'True' else False

def set_deposits_enabled(status: bool):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES ("deposits_enabled", ?)', (str(status),))
        conn.commit()

def is_withdrawals_enabled():
    with get_db() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key = "withdrawals_enabled"').fetchone()
        return True if row is None or row[0] == 'True' else False

def set_withdrawals_enabled(status: bool):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES ("withdrawals_enabled", ?)', (str(status),))
        conn.commit()

def save_requisites(text):
    with get_db() as conn:
        conn.execute('DELETE FROM requisites')
        conn.execute('INSERT INTO requisites (req_text) VALUES (?)', (text,))
        conn.commit()

def get_requisites():
    with get_db() as conn:
        row = conn.execute('SELECT req_text FROM requisites ORDER BY id DESC LIMIT 1').fetchone()
        return row[0] if row else "🏦 Доступные банки: MBank, O!Bank, Bakai Bank."

def is_bot_active():
    with get_db() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key = "bot_active"').fetchone()
        return True if row is None else row[0] == 'True'

def set_bot_active(status: bool):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES ("bot_active", ?)', (str(status),))
        conn.commit()

def get_admins():
    with get_db() as conn:
        admins = [r[0] for r in conn.execute('SELECT chat_id FROM admins').fetchall()]
        if MAIN_ADMIN not in admins:
            admins.append(MAIN_ADMIN)
        return admins

def add_user(chat_id, username=None, full_name=None):
    with get_db() as conn:
        exists = conn.execute('SELECT 1 FROM users WHERE chat_id = ?', (chat_id,)).fetchone()
        if not exists:
            conn.execute(
                'INSERT INTO users (chat_id, join_date, username, full_name) VALUES (?, ?, ?, ?)',
                (chat_id, now_kg().strftime("%d.%m.%Y %H:%M"), username or "", full_name or ""),
            )
            conn.commit()
            return True
        if username is not None or full_name is not None:
            conn.execute(
                'UPDATE users SET username = COALESCE(?, username), full_name = COALESCE(?, full_name) WHERE chat_id = ?',
                (username, full_name, chat_id),
            )
            conn.commit()
        return False

def user_label(user_id):
    with get_db() as conn:
        row = conn.execute('SELECT username, full_name FROM users WHERE chat_id = ?', (user_id,)).fetchone()
    if not row:
        return f"<code>{user_id}</code>"
    uname, fname = row[0] or "", row[1] or ""
    parts = [f"<code>{user_id}</code>"]
    if uname:
        parts.append(f"@{safe_html(uname)}")
    if fname:
        parts.append(safe_html(fname))
    return " · ".join(parts)

def touch_user(msg):
    u = msg.from_user
    add_user(u.id, username=u.username or "", full_name=(u.full_name or u.first_name or ""))

def check_subscription(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_ID, user_id)
        if member.status in ['member', 'administrator', 'creator']:
            return True
    except Exception as e:
        logger.warning(f"Ошибка проверки подписки: {e}")
        return False
    return False

def create_ticket(user_id, username, message, photo_id=None):
    now = now_kg().strftime("%d.%m.%Y %H:%M")
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO support_tickets (user_id, username, message, photo_id, status, created, updated)
                     VALUES (?, ?, ?, ?, 'new', ?, ?)""",
                  (user_id, username or "", message or "", photo_id, now, now))
        tid = c.lastrowid
        conn.commit()
        return tid

def get_ticket(tid):
    with get_db() as conn:
        return conn.execute("SELECT * FROM support_tickets WHERE id = ?", (tid,)).fetchone()

def set_ticket_status(tid, status):
    now = now_kg().strftime("%d.%m.%Y %H:%M")
    with get_db() as conn:
        conn.execute("UPDATE support_tickets SET status = ?, updated = ? WHERE id = ?", (status, now, tid))
        conn.commit()

def add_ticket_reply(tid, from_admin, message):
    now = now_kg().strftime("%d.%m.%Y %H:%M")
    with get_db() as conn:
        conn.execute("""INSERT INTO support_replies (ticket_id, from_admin, message, created)
                       VALUES (?, ?, ?, ?)""", (tid, 1 if from_admin else 0, message, now))
        conn.commit()

def list_tickets(status=None, limit=20):
    with get_db() as conn:
        if status:
            return conn.execute("SELECT id, user_id, username, message, status, created FROM support_tickets WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        return conn.execute("SELECT id, user_id, username, message, status, created FROM support_tickets ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

def notify_operators_ticket(tid, user_id, username, message, photo_id=None):
    uname = f"@{username}" if username else user_label(user_id)
    text = (
        f"🆘 <b>НОВОЕ ОБРАЩЕНИЕ #{tid}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>От:</b> {uname}\n"
        f"💬 <b>Текст:</b> {safe_html(message) if message else '📎 Фотография'}"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔄 В работу", callback_data=f"tkt_work_{tid}"),
        types.InlineKeyboardButton("✍️ Ответить", callback_data=f"tkt_reply_{tid}"),
        types.InlineKeyboardButton("❌ Закрыть", callback_data=f"tkt_close_{tid}")
    )
    targets = set(get_admins())
    targets.add(MAIN_ADMIN)
    targets.add(OPERATOR_ID)
    for oid in targets:
        try:
            if photo_id:
                send_media_bulletproof(oid, photo_id, caption=text, reply_markup=kb)
            else:
                bot.send_message(oid, text, reply_markup=kb)
        except Exception as e:
            logger.warning(f"ticket notify {oid}: {e}")

def get_all_users():
    with get_db() as conn:
        return [r[0] for r in conn.execute('SELECT chat_id FROM users').fetchall()]

def add_admin(chat_id):
    with get_db() as conn:
        conn.execute('INSERT OR IGNORE INTO admins (chat_id) VALUES (?)', (chat_id,))
        conn.commit()

def remove_admin(chat_id):
    with get_db() as conn:
        conn.execute('DELETE FROM admins WHERE chat_id = ?', (chat_id,))
        conn.commit()

def add_deposit(user_id, amount, account_id, photo_id, pay_method="QR-код"):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO deposits (user_id, amount, account_id, photo_id, status, date, timestamp, pay_method)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (user_id, amount, account_id, photo_id, 'pending',
                   now_kg().strftime("%d.%m.%Y %H:%M:%S"), int(time.time()), pay_method))
        dep_id = c.lastrowid
        conn.commit()
        return dep_id

def update_deposit_status(dep_id, status):
    with get_db() as conn:
        conn.execute('UPDATE deposits SET status = ? WHERE id = ?', (status, dep_id))
        conn.commit()

def get_deposit_status(dep_id):
    with get_db() as conn:
        row = conn.execute('SELECT status FROM deposits WHERE id = ?', (dep_id,)).fetchone()
        return row[0] if row else None

def add_withdrawal(user_id, elqr, id_photo, code):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO withdrawals (user_id, elqr_photo, id_photo, sms_code, status, date)
                     VALUES (?, ?, ?, ?, ?, ?)''',
                  (user_id, elqr, id_photo, code, 'completed', now_kg().strftime("%d.%m.%Y %H:%M")))
        w_id = c.lastrowid
        conn.commit()
        return w_id

def get_withdrawal_status(w_id):
    with get_db() as conn:
        row = conn.execute('SELECT status FROM withdrawals WHERE id = ?', (w_id,)).fetchone()
        return row[0] if row else None

def get_pending_deposits():
    with get_db() as conn:
        return conn.execute('SELECT id, user_id, amount, account_id, photo_id, date, timestamp, pay_method FROM deposits WHERE status="pending"').fetchall()

def save_qr(file_id):
    with get_db() as conn:
        conn.execute('DELETE FROM qr_codes')
        conn.execute('UPDATE deposits SET photo_id = NULL WHERE status = "pending"')
        conn.execute('INSERT INTO qr_codes (file_id, date) VALUES (?, ?)', (file_id, now_kg().strftime("%d.%m.%Y %H:%M")))
        conn.commit()

def get_last_qr():
    with get_db() as conn:
        row = conn.execute('SELECT file_id FROM qr_codes ORDER BY id DESC LIMIT 1').fetchone()
        return row[0] if row else None

def save_withdraw_image(file_id):
    with get_db() as conn:
        conn.execute('DELETE FROM withdraw_images')
        conn.execute('INSERT INTO withdraw_images (file_id, date) VALUES (?, ?)', (file_id, now_kg().strftime("%d.%m.%Y %H:%M")))
        conn.commit()

def get_withdraw_image():
    with get_db() as conn:
        row = conn.execute('SELECT file_id FROM withdraw_images ORDER BY id DESC LIMIT 1').fetchone()
        return row[0] if row else None

def get_stats():
    with get_db() as conn:
        c = conn.cursor()
        users = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        pending = c.execute('SELECT COUNT(*) FROM deposits WHERE status="pending"').fetchone()[0]
        total = c.execute('SELECT SUM(amount) FROM deposits WHERE status="approved"').fetchone()[0] or 0
        return {'users': users, 'pending': pending, 'total': total}

init_db()

def send_msg(chat_id, text, reply_markup=None):
    try:
        return bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"send_msg [{chat_id}]: {e}")
        return None

def send_media_bulletproof(chat_id, file_id, caption=None, reply_markup=None):
    if not file_id:
        return send_msg(chat_id, caption, reply_markup) if caption else None
    try:
        return bot.send_photo(chat_id, file_id, caption=caption, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"send_photo failed: {e}")
        try:
            return bot.send_document(chat_id, file_id, caption=caption, reply_markup=reply_markup, parse_mode='HTML')
        except Exception as e2:
            logger.warning(f"send_document failed: {e2}")
            if caption:
                return send_msg(chat_id, caption, reply_markup)
            return None

def cancel_payment(user_id):
    temp_data.pop(user_id, None)
    payment_timers.pop(user_id, None)
    try:
        send_msg(user_id, f"⌛ <b>Время оплаты истекло</b>\n\nЗаявка автоматически отменена. Попробуйте снова.")
    except Exception:
        pass

# --- КЛАВИАТУРЫ ---

def main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("↩️ Пополнить", "↩️ Вывести")
    markup.add("🏦 Реквизиты банков", "📖 Инструкция")
    markup.add("👨‍💻 Поддержка")
    return markup

def pay_methods_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("💳 Оплата по QR-коду (MBank, O!Bank, Bakai)")
    markup.add("↩️ Отмена")
    return markup

def cancel_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("↩️ Отмена")
    return markup

def admin_keyboard():
    active = is_bot_active()
    auto_active = is_auto_deposit_active()
    dep_on = is_deposits_enabled()
    with_on = is_withdrawals_enabled()
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("📋 Заявки", "📩 Обращения")
    markup.add("📊 Статистика", "💳 Реквизиты")
    markup.add("⚙️ Авто-API: ВКЛ" if auto_active else "⚙️ Авто-API: ВЫКЛ")
    markup.add("🟢 Пополнение: ВКЛ" if dep_on else "🔴 Пополнение: ВЫКЛ")
    markup.add("🟢 Вывод: ВКЛ" if with_on else "🔴 Вывод: ВЫКЛ")
    markup.add("🚫 Забанить", "✅ Разбанить")
    markup.add("🖼 Изменить QR", "🖼 Инструкция вывода")
    markup.add("➕ Админ", "➖ Удалить админа")
    markup.add("📢 Рассылка")
    markup.add("🔴 ВЫКЛ" if active else "🟢 ВКЛ")
    markup.add("🔙 Главное меню")
    return markup

@bot.message_handler(commands=['start'])
def start(msg):
    chat_id = msg.chat.id
    if is_user_banned(chat_id):
        send_msg(chat_id, f"❌ <b>Доступ ограничен.</b> Вы заблокированы администратором.")
        return

    if not check_subscription(chat_id):
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("✔️ Подписаться", url=CHANNEL_URL),
            types.InlineKeyboardButton("🔑 Проверить", callback_data="check_sub")
        )
        send_msg(chat_id, "Прежде чем продолжить, подпишись на канал", reply_markup=markup)
        return

    start_welcome(msg)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def callback_check_sub(call):
    chat_id = call.message.chat.id
    if check_subscription(chat_id):
        bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
        try: bot.delete_message(chat_id, call.message.message_id)
        except Exception: pass
        
        class FakeMsg:
            def __init__(self, chat, user):
                self.chat = chat
                self.from_user = user
                self.text = "/start"
        
        start_welcome(FakeMsg(call.message.chat, call.from_user))
    else:
        bot.answer_callback_query(call.id, "❌ Вы еще не подписались на канал!", show_alert=True)

def start_welcome(msg):
    chat_id = msg.chat.id
    bot.clear_step_handler_by_chat_id(chat_id)
    if chat_id in payment_timers:
        try: payment_timers[chat_id].cancel()
        except Exception: pass
        payment_timers.pop(chat_id, None)
    temp_data[chat_id] = {}

    if not is_bot_active() and msg.from_user.id not in get_admins() and msg.from_user.id != MAIN_ADMIN:
        send_msg(chat_id, f"🔴 <b>Бот находится на техническом обслуживании.</b>\nПопробуйте позже.")
        return

    add_user(chat_id, username=msg.from_user.username or "", full_name=msg.from_user.full_name or msg.from_user.first_name or "")
    name = safe_html(msg.from_user.first_name or "Пользователь")
    
    text = (
        f"{E_ROCKET} Добро пожаловать в платежный сервис <b>BigPay</b>, <b>{name}</b> !\n\n"
        f"{E_LIGHTNING} Быстрое пополнение и вывод 1XBET:\n"
        f" • Поддерживаем <b>MBank</b>, <b>O!Bank</b>, <b>Bakai Bank</b>\n"
        f" • Мгновенное зачисление по ID\n\n"
        f"{E_SUPPORT} Круглосуточная поддержка:\n"
        f"<a href=\"{support_url()}\">@{SUPPORT.lstrip('@')}</a>\n\n"
        f"{E_STORE} Финансовая безопасность ваших средств гарантирована!"
    )
    send_msg(chat_id, text, reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: m.text == "🔙 Главное меню" or m.text == "🔙 Назад" or "Отмена" in (m.text or ""))
def back_to_main(msg):
    start(msg)

@bot.message_handler(func=lambda m: m.text in ["🏦 Реквизиты банков", "Реквизиты банков"])
def banks_requisites_handler(msg):
    if is_user_banned(msg.chat.id): return
    reqs = get_requisites()
    text = (
        f"🏦 <b>АКТУАЛЬНЫЕ РЕКВИЗИТЫ И БАНКИ КЫРГЫЗСТАНА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"💳 <b>Доступные банки для переводов:</b>\n"
        f"• 🟢 <b>MBank (Мбанк)</b>\n"
        f"• 🟠 <b>O!Bank (О!Банк)</b>\n"
        f"• 🔵 <b>Bakai Bank (Бакай)</b>\n\n"
        f"{safe_html(reqs)}"
    )
    send_msg(msg.chat.id, text, reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: m.text in ["📖 Инструкция", "Инструкция"])
def instruction_handler(msg):
    if is_user_banned(msg.chat.id): return
    text = (
        f"📖 <b>ИНСТРУКЦИЯ ПО ИСПОЛЬЗОВАНИЮ (BigPay / 1XBET)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"{E_CARD} <b>Как пополнить счет:</b>\n"
        f"1️⃣ Нажмите кнопку «Пополнить».\n"
        f"2️⃣ Выберите способ оплаты через поддерживаемые банки (MBank, O!Bank, Bakai).\n"
        f"3️⃣ Укажите ваш <b>ID 1XBET</b> (система найдет ваше имя).\n"
        f"4️⃣ Укажите сумму пополнения.\n"
        f"5️⃣ Оплатите по QR-коду и отправьте скриншот чека в чат.\n\n"
        f"{E_MONEY} <b>Как вывести средства:</b>\n"
        f"1️⃣ В приложении 1XBET перейдите: <i>Вывести со счета ➞ Наличные</i>.\n"
        f"2️⃣ Укажите город и подтвердите запрос, чтобы получить код.\n"
        f"3️⃣ В боте нажмите «Вывести», отправьте скриншот или QR кошелька, введите ID и полученный код."
    )
    send_msg(msg.chat.id, text, reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: m.text in ["👤 Поддержка", "👨‍💻 Поддержка", "Поддержка"])
def support_handler(msg):
    if is_user_banned(msg.chat.id): return
    text = (
        f"👨‍💻 <b>СЛУЖБА ПОДДЕРЖКИ BigPay</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"Задайте ваш вопрос текстом или отправьте фото/чек проблемы прямо в этот чат.\n\n"
        f"📩 Прямой контакт оператора: <a href=\"{support_url()}\">@{SUPPORT.lstrip('@')}</a>"
    )
    send_msg(msg.chat.id, text, reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, support_receive_message)

def support_receive_message(msg):
    if not msg.text or msg.text.startswith("/start") or "Отмена" in msg.text:
        start(msg); return
    photo_id = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document and (getattr(msg.document, "mime_type", None) or "").startswith("image/") else None)
    text = (msg.caption or msg.text or "").strip()
    if not photo_id and len(text) < 2:
        send_msg(msg.chat.id, "❌ Пожалуйста, напишите подробный текст или прикрепите скриншот:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, support_receive_message)
        return
    if not text and photo_id: text = "📎 Скриншот"
    touch_user(msg)
    uid = msg.from_user.id
    uname = msg.from_user.username or ""
    tid = create_ticket(uid, uname, text, photo_id=photo_id)
    notify_operators_ticket(tid, uid, uname, text, photo_id=photo_id)
    send_msg(msg.chat.id, f"✅ <b>Запрос #{tid} отправлен оператору!</b>\n\nОжидайте ответа в этом чате.", reply_markup=main_keyboard())

# --- ПОПОЛНЕНИЕ СЧЕТА С ПОИСКОМ ИГРОКА (1XBET) ---

@bot.message_handler(func=lambda m: "Пополнить" in (m.text or ""))
def deposit_start(msg):
    if is_user_banned(msg.chat.id): return
    if not is_deposits_enabled() and msg.from_user.id not in get_admins() and msg.from_user.id != MAIN_ADMIN:
        send_msg(msg.chat.id, "⚠️ <b>Пополнения временно отключены администратором.</b> Попробуйте позже.")
        return
    if not is_bot_active() and msg.from_user.id not in get_admins() and msg.from_user.id != MAIN_ADMIN:
        send_msg(msg.chat.id, "🔴 Бот на техническом обслуживании.")
        return
    
    send_msg(msg.chat.id, f"💳 <b>Выберите метод пополнения (MBank, O!Bank, Bakai):</b>", reply_markup=pay_methods_keyboard())
    bot.register_next_step_handler(msg, choose_pay_method)

def choose_pay_method(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    
    text = msg.text.strip().lower()
    if "qr" in text or "код" in text or "💳" in text or "mbank" in text or "o!" in text or "бакай" in text:
        pay_method = "QR-код (MBank/O!/Bakai)"
    else:
        send_msg(msg.chat.id, "❌ Пожалуйста, выберите способ пополнения из меню ниже:", reply_markup=pay_methods_keyboard())
        bot.register_next_step_handler(msg, choose_pay_method)
        return

    temp_data[msg.chat.id] = {"platform": "1XBET", "pay_method": pay_method, "action": "deposit"}
    send_msg(msg.chat.id, "🆔 <b>Введите ваш ID 1XBET:</b>", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, get_account_id)

def get_account_id(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    
    acc_number = msg.text.strip()
    if not acc_number.isdigit():
        send_msg(msg.chat.id, "❌ ID должен состоять только из цифр. Введите корректный ID 1XBET:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_account_id)
        return

    sent_msg = send_msg(msg.chat.id, "🔍 <i>Идет проверка игрока в системе 1XBET...</i>")
    
    try:
        data = api.find_user(int(acc_number))
        if sent_msg:
            try: bot.delete_message(msg.chat.id, sent_msg.message_id)
            except Exception: pass

        player_name = data.get('name') or data.get('Name') or data.get('fio') or "Игрок найден"
        temp_data.setdefault(msg.chat.id, {})["account_id"] = f"1XBET | {acc_number}"
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        markup.add("Да", "Нет")
        
        confirm_text = f"👤 <b>Игрок найден:</b> {safe_html(player_name)}\n\nЭто ваш аккаунт?"
        send_msg(msg.chat.id, confirm_text, reply_markup=markup)
        bot.register_next_step_handler(msg, confirm_player_step)

    except Exception as e:
        if sent_msg:
            try: bot.delete_message(msg.chat.id, sent_msg.message_id)
            except Exception: pass
        send_msg(msg.chat.id, f"❌ <b>Игрок не найден или ошибка API.</b>\nПроверьте ID и введите заново:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_account_id)

def confirm_player_step(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    
    if msg.text.strip().capitalize() == "Да":
        send_msg(msg.chat.id, f"{E_MONEY} <b>Введите сумму пополнения:</b>\n<i>(От {MIN_DEPOSIT} до {MAX_DEPOSIT} сом)</i>", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_amount)
    else:
        send_msg(msg.chat.id, "Введите правильный ID 1XBET:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_account_id)

def get_amount(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    try:
        base = float(msg.text.replace(',', '.'))
    except Exception:
        send_msg(msg.chat.id, "❌ Пожалуйста, введите корректную сумму числом:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_amount); return

    if not (MIN_DEPOSIT <= base <= MAX_DEPOSIT):
        send_msg(msg.chat.id, f"❌ Сумма должна быть от <b>{MIN_DEPOSIT}</b> до <b>{MAX_DEPOSIT}</b> сом!", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_amount); return

    final = round(base + round(random.randint(10, 99) / 100.0, 2), 2)
    user_id = msg.chat.id
    account_id = temp_data.get(user_id, {}).get("account_id", "Не указан")
    pay_method = temp_data.get(user_id, {}).get("pay_method", "QR-код")
    temp_data[user_id]["amount"] = final

    qr = get_last_qr()
    if qr:
        send_media_bulletproof(user_id, qr, caption=f"📸 <b>Оплата через MBank / O!Bank / Bakai</b>\nСумма: <b>{final:.2f} сом</b>")

    banks = types.InlineKeyboardMarkup(row_width=1)
    banks.add(types.InlineKeyboardButton(f"🔗 Оплатить по QR-ссылке", url=PAY_LINK))

    text = (
        f"🧾 <b>СЧЕТ НА ОПЛАТУ (BigPay / 1XBET)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E_CASH} <b>Сумма к оплате:</b> <code>{final:.2f}</code> KGS\n"
        f"🆔 <b>Счет игрока:</b> <code>{safe_html(account_id)}</code>\n"
        f"🏦 <b>Банки:</b> MBank, O!Bank, Bakai Bank\n"
        f"⏱ <b>Время на оплату:</b> 5 минут\n\n"
        f"⚠️ <i>Переводите ТОЧНУЮ сумму с копейками через ваш банк!</i>\n\n"
        f"После перевода отправьте 📸 <b>скриншот чека</b> в этот чат."
    )
    send_msg(user_id, text, reply_markup=banks)
    send_msg(user_id, "⏳ Ожидаем отправки чека...", reply_markup=cancel_keyboard())

    if user_id in payment_timers:
        try: payment_timers[user_id].cancel()
        except Exception: pass
    timer = threading.Timer(300, cancel_payment, args=[user_id])
    payment_timers[user_id] = timer
    timer.start()
    bot.register_next_step_handler(msg, get_check_photo)

def get_check_photo(msg):
    user_id = msg.chat.id
    if msg.text and (msg.text.startswith('/start') or "Отмена" in msg.text):
        if user_id in payment_timers:
            try: payment_timers[user_id].cancel()
            except Exception: pass
            payment_timers.pop(user_id, None)
        start(msg); return

    photo_id = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else None)
    if not photo_id:
        send_msg(user_id, "❌ Пожалуйста, отправьте фото или файл чека об оплате:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_check_photo); return

    if user_id in payment_timers:
        try: payment_timers[user_id].cancel()
        except Exception: pass
        payment_timers.pop(user_id, None)

    account_id = temp_data.get(user_id, {}).get("account_id")
    amount = temp_data.get(user_id, {}).get("amount")
    pay_method = temp_data.get(user_id, {}).get("pay_method", "QR-код")
    
    if not account_id or not amount:
        send_msg(user_id, "❌ Сессия истекла. Начните заново.")
        start(msg); return

    touch_user(msg)
    dep_id = add_deposit(user_id, amount, account_id, photo_id, pay_method=pay_method)
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(f"✅ Одобрить", callback_data=f"approve_{dep_id}"),
        types.InlineKeyboardButton(f"❌ Отклонить", callback_data=f"reject_{dep_id}")
    )
    caption = (
        f"📥 <b>ЗАЯВКА НА ПОПОЛНЕНИЕ 1XBET #{dep_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🏦 <b>Метод:</b> {pay_method}\n"
        f"👤 <b>Игрок:</b> {user_label(user_id)}\n"
        f"💰 <b>Сумма:</b> <code>{amount:.2f}</code> KGS\n"
        f"🆔 <b>Аккаунт:</b> <code>{safe_html(account_id)}</code>"
    )
    for admin in get_admins():
        send_media_bulletproof(admin, photo_id, caption=caption, reply_markup=markup)

    send_msg(
        user_id,
        f"⏳ <b>Заявка #{dep_id} принята и находится на проверке!</b>\n\n"
        f"Чек отправлен оператору. Если авто-зачисление не поступит в течение <b>20 минут</b>, "
        f"администратор проверит платеж и одобрит заявку вручную.",
        reply_markup=main_keyboard()
    )
    temp_data.pop(user_id, None)

# --- АВТО-ВЫВОД СРЕДСТВ ---

@bot.message_handler(func=lambda m: "Вывести" in (m.text or ""))
def withdraw_start(msg):
    if is_user_banned(msg.chat.id): return
    if not is_withdrawals_enabled() and msg.from_user.id not in get_admins() and msg.from_user.id != MAIN_ADMIN:
        send_msg(msg.chat.id, "⚠️ <b>Вывод средств временно отключен администратором.</b> Попробуйте позже.")
        return
    if not is_bot_active() and msg.from_user.id not in get_admins() and msg.from_user.id != MAIN_ADMIN:
        send_msg(msg.chat.id, "🔴 Бот на техническом обслуживании.")
        return
    temp_data[msg.chat.id] = {"platform": "1XBET"}
    send_msg(msg.chat.id, f"📸 <b>Отправьте QR-код вашего кошелька или реквизиты (MBank, O!Bank, Bakai) для получения средств:</b>", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, withdraw_get_elqr)

def withdraw_get_elqr(msg):
    if msg.text and (msg.text.startswith('/start') or "Отмена" in msg.text):
        start(msg); return
    elqr = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else None)
    if not elqr:
        send_msg(msg.chat.id, "❌ Пожалуйста, отправьте изображение QR-кода или реквизитов:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, withdraw_get_elqr); return
    temp_data.setdefault(msg.chat.id, {})["elqr"] = elqr
    send_msg(msg.chat.id, f"🆔 <b>Введите ваш ID 1XBET:</b>", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, withdraw_get_id)

def withdraw_get_id(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    acc_number = msg.text.strip()
    if not acc_number.isdigit():
        send_msg(msg.chat.id, "❌ ID должен состоять только из цифр. Введите корректный ID 1XBET:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, withdraw_get_id)
        return
    temp_data.setdefault(msg.chat.id, {})["id_photo"] = f"1XBET | {acc_number}"
    
    img = get_withdraw_image()
    if img: send_media_bulletproof(msg.chat.id, img)
    
    send_msg(msg.chat.id, f"🔑 <b>Введите код подтверждения вывода из 1XBET:</b>", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, withdraw_get_code)

def withdraw_get_code(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    user_id = msg.chat.id
    elqr = temp_data.get(user_id, {}).get("elqr")
    id_photo = temp_data.get(user_id, {}).get("id_photo")
    code = msg.text.strip()
    
    if not elqr or not id_photo:
        send_msg(user_id, "❌ Ошибка данных. Начните сначала.", reply_markup=main_keyboard())
        start(msg); return
        
    touch_user(msg)
    raw_acc_id = int(id_photo.replace("1XBET | ", "").strip())

    sent_msg = send_msg(user_id, "🔍 <i>Проверяем код и выполняем авто-вывод через кассу BigPay...</i>")

    try:
        api_resp = api.payout(raw_acc_id, code)
        logger.info(f"[API PAYOUT RESPONSE] {api_resp}")
        
        is_success = api_resp.get("success") or api_resp.get("Success")
        
        if sent_msg:
            try: bot.delete_message(user_id, sent_msg.message_id)
            except Exception: pass

        if not is_success:
            err_text = api_resp.get('Message') or api_resp.get('message') or 'Неверный код или ошибка кассы'
            send_msg(user_id, f"❌ <b>Ошибка выплаты:</b> {safe_html(err_text)}", reply_markup=main_keyboard())
            temp_data.pop(user_id, None)
            return

        payout_amount = api_resp.get("summa") or api_resp.get("Summa") or api_resp.get("amount") or 0.0

        w_id = add_withdrawal(user_id, elqr, id_photo, code)
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(f"✅ Выплата выполнена (#{w_id})", callback_data="none"))

        caption = (
            f"📤 <b>АВТО-ВЫВОД BigPay ЗАВЕРШЕН #{w_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Игрок:</b> {user_label(user_id)}\n"
            f"🆔 <b>Аккаунт:</b> <code>{safe_html(id_photo)}</code>\n"
            f"💰 <b>Сумма вывода:</b> <code>{payout_amount}</code> KGS\n"
            f"🔑 <b>Код:</b> <code>{safe_html(code)}</code>"
        )
        
        for admin in get_admins():
            send_media_bulletproof(admin, elqr, caption=caption, reply_markup=markup)

        send_msg(
            user_id, 
            f"✅ <b>Вывод успешно выполнен через кассу!</b>\n\n"
            f"💰 Сумма: <b>{payout_amount} KGS</b>\n"
            f"⏳ Средства отправлены на ваши реквизиты.", 
            reply_markup=main_keyboard()
        )

    except Exception as e:
        if sent_msg:
            try: bot.delete_message(user_id, sent_msg.message_id)
            except Exception: pass
        logger.error(f"Withdraw error: {e}")
        send_msg(user_id, f"❌ <b>Ошибка связи с API кассы:</b> {safe_html(str(e)[:150])}", reply_markup=main_keyboard())
        
    temp_data.pop(user_id, None)

# --- АДМИН-ПАНЕЛЬ ---

@bot.message_handler(commands=["admin"])
@bot.message_handler(func=lambda m: m.text in ["⚙️ Admin", "Admin"] and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def admin_panel(msg):
    uid = msg.from_user.id
    if uid not in get_admins() and uid != MAIN_ADMIN:
        send_msg(msg.chat.id, "❌ У вас нет прав доступа.")
        return
    send_msg(msg.chat.id, f"⚙️ <b>ПАНЕЛЬ АДМИНИСТРАТОРА (BigPay)</b>\n\nВаш ID: <code>{uid}</code>\nКасса ID: <code>{CASHDESK_ID}</code>", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "💳 Реквизиты" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def edit_reqs(msg):
    curr = get_requisites()
    send_msg(msg.chat.id, f"💳 <b>ТЕКУЩИЕ ДАННЫЕ / РЕКВИЗИТЫ:</b>\n<code>{safe_html(curr)}</code>\n\nОтправьте новые данные сообщением:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, save_reqs_proc)

def save_reqs_proc(msg):
    if not msg.text or "Отмена" in msg.text:
        admin_panel(msg); return
    save_requisites(msg.text.strip())
    send_msg(msg.chat.id, f"✅ Данные успешно обновлены!", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "Авто-API" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def toggle_auto_deposit(msg):
    current = is_auto_deposit_active()
    new_status = not current
    set_auto_deposit_active(new_status)
    status_text = "🟢 ВКЛЮЧЕНО" if new_status else "🔴 ВЫКЛЮЧЕНО"
    send_msg(msg.chat.id, f"⚙️ Авто-зачисление через API кассы при одобрении: <b>{status_text}</b>", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "Пополнение:" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def toggle_deposits_handler(msg):
    current = is_deposits_enabled()
    new_status = not current
    set_deposits_enabled(new_status)
    status_text = "🟢 ВКЛЮЧЕНО" if new_status else "🔴 ВЫКЛЮЧЕНО"
    send_msg(msg.chat.id, f"💳 Прием пополнений от пользователей: <b>{status_text}</b>", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "Вывод:" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def toggle_withdrawals_handler(msg):
    current = is_withdrawals_enabled()
    new_status = not current
    set_withdrawals_enabled(new_status)
    status_text = "🟢 ВКЛЮЧЕНО" if new_status else "🔴 ВЫКЛЮЧЕНО"
    send_msg(msg.chat.id, f"📤 Обработка выводов средств: <b>{status_text}</b>", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "🚫 Забанить" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def ban_user_start(msg):
    send_msg(msg.chat.id, "Введите Telegram ID пользователя для блокировки:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, ban_user_proc)

def ban_user_proc(msg):
    if not msg.text or "Отмена" in msg.text:
        admin_panel(msg); return
    try:
        uid = int(msg.text.strip())
        set_ban_status(uid, True)
        send_msg(msg.chat.id, f"❌ Пользователь <code>{uid}</code> заблокирован!", reply_markup=admin_keyboard())
    except Exception:
        send_msg(msg.chat.id, "❌ Некорректный ID!", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "✅ Разбанить" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def unban_user_start(msg):
    send_msg(msg.chat.id, "Введите Telegram ID пользователя для разблокировки:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, unban_user_proc)

def unban_user_proc(msg):
    if not msg.text or "Отмена" in msg.text:
        admin_panel(msg); return
    try:
        uid = int(msg.text.strip())
        set_ban_status(uid, False)
        send_msg(msg.chat.id, f"✅ Пользователь <code>{uid}</code> разблокирован!", reply_markup=admin_keyboard())
    except Exception:
        send_msg(msg.chat.id, "❌ Некорректный ID!", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: m.text in ["➕ Админ", "Админ"] and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def add_admin_btn(msg):
    send_msg(msg.chat.id, "Введите ID нового администратора:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, process_add_admin)

def process_add_admin(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        admin_panel(msg); return
    try:
        add_admin(int(msg.text.strip()))
        send_msg(msg.chat.id, f"✅ Новый администратор успешно добавлен!", reply_markup=admin_keyboard())
    except Exception:
        send_msg(msg.chat.id, "❌ Ошибка! Некорректный ID.", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: m.text in ["➖ Удалить админа", "Удалить админа"] and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def remove_admin_btn(msg):
    if msg.from_user.id != MAIN_ADMIN:
        send_msg(msg.chat.id, "❌ Данное действие доступно только главному администратору.")
        return
    send_msg(msg.chat.id, "Введите ID администратора для удаления:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, process_remove_admin)

def process_remove_admin(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        admin_panel(msg); return
    try:
        admin_id = int(msg.text.strip())
        if admin_id == MAIN_ADMIN:
            send_msg(msg.chat.id, "❌ Нельзя удалить главного администратора!", reply_markup=admin_keyboard())
            return
        remove_admin(admin_id)
        send_msg(msg.chat.id, f"✅ Администратор <code>{admin_id}</code> удален!", reply_markup=admin_keyboard())
    except Exception:
        send_msg(msg.chat.id, "❌ Некорректный ID!", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: ("ВЫКЛ" in (m.text or "") or "ВКЛ" in (m.text or "")) and not "Авто-API" in (m.text or "") and not "Пополнение:" in (m.text or "") and not "Вывод:" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def toggle_bot(msg):
    active = "ВКЛ" in msg.text
    set_bot_active(active)
    send_msg(msg.chat.id, f"🟢 Бот ВКЛЮЧЕН" if active else f"🔴 Бот ВЫКЛЮЧЕН", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "Изменить QR" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def change_qr(msg):
    send_msg(msg.chat.id, "📸 Отправьте изображение нового QR-кода:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, save_new_qr)

def save_new_qr(msg):
    if msg.text and (msg.text.startswith('/start') or "Отмена" in msg.text):
        admin_panel(msg); return
    file_id = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else None)
    if file_id:
        save_qr(file_id)
        send_msg(msg.chat.id, f"✅ Новый QR-код успешно сохранен!", reply_markup=admin_keyboard())
    else:
        send_msg(msg.chat.id, "❌ Пожалуйста, отправьте изображение QR-кода:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, save_new_qr)

@bot.message_handler(func=lambda m: "Инструкция вывода" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def change_withdraw_image(msg):
    send_msg(msg.chat.id, "📸 Отправьте новое изображение инструкции вывода:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, save_new_withdraw_image)

def save_new_withdraw_image(msg):
    if msg.text and (msg.text.startswith('/start') or "Отмена" in msg.text):
        admin_panel(msg); return
    file_id = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else None)
    if file_id:
        save_withdraw_image(file_id)
        send_msg(msg.chat.id, f"✅ Изображение инструкции вывода сохранено!", reply_markup=admin_keyboard())
    else:
        send_msg(msg.chat.id, "❌ Пожалуйста, отправьте изображение!", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, save_new_withdraw_image)

@bot.message_handler(func=lambda m: m.text in ["📋 Заявки", "Заявки"] and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def view_requests(msg):
    deposits = get_pending_deposits()
    if not deposits:
        send_msg(msg.chat.id, f"✅ Нет активных заявок на обработку.")
        return
    for dep in deposits:
        dep_id, user_id, amount, account_id, photo_id, date, ts, pay_method = dep
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton(f"✅ Одобрить", callback_data=f"approve_{dep_id}"),
            types.InlineKeyboardButton(f"❌ Отклонить", callback_data=f"reject_{dep_id}")
        )
        caption = (
            f"📥 <b>ЗАЯВКА #{dep_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🏦 <b>Метод:</b> {pay_method or 'QR-код'}\n"
            f"👤 Игрок: {user_label(user_id)}\n"
            f"💰 Сумма: <code>{amount:.2f}</code> KGS\n"
            f"🆔 Счет: <code>{safe_html(account_id)}</code>"
        )
        send_media_bulletproof(msg.chat.id, photo_id, caption=caption, reply_markup=markup)

@bot.message_handler(func=lambda m: "📊 Статистика" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def stats(msg):
    s = get_stats()
    auto_status = "ВКЛЮЧЕНО ⚡️" if is_auto_deposit_active() else "ВЫКЛЮЧЕНО 🔴"
    text = (
        f"📊 <b>СТАТИСТИКА БОТА (BigPay)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Всего пользователей: <b>{s['users']}</b>\n"
        f"⏳ Заявок в очереди: <b>{s['pending']}</b>\n"
        f"💰 Успешных пополнений: <b>{s['total']:.2f} сом</b>\n"
        f"⚡️ Авто-API при одобрении: <b>{auto_status}</b>"
    )
    send_msg(msg.chat.id, text)

@bot.message_handler(func=lambda m: "Рассылка" in (m.text or "") and (m.from_user.id in get_admins() or m.from_user.id == MAIN_ADMIN))
def broadcast_start(msg):
    send_msg(msg.chat.id, "📢 Отправьте сообщение (текст или фото с описанием) для рассылки всем пользователям:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, broadcast_send)

def broadcast_send(msg):
    if msg.text and (msg.text.startswith('/start') or "Отмена" in msg.text):
        admin_panel(msg); return
    users = get_all_users()
    success = 0
    photo_id = msg.photo[-1].file_id if msg.photo else None
    text = msg.caption or msg.text or ""
    for uid in users:
        try:
            if photo_id: bot.send_photo(uid, photo_id, caption=text)
            elif text: bot.send_message(uid, text)
            success += 1
        except Exception: pass
        time.sleep(0.05)
    send_msg(msg.chat.id, f"✅ Рассылка завершена! Успешно отправлено: {success}/{len(users)}", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: m.text in ["📩 Обращения", "Обращения"] and (m.from_user.id in get_admins() or m.from_user.id in (MAIN_ADMIN, OPERATOR_ID)))
def operator_tickets(msg):
    rows = list_tickets(limit=15)
    if not rows:
        send_msg(msg.chat.id, f"✅ Активных обращений нет.")
        return
    status_map = {"new": "🆕 Новое", "work": "🔄 В работе", "closed": "✅ Закрыто"}
    for tid, user_id, username, message, status, created in rows:
        st = status_map.get(status, status)
        uname = f"@{username}" if username else str(user_id)
        kb = types.InlineKeyboardMarkup(row_width=2)
        if status != "closed":
            kb.add(
                types.InlineKeyboardButton("🔄 В работу", callback_data=f"tkt_work_{tid}"),
                types.InlineKeyboardButton("✍️ Ответить", callback_data=f"tkt_reply_{tid}"),
                types.InlineKeyboardButton("❌ Закрыть", callback_data=f"tkt_close_{tid}")
            )
        send_msg(msg.chat.id, f"🆘 <b>ОБРАЩЕНИЕ #{tid}</b> · {st}\n👤 {uname} (<code>{user_id}</code>)\n📅 {created}\n\n💬 {safe_html(message[:500])}", reply_markup=kb if status != "closed" else None)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("tkt_"))
def ticket_callbacks(call):
    if call.from_user.id not in get_admins() and call.from_user.id not in (MAIN_ADMIN, OPERATOR_ID):
        bot.answer_callback_query(call.id, "У вас нет прав"); return
    parts = call.data.split("_")
    action, tid = parts[1], int(parts[2])
    row = get_ticket(tid)
    if not row:
        bot.answer_callback_query(call.id, "Обращение не найдено"); return
    user_id = row[1]

    if action == "work":
        set_ticket_status(tid, "work")
        bot.answer_callback_query(call.id, "Взято в работу")
        try: bot.send_message(user_id, f"🔄 Ваш запрос #{tid} взят в работу оператором.")
        except Exception: pass
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception: pass
        return

    if action == "close":
        set_ticket_status(tid, "closed")
        bot.answer_callback_query(call.id, "Закрыто")
        try: bot.send_message(user_id, f"✅ Ваш запрос #{tid} решен и закрыт.")
        except Exception: pass
        try: bot.edit_message_text((call.message.text or "") + f"\n\n✅ <b>Закрыто</b>", call.message.chat.id, call.message.message_id)
        except Exception: pass
        return

    if action == "reply":
        bot.answer_callback_query(call.id)
        send_msg(call.from_user.id, f"✍️ <b>Ответ на обращение #{tid}:</b>\nНапишите ваш ответ пользователю.")
        temp_data[call.from_user.id] = {"reply_ticket": tid}
        bot.register_next_step_handler_by_chat_id(call.from_user.id, operator_send_reply)
        return

def operator_send_reply(msg):
    if not msg.text or msg.text.startswith("/start"):
        start(msg); return
    tid = temp_data.get(msg.chat.id, {}).get("reply_ticket")
    row = get_ticket(tid) if tid else None
    if not row:
        send_msg(msg.chat.id, "❌ Обращение не найдено."); return
    user_id = row[1]
    answer = msg.text.strip()
    add_ticket_reply(tid, True, answer)
    set_ticket_status(tid, "work")
    try:
        bot.send_message(user_id, f"💬 <b>Ответ оператора по запросу #{tid}:</b>\n\n{safe_html(answer)}")
    except Exception as e:
        send_msg(msg.chat.id, f"❌ Ошибка отправки: {e}"); return
    send_msg(msg.chat.id, f"✅ Ответ успешно доставлен пользователю (#{tid}).", reply_markup=admin_keyboard())
    temp_data.pop(msg.chat.id, None)

# --- ОБРАБОТКА КНОПОК АДМИНА (ДЕПОЗИТЫ) ---

@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_', 'w_done_', 'w_cancel_')))
def handle_admin_callbacks(call):
    if call.from_user.id not in get_admins() and call.from_user.id != MAIN_ADMIN:
        bot.answer_callback_query(call.id, "❌ Нет прав доступа!"); return
    data = call.data

    if data.startswith('approve_'):
        dep_id = int(data.split('_')[1])
        status = get_deposit_status(dep_id)
        if status != 'pending':
            bot.answer_callback_query(call.id, f"⚠️ Эта заявка уже обработана! (Статус: {status})", show_alert=True)
            return

        with get_db() as conn:
            row = conn.execute('SELECT user_id, amount, account_id, timestamp FROM deposits WHERE id = ?', (dep_id,)).fetchone()
        
        if row:
            user_id, amount, account_id, ts = row
            raw_acc_id = int(account_id.replace("1XBET | ", "").strip())
            
            if is_auto_deposit_active():
                try:
                    api_resp = api.deposit(raw_acc_id, amount)
                    logger.info(f"[API DEPOSIT RESPONSE] {api_resp}")
                    
                    is_success = api_resp.get("success") or api_resp.get("Success")
                    if not is_success:
                        err_text = api_resp.get('Message') or api_resp.get('message') or api_resp.get('error') or str(api_resp)
                        bot.answer_callback_query(call.id, f"❌ Ошибка кассы: {err_text}", show_alert=True)
                        return
                except requests.exceptions.HTTPError as http_err:
                    err_msg = str(http_err)
                    try:
                        err_json = http_err.response.json()
                        err_msg = err_json.get("Message") or err_json.get("message") or err_json.get("error") or str(err_json)
                    except Exception:
                        err_msg = http_err.response.text or err_msg
                    bot.answer_callback_query(call.id, f"❌ Ошибка API: {err_msg[:150]}", show_alert=True)
                    return
                except Exception as e:
                    bot.answer_callback_query(call.id, f"❌ Ошибка связи с API: {str(e)[:150]}", show_alert=True)
                    return

            update_deposit_status(dep_id, "approved")
            bot.answer_callback_query(call.id, "✅ Заявка одобрена и успешно зачислена!")
            elapsed = int(time.time()) - ts
            
            try:
                bot.send_message(
                    user_id,
                    f"🎉 <b>БАЛАНС 1XBET УСПЕШНО ПОПОЛНЕН!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"{E_MONEY} <b>Зачислено:</b> <code>{amount:.2f}</code> KGS\n"
                    f"🆔 <b>Счет игрока:</b> <code>{safe_html(account_id)}</code>\n"
                    f"⏱ <b>Время обработки:</b> {elapsed} сек.\n\n"
                    f"Спасибо за выбор BigPay! 🚀"
                )
            except Exception: 
                pass
                
            try: 
                bot.edit_message_caption(
                    f"✅ <b>Заявка #{dep_id} ОДОБРЕНА И ЗАСЧИТАНА (Админ: {call.from_user.first_name})</b>", 
                    call.message.chat.id, 
                    call.message.message_id, 
                    reply_markup=None
                )
            except Exception: 
                pass

    elif data.startswith('reject_'):
        dep_id = int(data.split('_')[1])
        status = get_deposit_status(dep_id)
        if status != 'pending':
            bot.answer_callback_query(call.id, f"⚠️ Эта заявка уже обработана! (Статус: {status})", show_alert=True)
            return

        with get_db() as conn:
            row = conn.execute('SELECT user_id, amount FROM deposits WHERE id = ?', (dep_id,)).fetchone()
        if row:
            user_id, amount = row
            update_deposit_status(dep_id, "rejected")
            bot.answer_callback_query(call.id, "❌ Заявка отклонена!")
            try:
                bot.send_message(user_id, f"❌ <b>Заявка на {amount:.2f} сом была отклонена (неверный чек/оплата не поступила).</b>\nЕсли возникли вопросы, обратитесь в поддержку: <a href=\"{support_url()}\">@{SUPPORT.lstrip('@')}</a>")
            except Exception: pass
            try: bot.edit_message_caption(f"❌ <b>Заявка #{dep_id} ОТКЛОНЕНА (Админ: {call.from_user.first_name})</b>", call.message.chat.id, call.message.message_id)
            except Exception: pass

    elif data.startswith('w_cancel_'):
        w_id = int(data.split('_')[2])
        status = get_withdrawal_status(w_id)
        if status != 'pending':
            bot.answer_callback_query(call.id, f"⚠️ Эта заявка уже обработана! (Статус: {status})", show_alert=True)
            return

        with get_db() as conn:
            conn.execute('UPDATE withdrawals SET status = "rejected" WHERE id = ?', (w_id,))
            row = conn.execute('SELECT user_id FROM withdrawals WHERE id = ?', (w_id,)).fetchone()
            conn.commit()
        if row:
            bot.answer_callback_query(call.id, "Заявка отклонена")
            try: bot.send_message(row[0], f"❌ <b>Вывод #{w_id} был отклонен.</b>\nОбратитесь в поддержку: <a href=\"{support_url()}\">@{SUPPORT.lstrip('@')}</a>")
            except Exception: pass
        try: bot.edit_message_caption(f"❌ <b>ВЫВОД #{w_id} ОТКЛОНЕН (Админ: {call.from_user.first_name})</b>", call.message.chat.id, call.message.message_id)
        except Exception: pass

@app.route('/')
def home():
    return {"status": "ok", "bot": BOT_NAME, "webapp": bool(WEBAPP_URL)}, 200

def _find_app_html():
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "app.html"),
        os.path.join(os.getcwd(), "static", "app.html"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.html"),
        os.path.join(os.getcwd(), "app.html"),
    ]
    for path in candidates:
        if os.path.isfile(path): return path
    return None

@app.route('/app')
@app.route('/static/app.html')
def mini_app():
    path = _find_app_html()
    if not path:
        return "<h3>app.html не найден</h3><p>Загрузите файл в папку <b>static/app.html</b> рядом с bot.py</p>", 404
    return send_from_directory(os.path.dirname(path), os.path.basename(path))

@app.route('/api/config')
def api_config():
    return jsonify({"pay_link": PAY_LINK, "min_deposit": MIN_DEPOSIT, "max_deposit": MAX_DEPOSIT, "bot": BOT_NAME})

@app.route('/api/admin', methods=['POST'])
def api_admin():
    data = request.get_json(silent=True) or {}
    uid = int(data.get("id") or 0)
    pin = str(data.get("pin") or "")
    if uid == MAIN_ADMIN or uid in get_admins():
        if pin == str(ADMIN_PIN):
            s = get_stats()
            return jsonify({"ok": True, "stats": s, "admin": True})
    return jsonify({"ok": False, "error": "Нет доступа"}), 403

def setup_menu_button():
    if not WEBAPP_URL:
        logger.warning("WEBAPP_URL не задан")
        return
    try:
        bot.set_chat_menu_button(menu_button=types.MenuButtonWebApp(text="Меню", web_app=types.WebAppInfo(url=f"{WEBAPP_URL}/app")))
        logger.info("MenuButtonWebApp установлен")
    except Exception as e:
        logger.warning(f"set_chat_menu_button: {e}")

if __name__ == "__main__":
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception as e:
        logger.warning(f"remove_webhook: {e}")

    setup_menu_button()

    def run_flask():
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)

    threading.Thread(target=run_flask, daemon=True).start()

    logger.info(f"{BOT_NAME} запущен | admin={MAIN_ADMIN} | cashdesk={CASHDESK_ID} (BigPay)")
    while True:
        try:
            bot.polling(none_stop=True, interval=1, timeout=40)
        except Exception as e:
            logger.error(f"Ошибка polling: {e}")
            time.sleep(5)
