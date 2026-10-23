# -*- coding: utf-8 -*-
"""
ربات فروشگاهی تلگرام
نیازمندی: pip install pyTelegramBotAPI
اجرا:
    export BOT_TOKEN="توکن جدید از BotFather"
    python vv.py
"""

import os
import sqlite3
import logging
import threading
import time
from datetime import datetime, timedelta
from contextlib import contextmanager

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# ───────────────────────── تنظیمات ─────────────────────────

# ┌─────────────────────────────────────────────────────────────┐
# │ این دو مقدار را خودت پر کن:                                 │
# │ 1) توکن ربات را از BotFather داخل BOT_TOKEN قرار بده.        │
# │ 2) آیدی عددی تلگرام ادمین را داخل ADMIN_CHAT_ID قرار بده.    │
# │ توکن واقعی را در فایل عمومی GitHub منتشر نکن.                │
# └─────────────────────────────────────────────────────────────┘
BOT_TOKEN = "8844989420:AAF-ldyVZ1jApTv0_JpdOTb4XqEdiH9_erc"
ADMIN_CHAT_ID = 6889501272  # این عدد را با آیدی عددی خودت عوض کن

TOKEN = BOT_TOKEN.strip()
if not TOKEN or TOKEN == "توکن_ربات_را_اینجا_قرار_بده":
    raise RuntimeError("ابتدا مقدار BOT_TOKEN را در ابتدای فایل bot.py وارد کن.")
if not isinstance(ADMIN_CHAT_ID, int) or ADMIN_CHAT_ID == 123456789:
    raise RuntimeError("ابتدا آیدی عددی ادمین را در مقدار ADMIN_CHAT_ID وارد کن.")

CARD_NUMBER = os.getenv("CARD_NUMBER", "").strip()
CARD_HOLDER = os.getenv("CARD_HOLDER", "مدیر فروشگاه").strip()
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://h73128149-cpu.github.io/mini-app/")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Hesvpnn")

DEFAULT_CONFIG = os.getenv("DEFAULT_CONFIG", "")
TEST_CONFIG = os.getenv("TEST_CONFIG", "")
DB_PATH = os.getenv("DB_PATH", "users.db")

PLANS = {
    50000: {"name": "۱۰ گیگ (۱ ماهه)", "label": "۱۰ گیگ (۵۰ هزار تومان) - ۱ ماهه"},
    100000: {"name": "۲۰ گیگ (۱ ماهه)", "label": "۲۰ گیگ (۱۰۰ هزار تومان) - ۱ ماهه"},
    300000: {"name": "۳۰ گیگ (۱ ماهه)", "label": "۳۰ گیگ (۳۰۰ هزار تومان) - ۱ ماهه"},
    350000: {"name": "نامحدود (۱ ماهه)", "label": "نامحدود (۳۵۰ هزار تومان) - ۱ ماهه"},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("telegram-shop")
bot = telebot.TeleBot(TOKEN, parse_mode=None)

# وضعیت‌های موقت؛ اطلاعات مهم پرداخت در SQLite نیز ثبت می‌شوند.
user_orders = {}
admin_states = {}
active_coupons = {}
last_notice_scan = 0


# ───────────────────────── پایگاه داده ─────────────────────────

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                join_date TEXT NOT NULL,
                referred_by INTEGER,
                wallet INTEGER NOT NULL DEFAULT 0 CHECK(wallet >= 0),
                expire_date TEXT,
                test_used INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL CHECK(amount > 0),
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by INTEGER,
                coupon_code TEXT,
                UNIQUE(user_id, id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                plan_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                paid_from_wallet INTEGER NOT NULL DEFAULT 0,
                config_sent INTEGER NOT NULL DEFAULT 0,
                coupon_code TEXT
            )
        """)
        conn.execute("""CREATE TABLE IF NOT EXISTS coupons (
            code TEXT PRIMARY KEY, percent INTEGER NOT NULL CHECK(percent BETWEEN 1 AND 90),
            max_uses INTEGER NOT NULL DEFAULT 0, uses INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL, replied_at TEXT, admin_reply TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS referral_rewards (
            order_id INTEGER PRIMARY KEY, referred_user INTEGER NOT NULL,
            referrer_id INTEGER NOT NULL, amount INTEGER NOT NULL, created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, event TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS subscription_notices (
            user_id INTEGER NOT NULL, expire_date TEXT NOT NULL, notice_type TEXT NOT NULL,
            sent_at TEXT NOT NULL, PRIMARY KEY(user_id, expire_date, notice_type)
        )""")
        # Migration for existing databases created by older versions.
        for table, column, definition in [
            ('payments', 'coupon_code', 'TEXT'), ('orders', 'coupon_code', 'TEXT'), ('users', 'loyalty_points', 'INTEGER NOT NULL DEFAULT 0')
        ]:
            cols = [r['name'] for r in conn.execute(f'PRAGMA table_info({table})')]
            if column not in cols:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referred_by)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)")


def audit_event(actor_id, event, details=""):
    try:
        with db() as conn:
            conn.execute("INSERT INTO audit_log(actor_id,event,details,created_at) VALUES(?,?,?,?)",
                         (actor_id, event, str(details)[:1000], now_text()))
    except Exception:
        logger.exception("Could not write audit log")


def coupon_discount(user_id, amount):
    info = active_coupons.get(user_id)
    if not info:
        return amount, None, 0
    with db() as conn:
        row = conn.execute("SELECT * FROM coupons WHERE code=? AND active=1", (info['code'],)).fetchone()
    if not row or (row['max_uses'] and row['uses'] >= row['max_uses']):
        active_coupons.pop(user_id, None)
        return amount, None, 0
    discounted = max(1, int(amount * (100 - row['percent']) / 100))
    return discounted, row['code'], row['percent']


def consume_coupon(code):
    if not code:
        return
    with db() as conn:
        conn.execute("UPDATE coupons SET uses=uses+1 WHERE code=? AND active=1 AND (max_uses=0 OR uses<max_uses)", (code,))


def reward_referrer(order_id, referred_user, amount):
    """Pay referral commission once per order, in the same DB transaction."""
    reward = 0
    referrer_id = None
    with db() as conn:
        row = conn.execute("SELECT referred_by FROM users WHERE user_id=?", (referred_user,)).fetchone()
        if not row or not row["referred_by"]:
            return 0, None
        referrer_id = int(row["referred_by"])
        reward = int(amount * 0.10)
        if reward <= 0:
            return 0, referrer_id
        cur = conn.execute(
            "INSERT OR IGNORE INTO referral_rewards(order_id,referred_user,referrer_id,amount,created_at) VALUES(?,?,?,?,?)",
            (order_id, referred_user, referrer_id, reward, now_text()),
        )
        if cur.rowcount != 1:
            return 0, referrer_id
        conn.execute("UPDATE users SET wallet=wallet+? WHERE user_id=?", (reward, referrer_id))
    # Write audit only after the wallet transaction has committed (avoid nested DB lock).
    audit_event(referred_user, "referral_reward", f"order={order_id};referrer={referrer_id};amount={reward}")
    return reward, referrer_id


def calculate_expiry(user_id, days=30):
    """Extend an active subscription instead of shortening it on renewal."""
    now = datetime.now()
    with db() as conn:
        row = conn.execute("SELECT expire_date FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row["expire_date"]:
        try:
            current = datetime.strptime(row["expire_date"], "%Y-%m-%d %H:%M:%S")
            if current > now:
                now = current
        except ValueError:
            pass
    return (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def expiry_notifier():
    """Send each 24-hour and expiry notice at most once per subscription expiry."""
    while True:
        try:
            now = datetime.now()
            with db() as conn:
                rows = conn.execute("SELECT user_id,expire_date FROM users WHERE expire_date IS NOT NULL AND expire_date != ''").fetchall()
            for row in rows:
                try:
                    exp = datetime.strptime(row['expire_date'], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                kind = None
                if now < exp <= now + timedelta(hours=24):
                    kind = '24h'
                elif exp <= now:
                    kind = 'expired'
                if not kind:
                    continue
                with db() as conn:
                    cur = conn.execute("INSERT OR IGNORE INTO subscription_notices(user_id,expire_date,notice_type,sent_at) VALUES(?,?,?,?)",
                                       (row['user_id'], row['expire_date'], kind, now_text()))
                if cur.rowcount:
                    msg = ("⏰ اشتراک شما کمتر از ۲۴ ساعت دیگر منقضی می‌شود. برای تمدید اقدام کنید." if kind == '24h'
                           else "⚠️ اشتراک شما منقضی شده است. برای تمدید وارد بخش خرید شوید.")
                    try:
                        bot.send_message(row['user_id'], msg)
                    except Exception:
                        pass
        except Exception:
            logger.exception("Expiry notifier failed")
        time.sleep(300)


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_user(user):
    user_id = user.id
    first_name = user.first_name or "کاربر"
    username = f"@{user.username}" if user.username else "بدون آیدی"
    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users
                (user_id, first_name, username, phone, join_date)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, first_name, username, "ثبت نشده", now_text()))
        conn.execute("""
            UPDATE users SET first_name = ?, username = ?
            WHERE user_id = ?
        """, (first_name, username, user_id))


def is_admin(user_id):
    return user_id == ADMIN_CHAT_ID


def check_membership(user_id):
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as exc:
        logger.warning("Membership check failed for %s: %s", user_id, exc)
        return False


def safe_edit(call, text, reply_markup=None, parse_mode=None):
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=reply_markup, parse_mode=parse_mode,
        )
    except Exception:
        bot.send_message(
            call.message.chat.id, text,
            reply_markup=reply_markup, parse_mode=parse_mode,
        )


def back_button(callback="home"):
    """دکمهٔ بازگشت و دسترسی مستقیم به منوی اصلی."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔙 بازگشت", callback_data=callback),
        InlineKeyboardButton("🏠 منوی اصلی", callback_data="home"),
    )
    return markup


def send_membership_prompt(chat_id):
    markup = InlineKeyboardMarkup()
    channel_name = REQUIRED_CHANNEL.lstrip("@")
    markup.add(InlineKeyboardButton(
        "📢 عضویت در کانال",
        url=f"https://t.me/{channel_name}",
    ))
    markup.add(InlineKeyboardButton(
        "✅ عضو شدم (بررسی مجدد)", callback_data="check_join"
    ))
    bot.send_message(
        chat_id,
        "❌ برای استفاده از ربات، ابتدا باید در کانال عضو شوید.\n\n"
        "پس از عضویت، روی «عضو شدم» بزنید.",
        reply_markup=markup,
    )


# ───────────────────────── منوها ─────────────────────────

def send_main_menu(chat_id, user_name, user_id):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🚀 مینی‌اپ فروشگاه", web_app=WebAppInfo(url=WEB_APP_URL)),
        InlineKeyboardButton("🛒 خرید سریع", callback_data="shop"),
    )
    markup.add(
        InlineKeyboardButton("🎁 تست رایگان (۲۴س)", callback_data="free_test"),
        InlineKeyboardButton("👤 حساب / اشتراک من", callback_data="my_account"),
    )
    markup.add(
        InlineKeyboardButton("💳 کیف پول", callback_data="wallet"),
        InlineKeyboardButton("🎁 دعوت از دوستان (۱۰٪)", callback_data="ref"),
    )
    markup.add(
        InlineKeyboardButton("📚 آموزش اتصال", callback_data="guide"),
        InlineKeyboardButton("📩 پشتیبانی و تیکت", callback_data="support"),
    )
    markup.add(
        InlineKeyboardButton("⭐ باشگاه مشتریان", callback_data="loyalty"),
        InlineKeyboardButton("🧾 تاریخچه سفارش‌ها", callback_data="order_history"),
    )
    if is_admin(user_id):
        markup.add(InlineKeyboardButton("⚙️ پنل مدیریت", callback_data="admin"))

    bot.send_message(
        chat_id,
        f"سلام {user_name} عزیز 👋\nبه ربات فروشگاه خوش آمدید.\n"
        "لطفاً گزینهٔ موردنظر را انتخاب کنید:",
        reply_markup=markup,
    )


@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_id = message.from_user.id
    if not check_membership(user_id):
        send_membership_prompt(message.chat.id)
        return

    # ابتدا کاربر ثبت می‌شود؛ لینک معرفی فقط هنگام ثبت اولیه اعمال می‌شود.
    existed = False
    with db() as conn:
        existed = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ).fetchone() is not None

    ensure_user(message.from_user)

    if not existed:
        referred_by = None
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("ref_"):
            try:
                ref_id = int(parts[1][4:])
                if ref_id != user_id:
                    with db() as conn:
                        valid_ref = conn.execute(
                            "SELECT 1 FROM users WHERE user_id = ?", (ref_id,)
                        ).fetchone()
                    if valid_ref:
                        referred_by = ref_id
                        with db() as conn:
                            conn.execute(
                                "UPDATE users SET referred_by = ? WHERE user_id = ?",
                                (referred_by, user_id),
                            )
            except ValueError:
                pass
        try:
            bot.send_message(ADMIN_CHAT_ID, f"🆕 کاربر جدید: {message.from_user.first_name or 'کاربر'} ({user_id})")
        except Exception:
            logger.exception("Could not notify admin about new user")

    send_main_menu(
        message.chat.id, message.from_user.first_name or "کاربر", user_id
    )


# ───────────────────────── پرداخت و سفارش ─────────────────────────

def create_order(user_id, amount, plan_name, paid_from_wallet=False):
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO orders
                (user_id, amount, plan_name, status, created_at, paid_from_wallet)
            VALUES (?, ?, ?, 'pending', ?, ?)
        """, (user_id, amount, plan_name, now_text(), int(paid_from_wallet)))
        return cur.lastrowid


def notify_admin_order(order_id, user_id, amount, plan_name):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(
        "📤 ارسال کانفیگ اختصاصی",
        callback_data=f"send_order_{order_id}",
    ))
    bot.send_message(
        ADMIN_CHAT_ID,
        f"💰 سفارش جدید\nشماره سفارش: {order_id}\nکاربر: {user_id}\n"
        f"پلن: {plan_name}\nمبلغ: {amount:,} تومان\n\n"
        "پس از بررسی پرداخت، کانفیگ را ارسال کنید.",
        reply_markup=markup,
    )


def show_shop(call):
    markup = InlineKeyboardMarkup(row_width=2)
    for price, plan in PLANS.items():
        markup.add(InlineKeyboardButton(
            plan["label"], callback_data=f"p_{price}"
        ))
    markup.add(InlineKeyboardButton("🎟️ وارد کردن کد تخفیف", callback_data="coupon"))
    markup.add(
        InlineKeyboardButton("🔙 بازگشت", callback_data="home"),
        InlineKeyboardButton("🏠 منوی اصلی", callback_data="home"),
    )
    safe_edit(call, "لطفاً پلن موردنظر خود را انتخاب کنید:", markup)


def begin_plan_purchase(call, user_id, price):
    plan = PLANS.get(price)
    if not plan:
        bot.answer_callback_query(call.id, "پلن معتبر نیست.", show_alert=True)
        return
    payable, coupon_code, percent = coupon_discount(user_id, price)
    with db() as conn:
        row = conn.execute("SELECT wallet FROM users WHERE user_id = ?", (user_id,)).fetchone()
    wallet = int(row["wallet"]) if row else 0
    if wallet >= payable:
        user_orders[user_id] = {"type": "wallet_purchase", "price": payable,
                               "original_price": price, "plan_name": plan["name"], "coupon_code": coupon_code}
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ تأیید خرید", callback_data="confirm_buy"),
                   InlineKeyboardButton("❌ انصراف", callback_data="shop"))
        discount_line = f"\n🎟️ تخفیف: {percent}٪" if coupon_code else ""
        safe_edit(call, f"🛒 سفارش: {plan['name']}\n💰 مبلغ قابل پرداخت: {payable:,} تومان{discount_line}\n"
                        f"💳 موجودی کیف پول: {wallet:,} تومان\n\nآیا خرید را تأیید می‌کنید؟", markup)
        return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("➕ شارژ کیف پول", callback_data="wallet"),
               InlineKeyboardButton("💳 کارت‌به‌کارت", callback_data=f"card_{price}"),
               InlineKeyboardButton("🔙 بازگشت", callback_data="shop"),
               InlineKeyboardButton("🏠 منوی اصلی", callback_data="home"))
    discount_line = f"\n🎟️ تخفیف اعمال‌شده: {percent}٪" if coupon_code else ""
    safe_edit(call, f"🛒 سفارش: {plan['name']}\n💰 مبلغ قابل پرداخت: {payable:,} تومان{discount_line}\n"
                    f"💳 موجودی کیف پول: {wallet:,} تومان\n❌ کسری موجودی: {payable-wallet:,} تومان\n\n"
                    "می‌توانید کیف پول را شارژ کنید یا کارت‌به‌کارت انجام دهید.", markup)


def start_card_payment(call, user_id, price, kind="plan"):
    if kind == "plan":
        plan = PLANS.get(price)
        if not plan:
            bot.answer_callback_query(call.id, "پلن معتبر نیست.", show_alert=True)
            return
        payable, coupon_code, percent = coupon_discount(user_id, price)
        user_orders[user_id] = {"type": "plan_card", "price": payable, "original_price": price,
                               "plan_name": plan["name"], "coupon_code": coupon_code}
        title = f"خرید پلن {plan['name']}"
    else:
        payable, coupon_code, percent = price, None, 0
        user_orders[user_id] = {"type": "wallet_charge", "price": price}
        title = "شارژ کیف پول"
    discount_line = f"\n🎟️ تخفیف: {percent}٪" if coupon_code else ""
    safe_edit(call, f"💳 کارت‌به‌کارت برای {title}\n\nشماره کارت: `{CARD_NUMBER}`\nبه نام: {CARD_HOLDER}\n\n"
                    f"مبلغ {payable:,} تومان را واریز کنید{discount_line} و سپس عکس فیش را ارسال کنید.",
              back_button("shop"), parse_mode="Markdown")


def confirm_wallet_purchase(call, user_id):
    order = user_orders.get(user_id)
    if not order or order.get("type") != "wallet_purchase":
        bot.answer_callback_query(call.id, "سفارش معتبری پیدا نشد.", show_alert=True)
        return

    price = int(order["price"])
    plan_name = order["plan_name"]
    exp = calculate_expiry(user_id, 30)
    with db() as conn:
        # تراکنش اتمیک مانع کسر دوبارهٔ موجودی در کلیک‌های هم‌زمان می‌شود.
        cur = conn.execute(
            "UPDATE users SET wallet = wallet - ? WHERE user_id = ? AND wallet >= ?",
            (price, user_id, price),
        )
        if cur.rowcount != 1:
            bot.answer_callback_query(call.id, "موجودی کافی نیست.", show_alert=True)
            return
        cur = conn.execute("""INSERT INTO orders
            (user_id, amount, plan_name, status, created_at, paid_from_wallet, coupon_code)
            VALUES (?, ?, ?, 'paid', ?, 1, ?)""",
            (user_id, price, plan_name, now_text(), order.get("coupon_code")))
        order_id = cur.lastrowid
        conn.execute("UPDATE users SET expire_date = ? WHERE user_id = ?", (exp, user_id))
        earned_points = price // 10000
        conn.execute("UPDATE users SET loyalty_points = loyalty_points + ? WHERE user_id = ?", (earned_points, user_id))

    commission, ref_id = reward_referrer(order_id, user_id, price)
    consume_coupon(order.get("coupon_code"))
    active_coupons.pop(user_id, None)
    audit_event(user_id, "wallet_purchase", f"order={order_id};amount={price}")
    user_orders.pop(user_id, None)
    bot.send_message(
        user_id,
        f"✅ پرداخت انجام شد و سفارش شمارهٔ {order_id} ثبت شد.\n"
        "پس از آماده‌شدن کانفیگ، آن را برایتان ارسال می‌کنیم.",
    )
    if ref_id and commission:
        try:
            bot.send_message(
                ref_id,
                f"🎉 مبلغ {commission:,} تومان پورسانت به کیف پول شما اضافه شد.",
            )
        except Exception:
            pass
    notify_admin_order(order_id, user_id, price, plan_name)


def create_payment_request(user_id, amount, kind, coupon_code=None):
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO payments (user_id, amount, kind, status, created_at, coupon_code)
            VALUES (?, ?, ?, 'pending', ?, ?)
        """, (user_id, amount, kind, now_text(), coupon_code))
        return cur.lastrowid


def send_payment_to_admin(message, amount, kind, plan_name=None, coupon_code=None):
    user_id = message.from_user.id
    payment_id = create_payment_request(user_id, amount, kind, coupon_code)
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ تأیید پرداخت", callback_data=f"payok_{payment_id}"),
        InlineKeyboardButton("❌ رد پرداخت", callback_data=f"payno_{payment_id}"),
    )
    caption = (
        f"🧾 پرداخت جدید\nشناسه پرداخت: {payment_id}\nکاربر: {user_id}\n"
        f"نوع: {'خرید پلن ' + plan_name if plan_name else 'شارژ کیف پول'}\n"
        f"مبلغ: {amount:,} تومان"
    )
    if message.photo:
        bot.send_photo(ADMIN_CHAT_ID, message.photo[-1].file_id,
                       caption=caption, reply_markup=markup)
    else:
        bot.send_message(ADMIN_CHAT_ID, caption + f"\nمتن ارسالی: {message.text or 'بدون متن'}",
                         reply_markup=markup)
    bot.send_message(user_id, "✅ رسید شما برای ادمین ارسال شد؛ پس از بررسی نتیجه اعلام می‌شود.")


# ───────────────────────── دکمه‌ها ─────────────────────────

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data or ""
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if data == "check_join":
        if check_membership(user_id):
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            ensure_user(call.from_user)
            send_main_menu(call.message.chat.id, call.from_user.first_name or "کاربر", user_id)
        else:
            bot.answer_callback_query(call.id, "❌ هنوز عضو کانال نشده‌اید.", show_alert=True)
        return

    if not check_membership(user_id):
        bot.answer_callback_query(call.id, "❌ ابتدا در کانال عضو شوید.", show_alert=True)
        return

    if data == "home":
        user_orders.pop(user_id, None)
        admin_states.pop(user_id, None)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        send_main_menu(call.message.chat.id, call.from_user.first_name or "کاربر", user_id)

    elif data == "shop":
        show_shop(call)

    elif data.startswith("p_"):
        try:
            begin_plan_purchase(call, user_id, int(data.split("_", 1)[1]))
        except ValueError:
            bot.answer_callback_query(call.id, "مبلغ نامعتبر است.", show_alert=True)

    elif data == "confirm_buy":
        confirm_wallet_purchase(call, user_id)

    elif data.startswith("card_"):
        try:
            start_card_payment(call, user_id, int(data.split("_", 1)[1]))
        except ValueError:
            bot.answer_callback_query(call.id, "مبلغ نامعتبر است.", show_alert=True)

    elif data == "free_test":
        if not TEST_CONFIG:
            bot.send_message(user_id, "⚠️ کانفیگ تست هنوز توسط مدیر تنظیم نشده است.")
            return
        with db() as conn:
            row = conn.execute(
                "SELECT test_used FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not row:
                bot.send_message(user_id, "ابتدا دستور /start را اجرا کنید.")
                return
            if row["test_used"]:
                bot.answer_callback_query(call.id, "❌ قبلاً از تست رایگان استفاده کرده‌اید.", show_alert=True)
                return
            exp = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                "UPDATE users SET test_used = 1, expire_date = ? WHERE user_id = ? AND test_used = 0",
                (exp, user_id),
            )
            if cur.rowcount != 1:
                bot.answer_callback_query(call.id, "❌ تست قبلاً استفاده شده است.", show_alert=True)
                return
        bot.send_message(
            user_id,
            f"🎉 تست ۲۴ ساعته فعال شد:\n\n`{TEST_CONFIG}`",
            parse_mode="Markdown",
        )

    elif data == "my_account":
        with db() as conn:
            row = conn.execute(
                "SELECT wallet, expire_date FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        wallet = row["wallet"] if row else 0
        exp = row["expire_date"] if row and row["expire_date"] else "فعال نیست"
        safe_edit(
            call,
            f"👤 اطلاعات حساب\n\n🆔 شناسه: `{user_id}`\n"
            f"💳 موجودی کیف پول: {wallet:,} تومان\n📅 تاریخ انقضا: {exp}",
            back_button(), parse_mode="Markdown",
        )

    elif data == "loyalty":
        with db() as conn:
            row = conn.execute("SELECT loyalty_points FROM users WHERE user_id=?", (user_id,)).fetchone()
        points = int(row["loyalty_points"] or 0) if row else 0
        tier = "🥉 برنزی"
        if points >= 1000:
            tier = "🥇 طلایی"
        elif points >= 300:
            tier = "🥈 نقره‌ای"
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(InlineKeyboardButton("🛒 خرید امتیازآور", callback_data="shop"), InlineKeyboardButton("🧾 تاریخچه سفارش‌ها", callback_data="order_history"))
        kb.add(InlineKeyboardButton("🔙 بازگشت", callback_data="home"), InlineKeyboardButton("🏠 منوی اصلی", callback_data="home"))
        safe_edit(call, f"⭐ باشگاه مشتریان HesVPN\n\nامتیاز شما: {points} ⭐\nسطح شما: {tier}\n\nهر ۱۰٬۰۰۰ تومان خرید موفق = ۱ امتیاز.\nامتیازها برای سطح‌بندی وفاداری ثبت می‌شوند و خودکار به پول تبدیل نمی‌شوند.", kb)

    elif data == "order_history":
        with db() as conn:
            rows = conn.execute("SELECT id,plan_name,amount,status,created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,)).fetchall()
        if not rows:
            safe_edit(call, "🧾 هنوز سفارشی برای شما ثبت نشده است.", back_button("home"))
        else:
            lines = [f"#{r['id']} · {r['plan_name']} · {r['amount']:,} تومان\nوضعیت: {r['status']} · {r['created_at']}" for r in rows]
            safe_edit(call, "🧾 تاریخچه ۱۰ سفارش اخیر\n\n" + "\n\n".join(lines), back_button("home"))

    elif data == "wallet":
        with db() as conn:
            row = conn.execute("SELECT wallet FROM users WHERE user_id = ?", (user_id,)).fetchone()
        balance = row["wallet"] if row else 0
        user_orders[user_id] = {"type": "awaiting_charge_amount"}
        safe_edit(
            call,
            f"💳 موجودی کیف پول: {balance:,} تومان\n\n"
            "مبلغ شارژ را به تومان و با عدد انگلیسی ارسال کنید:",
            back_button(),
        )

    elif data == "ref":
        with db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM users WHERE referred_by = ?", (user_id,)
            ).fetchone()
        bot_username = bot.get_me().username
        link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        safe_edit(
            call,
            f"🎁 دعوت از دوستان\n\nلینک اختصاصی:\n`{link}`\n"
            f"تعداد دعوت‌شده‌ها: {row['total']} نفر\n"
            "پورسانت: ۱۰٪ از خریدهای واجد شرایط.",
            back_button(), parse_mode="Markdown",
        )

    elif data == "guide":
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("📱 اندروید", callback_data="g_android"),
            InlineKeyboardButton("🍎 آیفون", callback_data="g_ios"),
            InlineKeyboardButton("💻 ویندوز", callback_data="g_win"),
            InlineKeyboardButton("🔙 بازگشت", callback_data="home"),
            InlineKeyboardButton("🏠 منوی اصلی", callback_data="home"),
        )
        safe_edit(call, "سیستم‌عامل خود را انتخاب کنید:", markup)

    elif data.startswith("g_"):
        device = data[2:]
        guides = {
            "android": "📱 اندروید:\n۱. برنامهٔ سازگار مانند v2rayNG را نصب کنید.\n۲. لینک کانفیگ را وارد یا در برنامه واردسازی کنید.",
            "ios": "🍎 آیفون:\n۱. برنامهٔ سازگار مانند V2Box را از منبع رسمی نصب کنید.\n۲. لینک کانفیگ را در برنامه وارد کنید.",
            "win": "💻 ویندوز:\n۱. برنامهٔ سازگار مانند v2rayN را از منبع رسمی بگیرید.\n۲. لینک کانفیگ را وارد برنامه کنید.",
        }
        safe_edit(call, guides.get(device, "راهنما پیدا نشد."), back_button("guide"))

    elif data == "support":
        user_orders[user_id] = {"type": "awaiting_support_msg"}
        safe_edit(call, "✉️ پیام یا مشکل خود را در یک پیام ارسال کنید:", back_button())

    elif data == "admin" and is_admin(user_id):
        with db() as conn:
            total = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
            pending = conn.execute("SELECT COUNT(*) AS total FROM payments WHERE status='pending'").fetchone()["total"]
            sales = conn.execute("SELECT COALESCE(SUM(amount),0) AS total FROM orders WHERE status IN ('paid','completed')").fetchone()["total"]
            active = conn.execute("SELECT COUNT(*) AS total FROM users WHERE expire_date > ?", (now_text(),)).fetchone()["total"]
            tickets = conn.execute("SELECT COUNT(*) AS total FROM tickets WHERE status='open'").fetchone()["total"]
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 پیام همگانی", callback_data="admin_bc"),
                   InlineKeyboardButton("🎟️ ساخت کد تخفیف", callback_data="admin_coupon"))
        markup.add(InlineKeyboardButton("🎫 تیکت‌های باز", callback_data="admin_tickets"),
                   InlineKeyboardButton("🧾 گزارش امنیتی", callback_data="admin_logs"))
        markup.add(InlineKeyboardButton("📦 سفارش‌های در انتظار", callback_data="admin_orders"),
                   InlineKeyboardButton("📈 گزارش فروش", callback_data="admin_sales"))
        markup.add(
            InlineKeyboardButton("🔙 بازگشت", callback_data="home"),
            InlineKeyboardButton("🏠 منوی اصلی", callback_data="home"),
        )
        safe_edit(call, f"⚙️ پنل مدیریت\n\n👥 کاربران: {total}\n🟢 اشتراک فعال: {active}\n💰 فروش ثبت‌شده: {sales:,} تومان\n🧾 پرداخت‌های در انتظار: {pending}\n🎫 تیکت باز: {tickets}", markup)

    elif data == "coupon":
        user_orders[user_id] = {"type": "awaiting_coupon_code"}
        safe_edit(call, "🎟️ کد تخفیف را ارسال کنید:", back_button("shop"))

    elif data == "admin_coupon" and is_admin(user_id):
        admin_states[user_id] = "awaiting_coupon_create"
        bot.send_message(user_id, "کد را با این قالب بفرستید:\nCODE PERCENT MAX_USES\nمثال: OFF20 20 100\nبرای استفاده نامحدود، MAX_USES را 0 بگذارید.")

    elif data == "admin_tickets" and is_admin(user_id):
        with db() as conn:
            rows = conn.execute("SELECT id,user_id,message,created_at FROM tickets WHERE status='open' ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            bot.send_message(user_id, "تیکت بازی وجود ندارد.", reply_markup=back_button("admin"))
        else:
            for ticket in rows:
                kb = InlineKeyboardMarkup().add(InlineKeyboardButton("↩️ پاسخ به تیکت", callback_data=f"ticket_reply_{ticket['id']}"))
                bot.send_message(user_id, f"🎫 تیکت #{ticket['id']} از کاربر {ticket['user_id']}\n{ticket['message'][:2500]}", reply_markup=kb)

    elif data.startswith("ticket_reply_") and is_admin(user_id):
        ticket_id = int(data.rsplit("_", 1)[1])
        with db() as conn:
            ticket = conn.execute("SELECT * FROM tickets WHERE id=? AND status='open'", (ticket_id,)).fetchone()
        if not ticket:
            bot.answer_callback_query(call.id, "تیکت باز پیدا نشد.", show_alert=True)
        else:
            admin_states[user_id] = f"reply_ticket_{ticket_id}"
            bot.send_message(user_id, f"پاسخ خود را برای تیکت #{ticket_id} بفرستید:")

    elif data == "admin_orders" and is_admin(user_id):
        with db() as conn:
            rows = conn.execute("SELECT id,user_id,plan_name,amount,status,created_at FROM orders WHERE status IN ('paid','pending') AND config_sent=0 ORDER BY id DESC LIMIT 15").fetchall()
        if not rows:
            bot.send_message(user_id, "✅ سفارشِ منتظر ارسال کانفیگ ندارید.", reply_markup=back_button("admin"))
        else:
            bot.send_message(user_id, "📦 سفارش‌های منتظر ارسال کانفیگ:")
            for r in rows:
                kb = InlineKeyboardMarkup().add(InlineKeyboardButton("📤 ارسال کانفیگ", callback_data=f"send_order_{r['id']}"))
                bot.send_message(user_id, f"سفارش #{r['id']}\nکاربر: {r['user_id']}\nپلن: {r['plan_name']}\nمبلغ: {r['amount']:,} تومان\nوضعیت: {r['status']}", reply_markup=kb)

    elif data == "admin_sales" and is_admin(user_id):
        with db() as conn:
            today = datetime.now().strftime("%Y-%m-%d")
            day_total = conn.execute("SELECT COALESCE(SUM(amount),0) n FROM orders WHERE status IN ('paid','completed') AND substr(created_at,1,10)=?", (today,)).fetchone()["n"]
            all_total = conn.execute("SELECT COALESCE(SUM(amount),0) n FROM orders WHERE status IN ('paid','completed')").fetchone()["n"]
            count = conn.execute("SELECT COUNT(*) n FROM orders WHERE status IN ('paid','completed')").fetchone()["n"]
        bot.send_message(user_id, f"📈 گزارش فروش\n\nامروز: {day_total:,} تومان\nکل فروش ثبت‌شده: {all_total:,} تومان\nتعداد سفارش‌های پرداخت‌شده: {count}", reply_markup=back_button("admin"))

    elif data == "admin_logs" and is_admin(user_id):
        with db() as conn:
            rows = conn.execute("SELECT actor_id,event,details,created_at FROM audit_log ORDER BY id DESC LIMIT 10").fetchall()
        body = "\n".join(f"{r['created_at']} | {r['actor_id']} | {r['event']} | {r['details']}" for r in rows) or "رویدادی ثبت نشده است."
        bot.send_message(user_id, "🧾 آخرین رویدادها:\n" + body[:3500], reply_markup=back_button("admin"))

    elif data == "admin_bc" and is_admin(user_id):
        admin_states[user_id] = "awaiting_broadcast"
        bot.send_message(ADMIN_CHAT_ID, "پیام همگانی را ارسال کنید:")

    elif data.startswith("payok_") and is_admin(user_id):
        payment_id = int(data.split("_", 1)[1])
        with db() as read_conn:
            payment_preview = read_conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        expiry_preview = calculate_expiry(payment_preview["user_id"], 30) if payment_preview and payment_preview["kind"] != "wallet" else None
        with db() as conn:
            payment = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if not payment or payment["status"] != "pending":
                bot.answer_callback_query(call.id, "این پرداخت قبلاً بررسی شده یا وجود ندارد.", show_alert=True)
                return
            cur_update = conn.execute(
                "UPDATE payments SET status='approved', reviewed_at=?, reviewed_by=? WHERE id=? AND status='pending'",
                (now_text(), user_id, payment_id),
            )
            if cur_update.rowcount != 1:
                bot.answer_callback_query(call.id, "این پرداخت هم‌اکنون بررسی شده است.", show_alert=True)
                return
            if payment["kind"] == "wallet":
                conn.execute(
                    "UPDATE users SET wallet = wallet + ? WHERE user_id = ?",
                    (payment["amount"], payment["user_id"]),
                )
                result_text = f"شارژ کیف پول به مبلغ {payment['amount']:,} تومان تأیید شد."
            else:
                plan_name = payment["kind"].removeprefix("plan:")
                cur = conn.execute("""INSERT INTO orders (user_id, amount, plan_name, status, created_at, coupon_code)
                    VALUES (?, ?, ?, 'paid', ?, ?)""",
                    (payment["user_id"], payment["amount"], plan_name, now_text(), payment["coupon_code"]))
                order_id = cur.lastrowid
                conn.execute("UPDATE users SET expire_date=? WHERE user_id=?", (expiry_preview, payment["user_id"]))
                earned_points = int(payment["amount"]) // 10000
                conn.execute("UPDATE users SET loyalty_points = loyalty_points + ? WHERE user_id=?", (earned_points, payment["user_id"]))
                result_text = f"پرداخت تأیید شد. سفارش شمارهٔ {order_id} آمادهٔ ارسال کانفیگ است."
        if payment["kind"] != "wallet":
            commission, referrer_id = reward_referrer(order_id, payment["user_id"], payment["amount"])
            if referrer_id and commission:
                try:
                    bot.send_message(referrer_id, f"🎉 مبلغ {commission:,} تومان پورسانت به کیف پول شما اضافه شد.")
                except Exception:
                    logger.exception("Could not notify referrer")
            consume_coupon(payment["coupon_code"])
            active_coupons.pop(payment["user_id"], None)
            audit_event(user_id, "payment_approved", f"payment={payment_id};user={payment['user_id']};amount={payment['amount']}")
        else:
            audit_event(user_id, "wallet_topup_approved", f"payment={payment_id};user={payment['user_id']};amount={payment['amount']}")
        bot.send_message(payment["user_id"], "✅ " + result_text)
        if payment["kind"] != "wallet":
            notify_admin_order(order_id, payment["user_id"], payment["amount"], plan_name)
        else:
            bot.send_message(payment["user_id"], "موجودی کیف پول شما به‌روزرسانی شد.")

    elif data.startswith("payno_") and is_admin(user_id):
        payment_id = int(data.split("_", 1)[1])
        with db() as conn:
            payment = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
            if not payment or payment["status"] != "pending":
                bot.answer_callback_query(call.id, "این پرداخت قبلاً بررسی شده یا وجود ندارد.", show_alert=True)
                return
            cur_update = conn.execute(
                "UPDATE payments SET status='rejected', reviewed_at=?, reviewed_by=? WHERE id=? AND status='pending'",
                (now_text(), user_id, payment_id),
            )
            if cur_update.rowcount != 1:
                bot.answer_callback_query(call.id, "این پرداخت هم‌اکنون بررسی شده است.", show_alert=True)
                return
        audit_event(user_id, "payment_rejected", f"payment={payment_id};user={payment['user_id']}")
        bot.send_message(payment["user_id"], "❌ پرداخت شما تأیید نشد. در صورت اشتباه با پشتیبانی تماس بگیرید.")
        bot.send_message(ADMIN_CHAT_ID, f"پرداخت {payment_id} رد شد.")

    elif data.startswith("send_order_") and is_admin(user_id):
        order_id = int(data.rsplit("_", 1)[1])
        with db() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["status"] not in ("paid", "pending"):
                bot.answer_callback_query(call.id, "سفارش پیدا نشد یا وضعیت آن معتبر نیست.", show_alert=True)
                return
        admin_states[user_id] = f"sending_order_{order_id}"
        bot.send_message(ADMIN_CHAT_ID, f"لینک کانفیگ اختصاصی سفارش {order_id} را ارسال کنید:")

    else:
        bot.answer_callback_query(call.id, "این گزینه شناخته نشد.", show_alert=False)


# ───────────────────────── پیام‌های متنی و فیش ─────────────────────────

@bot.message_handler(content_types=["text", "photo"])
def handle_text_messages(message):
    user_id = message.from_user.id
    text = (message.text or "").strip()

    if is_admin(user_id) and user_id in admin_states:
        state = admin_states[user_id]

        if state.startswith("sending_order_"):
            order_id = int(state.rsplit("_", 1)[1])
            config_text = text or DEFAULT_CONFIG
            if not config_text:
                bot.send_message(ADMIN_CHAT_ID, "لینک کانفیگ خالی است؛ دوباره ارسال کنید.")
                return
            with db() as conn:
                order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
                if not order:
                    bot.send_message(ADMIN_CHAT_ID, "سفارش پیدا نشد.")
                    admin_states.pop(user_id, None)
                    return
                if order["config_sent"]:
                    bot.send_message(ADMIN_CHAT_ID, "برای این سفارش قبلاً کانفیگ ارسال شده است.")
                    admin_states.pop(user_id, None)
                    return
                conn.execute("UPDATE orders SET config_sent=1, status='completed' WHERE id=?", (order_id,))
            try:
                bot.send_message(order["user_id"], f"🎉 کانفیگ سفارش شما:\n\n`{config_text}`", parse_mode="Markdown")
                bot.send_message(ADMIN_CHAT_ID, f"✅ کانفیگ سفارش {order_id} ارسال شد.")
            except Exception as exc:
                with db() as conn:
                    conn.execute("UPDATE orders SET config_sent=0, status='paid' WHERE id=?", (order_id,))
                bot.send_message(ADMIN_CHAT_ID, f"❌ ارسال ناموفق بود: {exc}")
            admin_states.pop(user_id, None)
            return

        if state == "awaiting_coupon_create":
            parts = text.split()
            if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                bot.send_message(user_id, "قالب نامعتبر است. نمونه: OFF20 20 100")
                return
            code, percent, max_uses = parts[0].upper(), int(parts[1]), int(parts[2])
            if not code.isalnum() or not 1 <= percent <= 90 or max_uses < 0:
                bot.send_message(user_id, "کد باید انگلیسی/عددی و تخفیف بین ۱ تا ۹۰ درصد باشد.")
                return
            try:
                with db() as conn:
                    conn.execute("INSERT INTO coupons(code,percent,max_uses,uses,active,created_at) VALUES(?,?,?,0,1,?)",
                                 (code, percent, max_uses, now_text()))
                audit_event(user_id, "coupon_created", f"code={code};percent={percent};max={max_uses}")
                bot.send_message(user_id, f"✅ کد {code} با تخفیف {percent}٪ ساخته شد.")
            except sqlite3.IntegrityError:
                bot.send_message(user_id, "این کد از قبل وجود دارد؛ یک کد دیگر انتخاب کنید.")
            admin_states.pop(user_id, None)
            return

        if state.startswith("reply_ticket_"):
            ticket_id = int(state.rsplit("_", 1)[1])
            with db() as conn:
                ticket = conn.execute("SELECT * FROM tickets WHERE id=? AND status='open'", (ticket_id,)).fetchone()
                if ticket:
                    conn.execute("UPDATE tickets SET status='answered',admin_reply=?,replied_at=? WHERE id=?",
                                 (text, now_text(), ticket_id))
            if not ticket:
                bot.send_message(user_id, "تیکت پیدا نشد یا قبلاً بسته شده است.")
            else:
                try:
                    bot.send_message(ticket['user_id'], f"📩 پاسخ پشتیبانی به تیکت #{ticket_id}:\n\n{text}")
                    bot.send_message(user_id, f"✅ پاسخ تیکت #{ticket_id} ارسال شد.")
                    audit_event(user_id, "ticket_replied", f"ticket={ticket_id};user={ticket['user_id']}")
                except Exception as exc:
                    bot.send_message(user_id, f"ارسال پاسخ ناموفق بود: {exc}")
            admin_states.pop(user_id, None)
            return

        if state == "awaiting_broadcast":
            if not text:
                bot.send_message(ADMIN_CHAT_ID, "لطفاً متن پیام همگانی را ارسال کنید.")
                return
            with db() as conn:
                user_ids = [row["user_id"] for row in conn.execute("SELECT user_id FROM users")]
            success = failed = 0
            for target_id in user_ids:
                try:
                    bot.send_message(target_id, text)
                    success += 1
                except Exception:
                    failed += 1
            bot.send_message(ADMIN_CHAT_ID, f"📢 پایان ارسال همگانی\n✅ موفق: {success}\n❌ ناموفق: {failed}")
            admin_states.pop(user_id, None)
            return

    order = user_orders.get(user_id)
    if not order:
        return

    order_type = order.get("type")

    if order_type == "awaiting_coupon_code":
        code = text.upper()
        with db() as conn:
            coupon = conn.execute("SELECT * FROM coupons WHERE code=? AND active=1", (code,)).fetchone()
        if not coupon or (coupon["max_uses"] and coupon["uses"] >= coupon["max_uses"]):
            bot.send_message(user_id, "❌ کد تخفیف نامعتبر یا تمام‌شده است. دوباره امتحان کنید یا به فروشگاه برگردید.", reply_markup=back_button("shop"))
            return
        active_coupons[user_id] = {"code": code}
        user_orders.pop(user_id, None)
        bot.send_message(user_id, f"✅ کد {code} فعال شد ({coupon['percent']}٪ تخفیف). حالا پلن موردنظر را انتخاب کنید.", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🛒 رفتن به فروشگاه", callback_data="shop"), InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")))
        audit_event(user_id, "coupon_applied", f"code={code}")
        return

    if order_type in ("plan_card", "wallet_charge"):
        amount = int(order["price"])
        kind = f"plan:{order['plan_name']}" if order_type == "plan_card" else "wallet"
        coupon_code = order.get("coupon_code") if order_type == "plan_card" else None
        user_orders.pop(user_id, None)
        send_payment_to_admin(message, amount, kind,
                              order.get("plan_name") if order_type == "plan_card" else None, coupon_code)
        return

    if order_type == "awaiting_charge_amount":
        if not text.isdigit() or int(text) <= 0:
            bot.send_message(user_id, "❌ مبلغ را فقط با عدد انگلیسی و بیشتر از صفر وارد کنید.")
            return
        amount = int(text)
        if amount > 100_000_000:
            bot.send_message(user_id, "❌ مبلغ واردشده بیش از حد مجاز است.")
            return
        user_orders[user_id] = {"type": "wallet_charge", "price": amount}
        bot.send_message(
            user_id,
            f"💳 مبلغ شارژ: {amount:,} تومان\n"
            f"شماره کارت: {CARD_NUMBER}\nبه نام: {CARD_HOLDER}\n\n"
            "پس از واریز، عکس فیش را ارسال کنید.",
        )
        return

    if order_type == "awaiting_support_msg":
        user_orders.pop(user_id, None)
        body = text or ("کاربر یک تصویر ارسال کرده است." if message.photo else "پیام خالی")
        with db() as conn:
            cur = conn.execute("INSERT INTO tickets(user_id,message,status,created_at) VALUES(?,?,'open',?)", (user_id, body, now_text()))
            ticket_id = cur.lastrowid
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("↩️ پاسخ به تیکت", callback_data=f"ticket_reply_{ticket_id}"))
        if message.photo:
            bot.send_photo(ADMIN_CHAT_ID, message.photo[-1].file_id, caption=f"🎫 تیکت #{ticket_id} از کاربر {user_id}\n{body}", reply_markup=kb)
        else:
            bot.send_message(ADMIN_CHAT_ID, f"🎫 تیکت #{ticket_id} از کاربر {user_id}\n\n{body}", reply_markup=kb)
        bot.send_message(user_id, f"✅ تیکت شما با شمارهٔ #{ticket_id} ثبت شد.")
        audit_event(user_id, "ticket_created", f"ticket={ticket_id}")
        return


# ───────────────────────── شروع ربات ─────────────────────────

init_db()
threading.Thread(target=expiry_notifier, daemon=True, name="expiry-notifier").start()
logger.info("Bot is running...")
try:
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
except Exception:
    logger.exception("Polling stopped. Check that no other instance uses this bot token (Telegram error 409).")
    raise
