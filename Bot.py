import os
import re
import json
import sqlite3
import time
import threading
import html
import random
import logging
import requests
from datetime import datetime
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

# ======================= НАСТРОЙКИ =======================
TOKEN = os.environ.get("TOKEN_REF") or os.environ.get("TOKEN")
if not TOKEN or len(TOKEN) < 40:
    logger.error("ТОКЕН НЕ НАЙДЕН!")
    raise SystemExit("No token")

MAIN_ADMIN = int(os.environ.get("MAIN_ADMIN", "8957913298"))
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1905")
OPERATOR_ID = int(os.environ.get("OPERATOR_ID", str(MAIN_ADMIN)))
SUPPORT = os.environ.get("SUPPORT", "BigPay_ADMIN")

CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "bigpay_news")
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@bigpay_news")

BOT_NAME = "BigPay"
BOT_USERNAME = os.environ.get("BOT_USERNAME", "BigPayKG_app")
MIN_DEPOSIT = 35
MAX_DEPOSIT = 50000

# Реферальная система
REF_PERCENT = 3          # % с каждого принятого пополнения друга
REF_MIN = 200            # мин. сумма пополнения / вывода с реф. баланса (KGS)
REF_PAY_METHOD = "Реф. баланс"

WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
PAY_LINK = os.environ.get("PAY_LINK", "")

# --- ДАННЫЕ КАССЫ MOB-CASH (1xBet) ---
CASHBOX_CODE = int(os.environ.get("CASHBOX_CODE", "1008610"))
LOGIN = os.environ.get("LOGIN", "tashtemirovt")
PASSWORD = os.environ.get("PASSWORD", "JetNurs009_")


# ======================= MOB-CASH API =======================
class MobCashAPI:
    BASE_AUTH = "https://admin.mob-cash.com/api/v2/cashbox"
    BASE_MOBILE = "https://admin.mob-cash.com/api/v1/mobile"

    def __init__(self, cashbox_code: int, login: str, password: str):
        self.cashbox_code = cashbox_code
        self.login = login
        self.password = password
        self.session = requests.Session()
        self.access_token = None
        self.session_id = None
        self.user_id = None
        self.token_expires_at = None
        self._lock = threading.Lock()

    def login_cashbox(self) -> bool:
        url = f"{self.BASE_AUTH}/login"
        payload = {"cashboxCode": self.cashbox_code, "login": self.login, "password": self.password}
        headers = {"Accept": "application/json, text/plain, */*", "Content-Type": "application/json"}
        try:
            response = self.session.post(url, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            self.access_token = data.get("accessToken")
            self.session_id = data.get("sessionID")
            self.user_id = data.get("userID")
            self.token_expires_at = data.get("expiresAt")
            logger.info(f"Успешная авторизация в MobCash. UserID: {self.user_id}")
            return True
        except Exception as e:
            logger.error(f"Ошибка авторизации MobCash: {e}")
            return False

    def _ensure_auth(self):
        if not self.access_token or not self.session_id or not self.user_id:
            return self.login_cashbox()
        return True

    @staticmethod
    def _parse(res_data, default_err):
        if isinstance(res_data, list) and len(res_data) > 0:
            item = res_data[0]
            if "error" in item:
                raise Exception(item["error"].get("message", default_err))
            return item.get("result", {})
        if isinstance(res_data, dict):
            if "error" in res_data:
                raise Exception(res_data["error"].get("message", default_err))
            return res_data.get("result", res_data)
        return res_data

    def _post(self, path, build_payload, default_err):
        """POST с авто-перелогином при 401 (payload строится заново с новой сессией)."""
        with self._lock:
            if not self._ensure_auth():
                raise Exception("Не удалось авторизоваться в платежной системе")
            url = f"{self.BASE_MOBILE}/{path}"
            for attempt in (1, 2):
                headers = {
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.access_token}",
                }
                response = self.session.post(url, json=build_payload(), headers=headers, timeout=15)
                if response.status_code == 401 and attempt == 1:
                    self.access_token = None
                    if not self.login_cashbox():
                        raise Exception("Не удалось авторизоваться в платежной системе")
                    continue
                response.raise_for_status()
                return self._parse(response.json(), default_err)

    def find_user(self, payer_id: str) -> dict:
        return self._post("payerNickname", lambda: {
            "id": 1, "jsonrpc": "2.0",
            "params": {"payerID": str(payer_id), "sessionID": str(self.session_id)},
            "userID": str(self.user_id),
        }, "Ошибка проверки аккаунта")

    def deposit(self, payer_id: str, amount: float) -> dict:
        return self._post("deposit", lambda: {
            "id": 1, "jsonrpc": "2.0",
            "params": {
                "deposit": {"amount": str(amount), "payerID": str(payer_id)},
                "sessionID": str(self.session_id), "userID": str(self.user_id),
            },
        }, "Ошибка пополнения")

    def get_withdrawal_amount(self, payer_id: str, withdrawal_code: str) -> dict:
        return self._post("getWithdrawalAmount", lambda: {
            "id": 1, "jsonrpc": "2.0",
            "params": {
                "sessionID": str(self.session_id), "userID": str(self.user_id),
                "withdraw": {"payerID": str(payer_id), "withdrawalCode": str(withdrawal_code)},
            },
        }, "Ошибка запроса ордера")

    def payout(self, payer_id: str, amount: float, withdrawal_code: str) -> dict:
        return self._post("withdrawal", lambda: {
            "id": 1, "jsonrpc": "2.0",
            "params": {
                "sessionID": str(self.session_id), "userID": str(self.user_id),
                "withdraw": {"amount": amount, "payerID": str(payer_id), "withdrawalCode": str(withdrawal_code)},
            },
        }, "Ошибка выплаты")


api = MobCashAPI(CASHBOX_CODE, LOGIN, PASSWORD)

# Платформы: ключ хранится в БД перед ID ("1XBET | 12345")
PLATFORMS = {"1XBET": {"title": "1xBet", "api": api}}
if os.environ.get("MELBET_CASHBOX_CODE"):
    PLATFORMS["MELBET"] = {
        "title": "Melbet",
        "api": MobCashAPI(
            int(os.environ["MELBET_CASHBOX_CODE"]),
            os.environ.get("MELBET_LOGIN", LOGIN),
            os.environ.get("MELBET_PASSWORD", PASSWORD),
        ),
    }

def platforms_label():
    return " и ".join(p["title"] for p in PLATFORMS.values())

def split_account(account_id):
    """'1XBET | 123' -> ('1XBET', '123')"""
    s = str(account_id or "")
    if "|" in s:
        plat, acc = s.split("|", 1)
        plat, acc = plat.strip().upper(), acc.strip()
        return (plat if plat in PLATFORMS else "1XBET"), acc
    return "1XBET", s.strip()

def support_url():
    if SUPPORT and str(SUPPORT).strip():
        return f"https://t.me/{str(SUPPORT).lstrip('@').strip()}"
    return f"tg://user?id={OPERATOR_ID}"


# ======================= ИНИЦИАЛИЗАЦИЯ =======================
bot = telebot.TeleBot(TOKEN, parse_mode='HTML')
app = Flask(__name__)

temp_data = {}
payment_timers = {}
pending_refs = {}
DB_NAME = 'bot.db'

E_ROCKET = '🚀'
E_LIGHTNING = '⚡️'
E_SUPPORT = '🎧'
E_STORE = '🏪'
E_CARD = '💳'
E_MONEY = '💰'
E_CASH = '💵'
E_GIFT = '🎁'

def safe_html(text):
    return html.escape(str(text)) if text else ""

def norm(text):
    """Убирает ведущие эмодзи/символы: '✕ Отмена' -> 'Отмена'."""
    return re.sub(r'^[^\w]+', '', (text or '').strip())

_bot_username = None
def get_bot_username():
    global _bot_username
    if not _bot_username:
        try:
            _bot_username = bot.get_me().username
        except Exception:
            _bot_username = BOT_USERNAME
    return _bot_username


# ======================= ЦВЕТНЫЕ КНОПКИ (Bot API 9.4: style) =======================
class RawMarkup(types.JsonSerializable):
    """Клавиатура «как есть» — позволяет использовать поле style (primary / success / danger)
    независимо от версии pyTelegramBotAPI."""
    def __init__(self, data):
        self.data = data

    def to_dict(self):
        return self.data

    def to_json(self):
        return json.dumps(self.data, ensure_ascii=False)

def kb(rows, **extra):
    """Reply-клавиатура. Элемент: 'Текст' или ('Текст', 'primary'|'success'|'danger')."""
    keyboard = []
    for row in rows:
        r = []
        for item in row:
            if isinstance(item, str):
                r.append({"text": item})
            else:
                text, style = item
                b = {"text": text}
                if style:
                    b["style"] = style
                r.append(b)
        keyboard.append(r)
    data = {"keyboard": keyboard, "resize_keyboard": True}
    data.update(extra)
    return RawMarkup(data)

def ikb(rows):
    """Inline-клавиатура. Элемент: (текст, стиль, 'cb'|'url', значение)."""
    out = []
    for row in rows:
        r = []
        for text, style, kind, val in row:
            b = {"text": text}
            if style:
                b["style"] = style
            b["url" if kind == "url" else "callback_data"] = val
            r.append(b)
        out.append(r)
    return RawMarkup({"inline_keyboard": out})


# ======================= БАЗА ДАННЫХ =======================
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
        c.execute('''CREATE TABLE IF NOT EXISTS ref_withdrawals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
                        qr_photo TEXT, status TEXT, date TEXT)''')

        for sql in (
            "ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0",
            "ALTER TABLE deposits ADD COLUMN pay_method TEXT",
            "ALTER TABLE users ADD COLUMN referrer_id INTEGER",
            "ALTER TABLE users ADD COLUMN ref_balance REAL DEFAULT 0",
            "ALTER TABLE withdrawals ADD COLUMN amount REAL",
        ):
            try:
                c.execute(sql)
            except Exception:
                pass

        if MAIN_ADMIN > 0:
            c.execute('INSERT OR IGNORE INTO admins (chat_id) VALUES (?)', (MAIN_ADMIN,))
        c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES ("bot_active", "True")')
        c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES ("auto_deposit", "True")')
        c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES ("deposits_enabled", "True")')
        c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES ("withdrawals_enabled", "True")')
        conn.commit()
    logger.info("База данных готова")

def is_user_banned(user_id):
    with get_db() as conn:
        row = conn.execute('SELECT is_banned FROM users WHERE chat_id = ?', (user_id,)).fetchone()
        return bool(row and row[0] == 1)

def set_ban_status(user_id, banned: bool):
    with get_db() as conn:
        conn.execute('UPDATE users SET is_banned = ? WHERE chat_id = ?', (1 if banned else 0, user_id))
        conn.commit()

def _get_flag(key, default_true=True):
    with get_db() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    if row is None:
        return default_true
    return row[0] == 'True'

def _set_flag(key, status: bool):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(status)))
        conn.commit()

def is_auto_deposit_active(): return _get_flag("auto_deposit")
def set_auto_deposit_active(s): _set_flag("auto_deposit", s)
def is_deposits_enabled(): return _get_flag("deposits_enabled")
def set_deposits_enabled(s): _set_flag("deposits_enabled", s)
def is_withdrawals_enabled(): return _get_flag("withdrawals_enabled")
def set_withdrawals_enabled(s): _set_flag("withdrawals_enabled", s)
def is_bot_active(): return _get_flag("bot_active")
def set_bot_active(s): _set_flag("bot_active", s)

def save_requisites(text):
    with get_db() as conn:
        conn.execute('DELETE FROM requisites')
        conn.execute('INSERT INTO requisites (req_text) VALUES (?)', (text,))
        conn.commit()

def get_requisites():
    with get_db() as conn:
        row = conn.execute('SELECT req_text FROM requisites ORDER BY id DESC LIMIT 1').fetchone()
        return row[0] if row else "Отсканируйте QR-код для оплаты:"

def get_admins():
    with get_db() as conn:
        admins = [r[0] for r in conn.execute('SELECT chat_id FROM admins').fetchall()]
        if MAIN_ADMIN > 0 and MAIN_ADMIN not in admins:
            admins.append(MAIN_ADMIN)
        return admins

def is_admin(uid):
    return uid == MAIN_ADMIN or uid in get_admins()

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

def user_exists(chat_id):
    with get_db() as conn:
        return conn.execute('SELECT 1 FROM users WHERE chat_id = ?', (chat_id,)).fetchone() is not None

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

# ---- тикеты ----
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

# ---- пополнения / выводы ----
def add_deposit(user_id, amount, account_id, photo_id, pay_method="QR-код", status='pending'):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO deposits (user_id, amount, account_id, photo_id, status, date, timestamp, pay_method)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (user_id, amount, account_id, photo_id, status,
                   now_kg().strftime("%d.%m.%Y %H:%M:%S"), int(time.time()), pay_method))
        dep_id = c.lastrowid
        conn.commit()
        return dep_id

def update_deposit_status(dep_id, status):
    with get_db() as conn:
        conn.execute('UPDATE deposits SET status = ? WHERE id = ?', (status, dep_id))
        conn.commit()

def claim_deposit(dep_id):
    """Атомарно «захватывает» заявку (pending -> processing), чтобы не зачислить дважды."""
    with get_db() as conn:
        cur = conn.execute("UPDATE deposits SET status = 'processing' WHERE id = ? AND status = 'pending'", (dep_id,))
        conn.commit()
        return cur.rowcount == 1

def get_deposit_status(dep_id):
    with get_db() as conn:
        row = conn.execute('SELECT status FROM deposits WHERE id = ?', (dep_id,)).fetchone()
        return row[0] if row else None

def add_withdrawal(user_id, elqr, id_photo, code, amount=None):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO withdrawals (user_id, elqr_photo, id_photo, sms_code, status, date, amount)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (user_id, elqr, id_photo, code, 'completed', now_kg().strftime("%d.%m.%Y %H:%M"), amount))
        w_id = c.lastrowid
        conn.commit()
        return w_id

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
        total = c.execute('SELECT SUM(amount) FROM deposits WHERE status="approved" AND (pay_method IS NULL OR pay_method != ?)', (REF_PAY_METHOD,)).fetchone()[0] or 0
        return {'users': users, 'pending': pending, 'total': total}

# ---- рефералы ----
def set_referrer(user_id, ref_id):
    with get_db() as conn:
        conn.execute('UPDATE users SET referrer_id = ? WHERE chat_id = ? AND referrer_id IS NULL', (ref_id, user_id))
        conn.commit()

def get_referrer(user_id):
    with get_db() as conn:
        row = conn.execute('SELECT referrer_id FROM users WHERE chat_id = ?', (user_id,)).fetchone()
        return row[0] if row and row[0] else None

def get_ref_balance(user_id):
    with get_db() as conn:
        row = conn.execute('SELECT ref_balance FROM users WHERE chat_id = ?', (user_id,)).fetchone()
        return round(row[0] or 0.0, 2) if row else 0.0

def add_ref_balance(user_id, amount):
    with get_db() as conn:
        conn.execute('UPDATE users SET ref_balance = ROUND(COALESCE(ref_balance, 0) + ?, 2) WHERE chat_id = ?', (amount, user_id))
        conn.commit()

def spend_ref_balance(user_id, amount):
    """Атомарно списывает с реф. баланса. True — если средств хватило."""
    with get_db() as conn:
        cur = conn.execute(
            'UPDATE users SET ref_balance = ROUND(ref_balance - ?, 2) WHERE chat_id = ? AND COALESCE(ref_balance, 0) >= ?',
            (amount, user_id, amount))
        conn.commit()
        return cur.rowcount == 1

def get_ref_stats(user_id):
    with get_db() as conn:
        invited = conn.execute('SELECT COUNT(*) FROM users WHERE referrer_id = ?', (user_id,)).fetchone()[0]
        row = conn.execute(
            '''SELECT COUNT(*), COALESCE(SUM(d.amount), 0) FROM deposits d
               JOIN users u ON u.chat_id = d.user_id
               WHERE u.referrer_id = ? AND d.status = 'approved'
               AND (d.pay_method IS NULL OR d.pay_method != ?)''', (user_id, REF_PAY_METHOD)).fetchone()
    return {"invited": invited, "deposits": row[0], "sum": row[1] or 0.0, "balance": get_ref_balance(user_id)}

def credit_referral(user_id, amount):
    """Начисляет реферу % с принятого пополнения."""
    ref = get_referrer(user_id)
    if not ref:
        return
    bonus = round(amount * REF_PERCENT / 100.0, 2)
    if bonus <= 0:
        return
    add_ref_balance(ref, bonus)
    send_msg(ref, f"{E_GIFT} <b>Реферальный бонус!</b>\n\nВаш друг пополнил баланс — вам начислено <b>{bonus:.2f} KGS</b>.")

def add_ref_withdrawal(user_id, amount, qr_photo):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO ref_withdrawals (user_id, amount, qr_photo, status, date) VALUES (?, ?, ?, ?, ?)',
                  (user_id, amount, qr_photo, 'pending', now_kg().strftime("%d.%m.%Y %H:%M")))
        rid = c.lastrowid
        conn.commit()
        return rid

def claim_ref_withdrawal(rid, new_status):
    with get_db() as conn:
        cur = conn.execute("UPDATE ref_withdrawals SET status = ? WHERE id = ? AND status = 'pending'", (new_status, rid))
        conn.commit()
        if cur.rowcount != 1:
            return None
        return conn.execute('SELECT user_id, amount FROM ref_withdrawals WHERE id = ?', (rid,)).fetchone()

init_db()


# ======================= ОТПРАВКА =======================
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

def edit_admin_message(call, text):
    """Меняет подпись (если сообщение с фото) или текст, убирая кнопки."""
    try:
        bot.edit_message_caption(text, call.message.chat.id, call.message.message_id, reply_markup=None)
        return
    except Exception:
        pass
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

def notify_operators_ticket(tid, user_id, username, message, photo_id=None):
    uname = f"@{username}" if username else user_label(user_id)
    text = (
        f"🆘 <b>НОВОЕ ОБРАЩЕНИЕ #{tid}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>От:</b> {uname}\n"
        f"💬 <b>Текст:</b> {safe_html(message) if message else '📎 Фотография'}"
    )
    kb_t = ikb([[
        ("🔄 В работу", "primary", "cb", f"tkt_work_{tid}"),
        ("✍️ Ответить", "success", "cb", f"tkt_reply_{tid}"),
        ("❌ Закрыть", "danger", "cb", f"tkt_close_{tid}"),
    ]])
    targets = set(get_admins())
    if MAIN_ADMIN > 0:
        targets.add(MAIN_ADMIN)
    if OPERATOR_ID > 0:
        targets.add(OPERATOR_ID)
    for oid in targets:
        try:
            if photo_id:
                send_media_bulletproof(oid, photo_id, caption=text, reply_markup=kb_t)
            else:
                bot.send_message(oid, text, reply_markup=kb_t)
        except Exception as e:
            logger.warning(f"ticket notify {oid}: {e}")

def cancel_payment(user_id):
    temp_data.pop(user_id, None)
    payment_timers.pop(user_id, None)
    try:
        send_msg(user_id, "⌛ <b>Время оплаты истекло</b>\n\nЗаявка автоматически отменена. Попробуйте снова.",
                 reply_markup=main_keyboard())
    except Exception:
        pass


# ======================= КЛАВИАТУРЫ (в стиле ZenPay) =======================
def main_keyboard():
    return kb([
        [("Пополнить", "primary"), ("Вывести", "danger")],
        [("Пригласить друга", "success")],
        ["История", "Инструкция"],
        ["Поддержка"],
    ])

def platform_keyboard():
    return kb([
        [(p["title"], "primary") for p in PLATFORMS.values()],
        [("✕ Отмена", "danger")],
    ])

def cancel_keyboard():
    return kb([[("✕ Отмена", "danger")]])

def yes_no_keyboard():
    return kb([[("Да", "success"), ("Нет", "danger")]])

def ref_keyboard():
    return kb([
        [("Пополнить БК с реф. баланса", "primary")],
        [("Вывести реф. баланс", "primary")],
        [("✕ Главное меню", "danger")],
    ])

def admin_keyboard():
    active = is_bot_active()
    auto_active = is_auto_deposit_active()
    dep_on = is_deposits_enabled()
    with_on = is_withdrawals_enabled()
    return kb([
        ["📋 Заявки", "📩 Обращения"],
        ["📊 Статистика", "💳 Реквизиты"],
        [("⚙️ Авто-API: ВКЛ", "success") if auto_active else ("⚙️ Авто-API: ВЫКЛ", "danger")],
        [("🟢 Пополнение: ВКЛ", "success") if dep_on else ("🔴 Пополнение: ВЫКЛ", "danger")],
        [("🟢 Вывод: ВКЛ", "success") if with_on else ("🔴 Вывод: ВЫКЛ", "danger")],
        [("🚫 Забанить", "danger"), ("✅ Разбанить", "success")],
        ["🖼 Изменить QR", "🖼 Инструкция вывода"],
        ["➕ Админ", "➖ Удалить админа"],
        [("📢 Рассылка", "primary")],
        [("🔴 ВЫКЛ", "danger") if active else ("🟢 ВКЛ", "success")],
        [("🔙 Главное меню", "danger")],
    ])


# ======================= /start =======================
def welcome_text(name):
    return (
        f"👋 Привет, <b>{name}</b>!\n\n"
        f"💠 Добро пожаловать в <b>{BOT_NAME}</b>\n\n"
        f"<blockquote>📥 <b>Пополнение</b> — мгновенно\n"
        f"📤 <b>Вывод</b> — до 15 минут</blockquote>\n\n"
        f"{E_SUPPORT} <b>Поддержка:</b> <a href=\"{support_url()}\">@{SUPPORT.lstrip('@')}</a>"
    )

@bot.message_handler(commands=['start'])
def start(msg):
    chat_id = msg.chat.id
    if is_user_banned(chat_id):
        send_msg(chat_id, "❌ <b>Доступ ограничен.</b> Вы заблокированы администратором.")
        return

    # реферальная ссылка: /start ref_123456
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            ref = int(parts[1][4:])
            if ref != chat_id:
                pending_refs[chat_id] = ref
        except ValueError:
            pass

    if not check_subscription(chat_id):
        markup = ikb([
            [("✔️ Подписаться", "success", "url", CHANNEL_URL)],
            [("🔑 Проверить", "primary", "cb", "check_sub")],
        ])
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

@bot.callback_query_handler(func=lambda call: call.data == "none")
def callback_none(call):
    bot.answer_callback_query(call.id)

def start_welcome(msg):
    chat_id = msg.chat.id
    bot.clear_step_handler_by_chat_id(chat_id)
    if chat_id in payment_timers:
        try: payment_timers[chat_id].cancel()
        except Exception: pass
        payment_timers.pop(chat_id, None)
    temp_data[chat_id] = {}

    if not is_bot_active() and not is_admin(msg.from_user.id):
        send_msg(chat_id, "🔴 <b>Бот находится на техническом обслуживании.</b>\nПопробуйте позже.")
        return

    is_new = add_user(chat_id, username=msg.from_user.username or "",
                      full_name=msg.from_user.full_name or msg.from_user.first_name or "")
    ref = pending_refs.pop(chat_id, None)
    if is_new and ref and ref != chat_id and user_exists(ref):
        set_referrer(chat_id, ref)
        send_msg(ref, "👥 <b>Новый друг!</b>\n\nПо вашей ссылке зарегистрировался новый пользователь.")

    name = safe_html(msg.from_user.first_name or "Пользователь")
    send_msg(chat_id, welcome_text(name), reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: "Главное меню" in (m.text or "") or m.text == "🔙 Назад" or "Отмена" in (m.text or ""))
def back_to_main(msg):
    start(msg)


# ======================= ИНСТРУКЦИЯ / ПОДДЕРЖКА / ИСТОРИЯ =======================
@bot.message_handler(func=lambda m: norm(m.text) == "Инструкция")
def instruction_handler(msg):
    if is_user_banned(msg.chat.id): return
    text = (
        f"📖 <b>ИНСТРУКЦИЯ ПО ИСПОЛЬЗОВАНИЮ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"{E_CARD} <b>Как пополнить счет через QR-код:</b>\n"
        f"1️⃣ Нажмите кнопку «Пополнить».\n"
        f"2️⃣ Выберите букмекера ({platforms_label()}).\n"
        f"3️⃣ Укажите ваш ID (происходит проверка).\n"
        f"4️⃣ Подтвердите имя игрока и укажите сумму.\n"
        f"5️⃣ Оплатите по QR и отправьте скриншот чека.\n\n"
        f"{E_MONEY} <b>Как вывести средства:</b>\n"
        f"1️⃣ В приложении букмекера перейдите: <i>Настройки ➞ Вывести со счета ➞ Наличные</i>.\n"
        f"2️⃣ Укажите город: <b>Манас</b>, Улица: <b>Skorpion.KG</b>.\n"
        f"3️⃣ Подтвердите запрос и получите код.\n"
        f"4️⃣ Нажмите «Вывести» в боте, выберите букмекера, отправьте QR, введите ID и код.\n\n"
        f"{E_GIFT} <b>Реферальная программа:</b> нажмите «Пригласить друга» — "
        f"получайте {REF_PERCENT}% с каждого пополнения приглашённых."
    )
    send_msg(msg.chat.id, text, reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: norm(m.text) == "Поддержка")
def support_handler(msg):
    if is_user_banned(msg.chat.id): return
    text = (
        f"{E_SUPPORT} <b>СЛУЖБА ПОДДЕРЖКИ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"Задайте ваш вопрос текстом или отправьте фото проблемы прямо в этот чат.\n\n"
        f"📩 Прямой контакт оператора: <a href=\"{support_url()}\">@{SUPPORT.lstrip('@')}</a>"
    )
    send_msg(msg.chat.id, text, reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, support_receive_message)

def support_receive_message(msg):
    if msg.text and (msg.text.startswith("/start") or "Отмена" in msg.text):
        start(msg); return
    photo_id = msg.photo[-1].file_id if msg.photo else (
        msg.document.file_id if msg.document and (getattr(msg.document, "mime_type", None) or "").startswith("image/") else None)
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

@bot.message_handler(func=lambda m: norm(m.text) == "История")
def history_handler(msg):
    if is_user_banned(msg.chat.id): return
    uid = msg.chat.id
    st = {"approved": "✅", "pending": "⏳", "processing": "⏳", "rejected": "❌", "completed": "✅"}
    with get_db() as conn:
        deps = conn.execute('SELECT amount, account_id, status, date, pay_method FROM deposits WHERE user_id = ? ORDER BY id DESC LIMIT 7', (uid,)).fetchall()
        wds = conn.execute('SELECT amount, id_photo, status, date FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 5', (uid,)).fetchall()
        rws = conn.execute('SELECT amount, status, date FROM ref_withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 3', (uid,)).fetchall()
    if not deps and not wds and not rws:
        send_msg(uid, "🕘 <b>История пуста</b>\n\nОперации появятся здесь после первого пополнения или вывода.", reply_markup=main_keyboard())
        return
    lines = ["🕘 <b>ИСТОРИЯ ОПЕРАЦИЙ</b>", "━━━━━━━━━━━━━━━━━━━"]
    if deps:
        lines.append("\n📥 <b>Пополнения</b>")
        for amount, acc, status, date, method in deps:
            extra = f" · {E_GIFT}" if method == REF_PAY_METHOD else ""
            lines.append(f"{st.get(status, '•')} <code>{amount:.2f}</code> KGS · {safe_html(acc)}{extra}\n      <i>{date}</i>")
    if wds:
        lines.append("\n📤 <b>Выводы</b>")
        for amount, acc, status, date in wds:
            a = f"<code>{amount:.2f}</code> KGS" if amount else "—"
            lines.append(f"{st.get(status, '•')} {a} · {safe_html(acc)}\n      <i>{date}</i>")
    if rws:
        lines.append(f"\n{E_GIFT} <b>Выводы реф. баланса</b>")
        for amount, status, date in rws:
            lines.append(f"{st.get(status, '•')} <code>{amount:.2f}</code> KGS\n      <i>{date}</i>")
    send_msg(uid, "\n".join(lines), reply_markup=main_keyboard())


# ======================= ВЫБОР ПЛАТФОРМЫ (общий для пополнения/вывода) =======================
def choose_platform(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    t = norm(msg.text).lower()
    plat = next((k for k, p in PLATFORMS.items() if p["title"].lower() == t), None)
    if not plat:
        send_msg(msg.chat.id, "❌ Пожалуйста, выберите вариант кнопкой ниже:", reply_markup=platform_keyboard())
        bot.register_next_step_handler(msg, choose_platform)
        return
    data = temp_data.setdefault(msg.chat.id, {})
    data["platform"] = plat
    if data.get("action") == "withdraw":
        send_msg(msg.chat.id, "📸 <b>Отправьте QR-код вашего кошелька или банка для получения средств:</b>", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, withdraw_get_elqr)
    else:
        send_msg(msg.chat.id, f"🆔 <b>Введите ваш ID {PLATFORMS[plat]['title']}:</b>", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_account_id)


# ======================= ПОПОЛНЕНИЕ =======================
@bot.message_handler(func=lambda m: norm(m.text) == "Пополнить")
def deposit_start(msg):
    if is_user_banned(msg.chat.id): return
    if not is_deposits_enabled() and not is_admin(msg.from_user.id):
        send_msg(msg.chat.id, "⚠️ <b>Пополнения временно отключены администратором.</b> Попробуйте позже.")
        return
    if not is_bot_active() and not is_admin(msg.from_user.id):
        send_msg(msg.chat.id, "🔴 Бот на техническом обслуживании.")
        return
    temp_data[msg.chat.id] = {"action": "deposit", "pay_method": "QR-код"}
    send_msg(msg.chat.id, "Выберите, куда хотите пополнить:", reply_markup=platform_keyboard())
    bot.register_next_step_handler(msg, choose_platform)

def get_account_id(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return

    chat_id = msg.chat.id
    plat = temp_data.get(chat_id, {}).get("platform", "1XBET")
    title = PLATFORMS[plat]["title"]
    acc_number = msg.text.strip()
    if not acc_number.isdigit():
        send_msg(chat_id, f"❌ ID должен состоять только из цифр. Введите корректный ID {title}:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_account_id)
        return

    sent_msg = send_msg(chat_id, "🔍 <i>Идет проверка игрока в системе...</i>")
    try:
        data = PLATFORMS[plat]["api"].find_user(acc_number)
        if sent_msg:
            try: bot.delete_message(chat_id, sent_msg.message_id)
            except Exception: pass

        player_name = data.get('nickname') or data.get('name') or "Игрок найден"
        temp_data.setdefault(chat_id, {})["account_id"] = f"{plat} | {acc_number}"
        send_msg(chat_id, f"👤 <b>Игрок найден:</b> {safe_html(player_name)}\n\nЭто ваш аккаунт?", reply_markup=yes_no_keyboard())
        bot.register_next_step_handler(msg, confirm_player_step)
    except Exception as e:
        if sent_msg:
            try: bot.delete_message(chat_id, sent_msg.message_id)
            except Exception: pass
        send_msg(chat_id, f"❌ <b>Игрок не найден или ошибка API.</b>\n{safe_html(str(e))}\n\nПроверьте ID и введите заново:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_account_id)

def confirm_player_step(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    chat_id = msg.chat.id
    if msg.text.strip().capitalize() == "Да":
        action = temp_data.get(chat_id, {}).get("action")
        if action == "ref_deposit":
            bal = get_ref_balance(chat_id)
            send_msg(chat_id, f"{E_MONEY} <b>Введите сумму пополнения с реф. баланса:</b>\n<i>(От {REF_MIN} до {bal:.2f} KGS)</i>", reply_markup=cancel_keyboard())
            bot.register_next_step_handler(msg, get_ref_deposit_amount)
        else:
            send_msg(chat_id, f"{E_MONEY} <b>Введите сумму пополнения:</b>\n<i>(От {MIN_DEPOSIT} до {MAX_DEPOSIT} сом)</i>", reply_markup=cancel_keyboard())
            bot.register_next_step_handler(msg, get_amount)
    else:
        plat = temp_data.get(chat_id, {}).get("platform", "1XBET")
        send_msg(chat_id, f"Введите правильный ID {PLATFORMS[plat]['title']}:", reply_markup=cancel_keyboard())
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
    temp_data.setdefault(user_id, {})["amount"] = final

    qr = get_last_qr()
    if qr:
        send_media_bulletproof(user_id, qr, caption=f"📸 <b>Отсканируйте QR-код для оплаты</b>\nСумма: <b>{final:.2f} сом</b>")

    pay_markup = ikb([[("🔗 Оплатить по QR-ссылке", "primary", "url", PAY_LINK)]]) if PAY_LINK else None
    text = (
        f"🧾 <b>СЧЕТ НА ОПЛАТУ (QR-КОД)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E_CASH} <b>Сумма к оплате:</b> <code>{final:.2f}</code> KGS\n"
        f"🆔 <b>Счет игрока:</b> <code>{safe_html(account_id)}</code>\n"
        f"⏱ <b>Время на оплату:</b> 5 минут\n\n"
        f"⚠️ <i>Переводите ТОЧНУЮ сумму с копейками через QR-код!</i>\n\n"
        f"После оплаты отправьте 📸 <b>скриншот чека</b> в этот чат."
    )
    send_msg(user_id, text, reply_markup=pay_markup)
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

    markup = ikb([[
        ("✅ Одобрить", "success", "cb", f"approve_{dep_id}"),
        ("❌ Отклонить", "danger", "cb", f"reject_{dep_id}"),
    ]])
    caption = (
        f"📥 <b>ЗАЯВКА НА ПОПОЛНЕНИЕ #{dep_id}</b>\n"
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
        f"администратор проверит чек и одобрит заявку вручную.",
        reply_markup=main_keyboard()
    )
    temp_data.pop(user_id, None)


# ======================= ВЫВОД =======================
@bot.message_handler(func=lambda m: norm(m.text) == "Вывести")
def withdraw_start(msg):
    if is_user_banned(msg.chat.id): return
    if not is_withdrawals_enabled() and not is_admin(msg.from_user.id):
        send_msg(msg.chat.id, "⚠️ <b>Вывод средств временно отключен администратором.</b> Попробуйте позже.")
        return
    if not is_bot_active() and not is_admin(msg.from_user.id):
        send_msg(msg.chat.id, "🔴 Бот на техническом обслуживании.")
        return
    temp_data[msg.chat.id] = {"action": "withdraw"}
    send_msg(msg.chat.id, "Выберите, откуда хотите вывести:", reply_markup=platform_keyboard())
    bot.register_next_step_handler(msg, choose_platform)

def withdraw_get_elqr(msg):
    if msg.text and (msg.text.startswith('/start') or "Отмена" in msg.text):
        start(msg); return
    elqr = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else None)
    if not elqr:
        send_msg(msg.chat.id, "❌ Пожалуйста, отправьте изображение QR-кода:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, withdraw_get_elqr); return
    chat_id = msg.chat.id
    temp_data.setdefault(chat_id, {})["elqr"] = elqr
    plat = temp_data[chat_id].get("platform", "1XBET")
    send_msg(chat_id, f"🆔 <b>Введите ваш ID {PLATFORMS[plat]['title']}:</b>", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, withdraw_get_id)

def withdraw_get_id(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    chat_id = msg.chat.id
    plat = temp_data.get(chat_id, {}).get("platform", "1XBET")
    acc_number = msg.text.strip()
    if not acc_number.isdigit():
        send_msg(chat_id, f"❌ ID должен состоять только из цифр. Введите корректный ID {PLATFORMS[plat]['title']}:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, withdraw_get_id)
        return
    temp_data.setdefault(chat_id, {})["id_photo"] = f"{plat} | {acc_number}"

    img = get_withdraw_image()
    if img: send_media_bulletproof(chat_id, img)

    send_msg(chat_id, f"🔑 <b>Введите код подтверждения вывода из {PLATFORMS[plat]['title']}:</b>", reply_markup=cancel_keyboard())
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
    plat, raw_acc_id = split_account(id_photo)
    p_api = PLATFORMS[plat]["api"]

    sent_msg = send_msg(user_id, "🔍 <i>Проверяем код и выполняем авто-вывод через кассу...</i>")

    def drop_wait():
        if sent_msg:
            try: bot.delete_message(user_id, sent_msg.message_id)
            except Exception: pass

    try:
        order_resp = p_api.get_withdrawal_amount(raw_acc_id, code)
        payout_amount = float(order_resp.get("amount") or 0.0)

        if payout_amount <= 0:
            drop_wait()
            send_msg(user_id, "❌ <b>Неверный код ордера или нулевая сумма.</b>", reply_markup=main_keyboard())
            temp_data.pop(user_id, None)
            return

        api_resp = p_api.payout(raw_acc_id, payout_amount, code)
        logger.info(f"[API PAYOUT RESPONSE] {api_resp}")
        is_success = api_resp.get("success") or api_resp.get("Success")
        drop_wait()

        if not is_success:
            err_text = api_resp.get('Message') or api_resp.get('message') or 'Ошибка выполнения выплаты'
            send_msg(user_id, f"❌ <b>Ошибка выплаты:</b> {safe_html(err_text)}", reply_markup=main_keyboard())
            temp_data.pop(user_id, None)
            return

        w_id = add_withdrawal(user_id, elqr, id_photo, code, amount=payout_amount)
        markup = ikb([[(f"✅ Выплата выполнена (#{w_id})", "success", "cb", "none")]])
        caption = (
            f"📤 <b>АВТО-ВЫВОД УСПЕШНО ЗАВЕРШЕН #{w_id}</b>\n"
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
            f"⏳ Средства отправлены на указанный вами QR-код.",
            reply_markup=main_keyboard()
        )
    except Exception as e:
        drop_wait()
        logger.error(f"Withdraw error: {e}")
        send_msg(user_id, f"❌ <b>Ошибка связи с API кассы:</b> {safe_html(str(e)[:150])}", reply_markup=main_keyboard())

    temp_data.pop(user_id, None)


# ======================= РЕФЕРАЛЬНАЯ ПРОГРАММА =======================
@bot.message_handler(func=lambda m: norm(m.text) == "Пригласить друга")
def referral_menu(msg):
    if is_user_banned(msg.chat.id): return
    uid = msg.chat.id
    s = get_ref_stats(uid)
    link = f"https://t.me/{get_bot_username()}?start=ref_{uid}"
    text = (
        f"{E_GIFT} <b>Пригласи друга</b>\n\n"
        f"Ваша персональная ссылка:\n{link}\n\n"
        f"За каждое принятое пополнение приглашённого друга начисляется <b>{REF_PERCENT}%</b>.\n\n"
        f"👥 Приглашено друзей: <b>{s['invited']}</b>\n"
        f"✅ Пополнений друзей: <b>{s['deposits']}</b>\n"
        f"{E_CARD} Сумма пополнений друзей: <b>{s['sum']:.2f} KGS</b>\n"
        f"{E_GIFT} Доступный баланс: <b>{s['balance']:.2f} KGS</b>\n\n"
        f"Минимальная сумма пополнения или вывода: <b>{REF_MIN} KGS</b>."
    )
    send_msg(uid, text, reply_markup=ref_keyboard())

def ref_low_balance(uid):
    bal = get_ref_balance(uid)
    if bal < REF_MIN:
        send_msg(uid, f"❌ <b>Недостаточно средств.</b>\n\nДоступно: <b>{bal:.2f} KGS</b>, минимум: <b>{REF_MIN} KGS</b>.", reply_markup=ref_keyboard())
        return True
    return False

@bot.message_handler(func=lambda m: norm(m.text) == "Пополнить БК с реф. баланса")
def ref_deposit_start(msg):
    if is_user_banned(msg.chat.id): return
    if not is_deposits_enabled() and not is_admin(msg.from_user.id):
        send_msg(msg.chat.id, "⚠️ <b>Пополнения временно отключены администратором.</b> Попробуйте позже.")
        return
    if ref_low_balance(msg.chat.id): return
    temp_data[msg.chat.id] = {"action": "ref_deposit"}
    send_msg(msg.chat.id, "Выберите, куда хотите пополнить:", reply_markup=platform_keyboard())
    bot.register_next_step_handler(msg, choose_platform)

def get_ref_deposit_amount(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    uid = msg.chat.id
    try:
        amount = round(float(msg.text.replace(',', '.')), 2)
    except Exception:
        send_msg(uid, "❌ Пожалуйста, введите корректную сумму числом:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_ref_deposit_amount); return

    bal = get_ref_balance(uid)
    if amount < REF_MIN or amount > bal:
        send_msg(uid, f"❌ Сумма должна быть от <b>{REF_MIN}</b> до <b>{bal:.2f}</b> KGS:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_ref_deposit_amount); return

    account_id = temp_data.get(uid, {}).get("account_id")
    if not account_id:
        send_msg(uid, "❌ Сессия истекла. Начните заново.", reply_markup=ref_keyboard()); return
    plat, raw_acc = split_account(account_id)

    if not spend_ref_balance(uid, amount):
        send_msg(uid, "❌ Недостаточно средств на реф. балансе.", reply_markup=ref_keyboard()); return

    sent_msg = send_msg(uid, "⏳ <i>Зачисляем на ваш аккаунт...</i>")
    try:
        resp = PLATFORMS[plat]["api"].deposit(raw_acc, amount)
        logger.info(f"[REF DEPOSIT RESPONSE] {resp}")
        if not (resp.get("success") or resp.get("Success")):
            raise Exception(resp.get('Message') or resp.get('message') or resp.get('error') or "Касса отклонила операцию")
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        # Неизвестно, прошла ли операция — деньги НЕ возвращаем, проверяет админ
        logger.error(f"Ref deposit uncertain: {e}")
        if sent_msg:
            try: bot.delete_message(uid, sent_msg.message_id)
            except Exception: pass
        send_msg(uid, "⚠️ <b>Статус операции уточняется.</b>\n\nКасса не ответила вовремя. Мы проверим зачисление вручную и сообщим вам.", reply_markup=main_keyboard())
        for admin in get_admins():
            send_msg(admin, f"⚠️ <b>ПРОВЕРЬТЕ ВРУЧНУЮ</b>: пополнение с реф. баланса не получило ответа кассы.\n👤 {user_label(uid)}\n🆔 <code>{safe_html(account_id)}</code>\n💰 <code>{amount:.2f}</code> KGS (списано с реф. баланса)")
        temp_data.pop(uid, None)
        return
    except Exception as e:
        add_ref_balance(uid, amount)  # возврат — операция точно не прошла
        if sent_msg:
            try: bot.delete_message(uid, sent_msg.message_id)
            except Exception: pass
        send_msg(uid, f"❌ <b>Не удалось пополнить:</b> {safe_html(str(e)[:150])}\n\nСредства возвращены на реф. баланс.", reply_markup=ref_keyboard())
        temp_data.pop(uid, None)
        return

    if sent_msg:
        try: bot.delete_message(uid, sent_msg.message_id)
        except Exception: pass
    dep_id = add_deposit(uid, amount, account_id, None, pay_method=REF_PAY_METHOD, status='approved')
    send_msg(uid,
             f"🎉 <b>АККАУНТ ПОПОЛНЕН С РЕФ. БАЛАНСА!</b>\n"
             f"━━━━━━━━━━━━━━━━━━━\n"
             f"{E_MONEY} <b>Зачислено:</b> <code>{amount:.2f}</code> KGS\n"
             f"🆔 <b>Счет игрока:</b> <code>{safe_html(account_id)}</code>\n"
             f"{E_GIFT} <b>Остаток реф. баланса:</b> <code>{get_ref_balance(uid):.2f}</code> KGS",
             reply_markup=main_keyboard())
    for admin in get_admins():
        send_msg(admin, f"{E_GIFT} Пополнение с реф. баланса #{dep_id}\n👤 {user_label(uid)}\n🆔 <code>{safe_html(account_id)}</code>\n💰 <code>{amount:.2f}</code> KGS")
    temp_data.pop(uid, None)

@bot.message_handler(func=lambda m: norm(m.text) == "Вывести реф. баланс")
def ref_withdraw_start(msg):
    if is_user_banned(msg.chat.id): return
    if not is_withdrawals_enabled() and not is_admin(msg.from_user.id):
        send_msg(msg.chat.id, "⚠️ <b>Вывод средств временно отключен администратором.</b> Попробуйте позже.")
        return
    uid = msg.chat.id
    if ref_low_balance(uid): return
    bal = get_ref_balance(uid)
    temp_data[uid] = {"action": "ref_withdraw"}
    send_msg(uid, f"{E_MONEY} <b>Введите сумму вывода:</b>\n<i>(От {REF_MIN} до {bal:.2f} KGS)</i>", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, get_ref_withdraw_amount)

def get_ref_withdraw_amount(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        start(msg); return
    uid = msg.chat.id
    try:
        amount = round(float(msg.text.replace(',', '.')), 2)
    except Exception:
        send_msg(uid, "❌ Пожалуйста, введите корректную сумму числом:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_ref_withdraw_amount); return
    bal = get_ref_balance(uid)
    if amount < REF_MIN or amount > bal:
        send_msg(uid, f"❌ Сумма должна быть от <b>{REF_MIN}</b> до <b>{bal:.2f}</b> KGS:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_ref_withdraw_amount); return
    temp_data.setdefault(uid, {})["ref_amount"] = amount
    send_msg(uid, "📸 <b>Отправьте QR-код вашего кошелька или банка для получения средств:</b>", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, get_ref_withdraw_qr)

def get_ref_withdraw_qr(msg):
    if msg.text and (msg.text.startswith('/start') or "Отмена" in msg.text):
        start(msg); return
    uid = msg.chat.id
    qr = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else None)
    if not qr:
        send_msg(uid, "❌ Пожалуйста, отправьте изображение QR-кода:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, get_ref_withdraw_qr); return
    amount = temp_data.get(uid, {}).get("ref_amount")
    if not amount:
        send_msg(uid, "❌ Сессия истекла. Начните заново.", reply_markup=ref_keyboard()); return
    if not spend_ref_balance(uid, amount):
        send_msg(uid, "❌ Недостаточно средств на реф. балансе.", reply_markup=ref_keyboard()); return

    touch_user(msg)
    rid = add_ref_withdrawal(uid, amount, qr)
    markup = ikb([[
        ("✅ Выплачено", "success", "cb", f"rw_ok_{rid}"),
        ("❌ Отклонить", "danger", "cb", f"rw_no_{rid}"),
    ]])
    caption = (
        f"{E_GIFT} <b>ВЫВОД РЕФ. БАЛАНСА #{rid}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Игрок:</b> {user_label(uid)}\n"
        f"💰 <b>Сумма:</b> <code>{amount:.2f}</code> KGS"
    )
    for admin in get_admins():
        send_media_bulletproof(admin, qr, caption=caption, reply_markup=markup)
    send_msg(uid, f"⏳ <b>Заявка на вывод #{rid} принята!</b>\n\nСумма: <b>{amount:.2f} KGS</b>. Оператор выплатит средства на ваш QR-код.", reply_markup=main_keyboard())
    temp_data.pop(uid, None)


# ======================= АДМИН-ПАНЕЛЬ =======================
def admin_only(m):
    return is_admin(m.from_user.id)

@bot.message_handler(commands=["admin"])
@bot.message_handler(func=lambda m: m.text in ["⚙️ Admin", "Admin"] and admin_only(m))
def admin_panel(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        send_msg(msg.chat.id, "❌ У вас нет прав доступа.")
        return
    send_msg(msg.chat.id, f"⚙️ <b>ПАНЕЛЬ АДМИНИСТРАТОРА {BOT_NAME}</b>\n\nВаш ID: <code>{uid}</code>\nКасса Код: <code>{CASHBOX_CODE}</code>", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "💳 Реквизиты" in (m.text or "") and admin_only(m))
def edit_reqs(msg):
    curr = get_requisites()
    send_msg(msg.chat.id, f"💳 <b>ТЕКУЩИЕ ДАННЫЕ / РЕКВИЗИТЫ:</b>\n<code>{safe_html(curr)}</code>\n\nОтправьте новые данные сообщением:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, save_reqs_proc)

def save_reqs_proc(msg):
    if not msg.text or "Отмена" in msg.text:
        admin_panel(msg); return
    save_requisites(msg.text.strip())
    send_msg(msg.chat.id, "✅ Данные успешно обновлены!", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "Авто-API" in (m.text or "") and admin_only(m))
def toggle_auto_deposit(msg):
    new_status = not is_auto_deposit_active()
    set_auto_deposit_active(new_status)
    status_text = "🟢 ВКЛЮЧЕНО" if new_status else "🔴 ВЫКЛЮЧЕНО"
    send_msg(msg.chat.id, f"⚙️ Авто-зачисление через API кассы при одобрении: <b>{status_text}</b>", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "Пополнение:" in (m.text or "") and admin_only(m))
def toggle_deposits_handler(msg):
    new_status = not is_deposits_enabled()
    set_deposits_enabled(new_status)
    status_text = "🟢 ВКЛЮЧЕНО" if new_status else "🔴 ВЫКЛЮЧЕНО"
    send_msg(msg.chat.id, f"💳 Прием пополнений от пользователей: <b>{status_text}</b>", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "Вывод:" in (m.text or "") and admin_only(m))
def toggle_withdrawals_handler(msg):
    new_status = not is_withdrawals_enabled()
    set_withdrawals_enabled(new_status)
    status_text = "🟢 ВКЛЮЧЕНО" if new_status else "🔴 ВЫКЛЮЧЕНО"
    send_msg(msg.chat.id, f"📤 Обработка выводов средств: <b>{status_text}</b>", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "🚫 Забанить" in (m.text or "") and admin_only(m))
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

@bot.message_handler(func=lambda m: "✅ Разбанить" in (m.text or "") and admin_only(m))
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

@bot.message_handler(func=lambda m: m.text in ["➕ Админ", "Админ"] and admin_only(m))
def add_admin_btn(msg):
    send_msg(msg.chat.id, "Введите ID нового администратора:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, process_add_admin)

def process_add_admin(msg):
    if not msg.text or msg.text.startswith('/start') or "Отмена" in msg.text:
        admin_panel(msg); return
    try:
        add_admin(int(msg.text.strip()))
        send_msg(msg.chat.id, "✅ Новый администратор успешно добавлен!", reply_markup=admin_keyboard())
    except Exception:
        send_msg(msg.chat.id, "❌ Ошибка! Некорректный ID.", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: m.text in ["➖ Удалить админа", "Удалить админа"] and admin_only(m))
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

@bot.message_handler(func=lambda m: m.text in ("🔴 ВЫКЛ", "🟢 ВКЛ") and admin_only(m))
def toggle_bot(msg):
    active = "ВКЛ" in msg.text
    set_bot_active(active)
    send_msg(msg.chat.id, "🟢 Бот ВКЛЮЧЕН" if active else "🔴 Бот ВЫКЛЮЧЕН", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: "Изменить QR" in (m.text or "") and admin_only(m))
def change_qr(msg):
    send_msg(msg.chat.id, "📸 Отправьте изображение нового QR-кода:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, save_new_qr)

def save_new_qr(msg):
    if msg.text and (msg.text.startswith('/start') or "Отмена" in msg.text):
        admin_panel(msg); return
    file_id = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else None)
    if file_id:
        save_qr(file_id)
        send_msg(msg.chat.id, "✅ Новый QR-код успешно сохранен!", reply_markup=admin_keyboard())
    else:
        send_msg(msg.chat.id, "❌ Пожалуйста, отправьте изображение QR-кода:", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, save_new_qr)

@bot.message_handler(func=lambda m: "Инструкция вывода" in (m.text or "") and admin_only(m))
def change_withdraw_image(msg):
    send_msg(msg.chat.id, "📸 Отправьте новое изображение инструкции вывода:", reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, save_new_withdraw_image)

def save_new_withdraw_image(msg):
    if msg.text and (msg.text.startswith('/start') or "Отмена" in msg.text):
        admin_panel(msg); return
    file_id = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else None)
    if file_id:
        save_withdraw_image(file_id)
        send_msg(msg.chat.id, "✅ Изображение инструкции вывода сохранено!", reply_markup=admin_keyboard())
    else:
        send_msg(msg.chat.id, "❌ Пожалуйста, отправьте изображение!", reply_markup=cancel_keyboard())
        bot.register_next_step_handler(msg, save_new_withdraw_image)

@bot.message_handler(func=lambda m: m.text in ["📋 Заявки", "Заявки"] and admin_only(m))
def view_requests(msg):
    deposits = get_pending_deposits()
    if not deposits:
        send_msg(msg.chat.id, "✅ Нет активных заявок на обработку.")
        return
    for dep in deposits:
        dep_id, user_id, amount, account_id, photo_id, date, ts, pay_method = dep
        markup = ikb([[
            ("✅ Одобрить", "success", "cb", f"approve_{dep_id}"),
            ("❌ Отклонить", "danger", "cb", f"reject_{dep_id}"),
        ]])
        caption = (
            f"📥 <b>ЗАЯВКА #{dep_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🏦 <b>Метод:</b> {pay_method or 'QR-код'}\n"
            f"👤 Игрок: {user_label(user_id)}\n"
            f"💰 Сумма: <code>{amount:.2f}</code> KGS\n"
            f"🆔 Счет: <code>{safe_html(account_id)}</code>"
        )
        send_media_bulletproof(msg.chat.id, photo_id, caption=caption, reply_markup=markup)

@bot.message_handler(func=lambda m: "📊 Статистика" in (m.text or "") and admin_only(m))
def stats(msg):
    s = get_stats()
    auto_status = "ВКЛЮЧЕНО ⚡️" if is_auto_deposit_active() else "ВЫКЛЮЧЕНО 🔴"
    text = (
        f"📊 <b>СТАТИСТИКА БОТА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Всего пользователей: <b>{s['users']}</b>\n"
        f"⏳ Заявок в очереди: <b>{s['pending']}</b>\n"
        f"💰 Успешных пополнений: <b>{s['total']:.2f} сом</b>\n"
        f"⚡️ Авто-API при одобрении: <b>{auto_status}</b>"
    )
    send_msg(msg.chat.id, text)

@bot.message_handler(func=lambda m: "Рассылка" in (m.text or "") and admin_only(m))
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

@bot.message_handler(func=lambda m: m.text in ["📩 Обращения", "Обращения"] and (is_admin(m.from_user.id) or m.from_user.id == OPERATOR_ID))
def operator_tickets(msg):
    rows = list_tickets(limit=15)
    if not rows:
        send_msg(msg.chat.id, "✅ Активных обращений нет.")
        return
    status_map = {"new": "🆕 Новое", "work": "🔄 В работе", "closed": "✅ Закрыто"}
    for tid, user_id, username, message, status, created in rows:
        st = status_map.get(status, status)
        uname = f"@{username}" if username else str(user_id)
        kb_t = None
        if status != "closed":
            kb_t = ikb([[
                ("🔄 В работу", "primary", "cb", f"tkt_work_{tid}"),
                ("✍️ Ответить", "success", "cb", f"tkt_reply_{tid}"),
                ("❌ Закрыть", "danger", "cb", f"tkt_close_{tid}"),
            ]])
        send_msg(msg.chat.id, f"🆘 <b>ОБРАЩЕНИЕ #{tid}</b> · {st}\n👤 {uname} (<code>{user_id}</code>)\n📅 {created}\n\n💬 {safe_html(message[:500])}", reply_markup=kb_t)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("tkt_"))
def ticket_callbacks(call):
    if not is_admin(call.from_user.id) and call.from_user.id != OPERATOR_ID:
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
        send_msg(user_id, f"🔄 Ваш запрос #{tid} взят в работу оператором.")
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception: pass
        return

    if action == "close":
        set_ticket_status(tid, "closed")
        bot.answer_callback_query(call.id, "Закрыто")
        send_msg(user_id, f"✅ Ваш запрос #{tid} решен и закрыт.")
        edit_admin_message(call, (call.message.text or call.message.caption or "") + "\n\n✅ Закрыто")
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

@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_', 'rw_ok_', 'rw_no_')))
def handle_admin_callbacks(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Нет прав доступа!"); return
    data = call.data

    # ---------- одобрение пополнения ----------
    if data.startswith('approve_'):
        dep_id = int(data.split('_')[1])
        if not claim_deposit(dep_id):
            bot.answer_callback_query(call.id, f"⚠️ Эта заявка уже обработана! (Статус: {get_deposit_status(dep_id)})", show_alert=True)
            return

        with get_db() as conn:
            row = conn.execute('SELECT user_id, amount, account_id, timestamp FROM deposits WHERE id = ?', (dep_id,)).fetchone()
        if not row:
            update_deposit_status(dep_id, "pending")
            bot.answer_callback_query(call.id, "Заявка не найдена", show_alert=True)
            return

        user_id, amount, account_id, ts = row
        plat, raw_acc_id = split_account(account_id)

        if is_auto_deposit_active():
            try:
                api_resp = PLATFORMS[plat]["api"].deposit(raw_acc_id, amount)
                logger.info(f"[API DEPOSIT RESPONSE] {api_resp}")
                if not (api_resp.get("success") or api_resp.get("Success")):
                    err_text = api_resp.get('Message') or api_resp.get('message') or api_resp.get('error') or str(api_resp)
                    update_deposit_status(dep_id, "pending")
                    bot.answer_callback_query(call.id, f"❌ Ошибка кассы: {err_text}"[:200], show_alert=True)
                    return
            except requests.exceptions.HTTPError as http_err:
                err_msg = str(http_err)
                try:
                    err_json = http_err.response.json()
                    err_msg = err_json.get("Message") or err_json.get("message") or err_json.get("error") or str(err_json)
                except Exception:
                    err_msg = http_err.response.text or err_msg
                update_deposit_status(dep_id, "pending")
                bot.answer_callback_query(call.id, f"❌ Ошибка API: {str(err_msg)[:150]}", show_alert=True)
                return
            except Exception as e:
                # при таймауте неясно, прошла ли операция — вернули в очередь, проверьте кассу вручную
                update_deposit_status(dep_id, "pending")
                bot.answer_callback_query(call.id, f"❌ Ошибка связи с API: {str(e)[:150]}\nПроверьте кассу перед повтором!", show_alert=True)
                return

        update_deposit_status(dep_id, "approved")
        bot.answer_callback_query(call.id, "✅ Заявка одобрена и успешно зачислена!")
        elapsed = int(time.time()) - (ts or int(time.time()))

        send_msg(
            user_id,
            f"🎉 <b>БАЛАНС УСПЕШНО ПОПОЛНЕН!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{E_MONEY} <b>Зачислено:</b> <code>{amount:.2f}</code> KGS\n"
            f"🆔 <b>Счет игрока:</b> <code>{safe_html(account_id)}</code>\n"
            f"⏱ <b>Время обработки:</b> {elapsed} сек.\n\n"
            f"Спасибо за выбор {BOT_NAME}! {E_ROCKET}"
        )
        credit_referral(user_id, amount)
        edit_admin_message(call, f"✅ <b>Заявка #{dep_id} ОДОБРЕНА И ЗАСЧИТАНА (Админ: {safe_html(call.from_user.first_name)})</b>")

    # ---------- отклонение пополнения ----------
    elif data.startswith('reject_'):
        dep_id = int(data.split('_')[1])
        if not claim_deposit(dep_id):
            bot.answer_callback_query(call.id, f"⚠️ Эта заявка уже обработана! (Статус: {get_deposit_status(dep_id)})", show_alert=True)
            return
        with get_db() as conn:
            row = conn.execute('SELECT user_id, amount FROM deposits WHERE id = ?', (dep_id,)).fetchone()
        update_deposit_status(dep_id, "rejected")
        bot.answer_callback_query(call.id, "❌ Заявка отклонена!")
        if row:
            user_id, amount = row
            send_msg(user_id, f"❌ <b>Заявка на {amount:.2f} сом была отклонена (неверный чек/оплата не поступила).</b>\nЕсли возникли вопросы, обратитесь в поддержку: <a href=\"{support_url()}\">@{SUPPORT.lstrip('@')}</a>")
        edit_admin_message(call, f"❌ <b>Заявка #{dep_id} ОТКЛОНЕНА (Админ: {safe_html(call.from_user.first_name)})</b>")

    # ---------- вывод реф. баланса: выплачено ----------
    elif data.startswith('rw_ok_'):
        rid = int(data.split('_')[2])
        r = claim_ref_withdrawal(rid, "completed")
        if not r:
            bot.answer_callback_query(call.id, "⚠️ Эта заявка уже обработана!", show_alert=True); return
        bot.answer_callback_query(call.id, "✅ Отмечено как выплачено")
        send_msg(r[0], f"✅ <b>Вывод реф. баланса #{rid} выполнен!</b>\n\nСумма: <b>{r[1]:.2f} KGS</b>.")
        edit_admin_message(call, f"✅ <b>ВЫВОД РЕФ. БАЛАНСА #{rid} ВЫПЛАЧЕН (Админ: {safe_html(call.from_user.first_name)})</b>")

    # ---------- вывод реф. баланса: отклонён (возврат) ----------
    elif data.startswith('rw_no_'):
        rid = int(data.split('_')[2])
        r = claim_ref_withdrawal(rid, "rejected")
        if not r:
            bot.answer_callback_query(call.id, "⚠️ Эта заявка уже обработана!", show_alert=True); return
        add_ref_balance(r[0], r[1])
        bot.answer_callback_query(call.id, "Отклонено, сумма возвращена на реф. баланс")
        send_msg(r[0], f"❌ <b>Вывод реф. баланса #{rid} отклонён.</b>\nСумма <b>{r[1]:.2f} KGS</b> возвращена на реф. баланс.\nПоддержка: <a href=\"{support_url()}\">@{SUPPORT.lstrip('@')}</a>")
        edit_admin_message(call, f"❌ <b>ВЫВОД РЕФ. БАЛАНСА #{rid} ОТКЛОНЁН (Админ: {safe_html(call.from_user.first_name)})</b>")


# ======================= FLASK / MINI APP =======================
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
    try:
        uid = int(data.get("id") or 0)
    except (TypeError, ValueError):
        uid = 0
    pin = str(data.get("pin") or "")
    if uid and is_admin(uid) and pin == str(ADMIN_PIN):
        return jsonify({"ok": True, "stats": get_stats(), "admin": True})
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

def setup_bot_description():
    """Экран «Что умеет этот бот?» до нажатия Старт (картинку ставьте через @BotFather → Edit Bot → Edit Description Picture)."""
    desc = (
        f"💎 Добро пожаловать в {BOT_NAME}!\n\n"
        f"🚀 Ваш надёжный помощник в пополнении и выводе средств с {platforms_label()}!\n\n"
        f"⚡ Быстрая обработка заявок\n"
        f"💸 Комиссия — 0%\n"
        f"🏦 Удобная оплата через банки Кыргызстана\n"
        f"🔐 Безопасные операции\n"
        f"🕐 Работаем 24/7\n"
        f"🧑‍💻 Поддержка всегда на связи\n\n"
        f"💙 {BOT_NAME} — быстро, удобно, надёжно!\n\n"
        f"👇 Нажмите «Старт» и начните пользоваться ботом!"
    )
    try:
        bot.set_my_description(desc[:512])
        bot.set_my_short_description(f"{BOT_NAME} — быстрое пополнение и вывод 1xBet / Melbet. Комиссия 0%, 24/7."[:120])
    except Exception as e:
        logger.warning(f"set_my_description: {e}")


if __name__ == "__main__":
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception as e:
        logger.warning(f"remove_webhook: {e}")

    setup_menu_button()
    setup_bot_description()

    def run_flask():
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)

    threading.Thread(target=run_flask, daemon=True).start()

    logger.info(f"{BOT_NAME} запущен | admin={MAIN_ADMIN} | cashbox_code={CASHBOX_CODE} | platforms={list(PLATFORMS)}")
    while True:
        try:
            bot.polling(none_stop=True, interval=1, timeout=40)
        except Exception as e:
            logger.error(f"Ошибка polling: {e}")
            time.sleep(5)
