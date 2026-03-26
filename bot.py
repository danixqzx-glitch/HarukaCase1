import logging
import sqlite3
import random
from datetime import datetime, timedelta

from telegram import (
Update, InlineKeyboardButton, InlineKeyboardMarkup,
LabeledPrice
)
from telegram.ext import (
Application, CommandHandler, CallbackQueryHandler,
ContextTypes, MessageHandler, filters,
PreCheckoutQueryHandler
)

# ------------------- НАСТРОЙКИ -------------------

TOKEN = "BOT_TOKEN"
PROVIDER_TOKEN = "PAYMENT_TOKEN"
ADMIN_IDS = [123456789]
DB_PATH = "bot.db"

GAME_COST = 5

# ------------------- ЛОГИ -------------------

logging.basicConfig(
level=logging.INFO,
format="%(asctime)s - %(levelname)s - %(message)s",
handlers=[
logging.FileHandler("bot.log"),
logging.StreamHandler()
]
)

# ------------------- БД -------------------

def connect_db():
return sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)

def init_db():
conn = connect_db()
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
user_id INTEGER PRIMARY KEY,
username TEXT,
referrer_id INTEGER,
created_at TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS balances (
user_id INTEGER PRIMARY KEY,
stars INTEGER DEFAULT 0,
tokens INTEGER DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS transactions (
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER,
amount INTEGER,
currency TEXT,
type TEXT,
description TEXT,
created_at TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS withdraw_requests (
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER,
amount INTEGER,
status TEXT,
created_at TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS user_cooldowns (
user_id INTEGER,
action TEXT,
last_used TEXT,
PRIMARY KEY(user_id, action)
)
""")

# Battle система
cur.execute("""
CREATE TABLE IF NOT EXISTS battles (
id INTEGER PRIMARY KEY AUTOINCREMENT,
user1 INTEGER,
user2 INTEGER,
votes1 INTEGER DEFAULT 0,
votes2 INTEGER DEFAULT 0,
status TEXT,
created_at TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS battle_votes (
battle_id INTEGER,
voter_id INTEGER,
voted_for INTEGER,
UNIQUE(battle_id, voter_id)
)
""")

# Индексы
cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_withdraw_status ON withdraw_requests(status)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_battles_status ON battles(status)")

conn.commit()
conn.close()

# ------------------- УТИЛИТЫ -------------------

def now():
return datetime.utcnow().isoformat()

def get_balance(user_id):
conn = connect_db()
cur = conn.cursor()
cur.execute("SELECT stars, tokens FROM balances WHERE user_id=?", (user_id,))
row = cur.fetchone()

if not row:
cur.execute("INSERT INTO balances (user_id) VALUES (?)", (user_id,))
conn.commit()
conn.close()
return 0, 0

conn.close()
return row

def update_balance(user_id, stars=0, tokens=0, t_type="system", desc=""):
conn = connect_db()
cur = conn.cursor()

cur.execute("SELECT stars, tokens FROM balances WHERE user_id=?", (user_id,))
row = cur.fetchone()

if not row:
cur.execute("INSERT INTO balances (user_id, stars, tokens) VALUES (?,0,0)", (user_id,))
current_stars, current_tokens = 0, 0
else:
current_stars, current_tokens = row

if current_stars + stars < 0 or current_tokens + tokens < 0:
conn.close()
return False

cur.execute("UPDATE balances SET stars = stars + ?, tokens = tokens + ? WHERE user_id=?",
(stars, tokens, user_id))

if stars != 0:
cur.execute("""
INSERT INTO transactions (user_id, amount, currency, type, description, created_at)
VALUES (?, ?, 'stars', ?, ?, ?)
""", (user_id, stars, t_type, desc, now()))

if tokens != 0:
cur.execute("""
INSERT INTO transactions (user_id, amount, currency, type, description, created_at)
VALUES (?, ?, 'tokens', ?, ?, ?)
""", (user_id, tokens, t_type, desc, now()))

conn.commit()
conn.close()
return True

# ------------------- COOLDOWN -------------------

def check_cooldown(user_id, action, seconds):
conn = connect_db()
cur = conn.cursor()

cur.execute("SELECT last_used FROM user_cooldowns WHERE user_id=? AND action=?",
(user_id, action))
row = cur.fetchone()

now_time = datetime.utcnow()

if row:
last = datetime.fromisoformat(row[0])
if now_time - last < timedelta(seconds=seconds):
conn.close()
return False

cur.execute("""
INSERT OR REPLACE INTO user_cooldowns (user_id, action, last_used)
VALUES (?, ?, ?)
""", (user_id, action, now_time.isoformat()))

conn.commit()
conn.close()
return True

# ------------------- РЕФЕРАЛКА -------------------

def register_user(user_id, username, referrer_id=None):
conn = connect_db()
cur = conn.cursor()

cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
if cur.fetchone():
conn.close()
return

cur.execute("INSERT INTO users VALUES (?, ?, ?, ?)",
(user_id, username, referrer_id, now()))

if referrer_id and referrer_id != user_id:
update_balance(referrer_id, tokens=5, t_type="referral", desc="Реферал")

conn.commit()
conn.close()

# ------------------- DAILY -------------------

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
uid = update.effective_user.id

if not check_cooldown(uid, "daily", 86400):
await update.message.reply_text("⏳ Уже получали бонус сегодня")
return

stars = random.randint(1, 5)
tokens = random.randint(1, 3)

update_balance(uid, stars=stars, tokens=tokens, t_type="daily")

await update.message.reply_text(f"🎁 Бонус: ⭐{stars} | 🪙{tokens}")

# ------------------- КАЗИНО -------------------

async def casino(update: Update, context: ContextTypes.DEFAULT_TYPE):
uid = update.effective_user.id

if not check_cooldown(uid, "casino", 10):
await update.message.reply_text("⏳ Подождите 10 сек")
return

stars, _ = get_balance(uid)

if stars < GAME_COST:
await update.message.reply_text("❌ Недостаточно звёзд")
return

update_balance(uid, stars=-GAME_COST, t_type="casino")

win = random.choice([0, 0, 0, 5, 10])

if win:
update_balance(uid, stars=win, t_type="casino_win")
await update.message.reply_text(f"🎰 Вы выиграли {win}⭐")
else:
await update.message.reply_text("🎰 Вы проиграли")

# ------------------- BATTLE -------------------

async def create_battle(update: Update, context: ContextTypes.DEFAULT_TYPE):
uid = update.effective_user.id

conn = connect_db()
cur = conn.cursor()

cur.execute("SELECT user_id FROM users ORDER BY RANDOM() LIMIT 1")
opponent = cur.fetchone()

if not opponent:
await update.message.reply_text("Нет соперников")
return

opponent_id = opponent[0]

cur.execute("""
INSERT INTO battles (user1, user2, status, created_at)
VALUES (?, ?, 'active', ?)
""", (uid, opponent_id, now()))

battle_id = cur.lastrowid
conn.commit()
conn.close()

kb = InlineKeyboardMarkup([
[InlineKeyboardButton("Голос за 1", callback_data=f"vote_{battle_id}_1")],
[InlineKeyboardButton("Голос за 2", callback_data=f"vote_{battle_id}_2")]
])

await update.message.reply_text("⚔️ Битва началась!", reply_markup=kb)

async def vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
uid = query.from_user.id
_, battle_id, vote_for = query.data.split("_")

conn = connect_db()
cur = conn.cursor()

try:
cur.execute("""
INSERT INTO battle_votes (battle_id, voter_id, voted_for)
VALUES (?, ?, ?)
""", (battle_id, uid, vote_for))
except:
await query.answer("Вы уже голосовали")
conn.close()
return

if vote_for == "1":
cur.execute("UPDATE battles SET votes1 = votes1 + 1 WHERE id=?", (battle_id,))
else:
cur.execute("UPDATE battles SET votes2 = votes2 + 1 WHERE id=?", (battle_id,))

conn.commit()
conn.close()

await query.answer("Голос засчитан!")

# ------------------- WITHDRAW -------------------

def create_withdraw(user_id, amount):
conn = connect_db()
cur = conn.cursor()

cur.execute("SELECT tokens FROM balances WHERE user_id=?", (user_id,))
row = cur.fetchone()

if not row or row[0] < amount:
conn.close()
return False

cur.execute("UPDATE balances SET tokens = tokens - ? WHERE user_id=?", (amount, user_id))

cur.execute("""
INSERT INTO withdraw_requests (user_id, amount, status, created_at)
VALUES (?, ?, 'pending', ?)
""", (user_id, amount, now()))

conn.commit()
conn.close()
return True

# ------------------- МЕНЮ -------------------

def main_menu():
return InlineKeyboardMarkup([
[InlineKeyboardButton("🎰 Казино", callback_data="casino")],
[InlineKeyboardButton("⚔️ Битва", callback_data="battle")],
[InlineKeyboardButton("🎁 Бонус", callback_data="daily")],
[InlineKeyboardButton("💰 Баланс", callback_data="balance")],
[InlineKeyboardButton("👥 Рефералка", callback_data="ref")],
[InlineKeyboardButton("💸 Вывод", callback_data="withdraw")]
])

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text("Меню:", reply_markup=main_menu())

# ------------------- СТАРТ -------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
uid = update.effective_user.id
username = update.effective_user.username

ref = int(context.args[0]) if context.args else None
register_user(uid, username, ref)

await update.message.reply_text("Добро пожаловать!", reply_markup=main_menu())

# ------------------- ОБРАБОТКА КНОПОК -------------------

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
data = query.data
uid = query.from_user.id

if data == "balance":
stars, tokens = get_balance(uid)
await query.message.edit_text(f"⭐ {stars}\n🪙 {tokens}", reply_markup=main_menu())

elif data == "casino":
await casino(update, context)

elif data == "daily":
await daily(update, context)

elif data == "battle":
await create_battle(update, context)

elif data.startswith("vote"):
await vote(update, context)

# ------------------- АДМИН -------------------

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
if update.effective_user.id not in ADMIN_IDS:
return

conn = connect_db()
cur = conn.cursor()
cur.execute("SELECT id, user_id, amount FROM withdraw_requests WHERE status='pending'")
rows = cur.fetchall()
conn.close()

text = "Заявки:\n"
for r in rows:
text += f"ID {r[0]} | User {r[1]} | {r[2]}\n"

await update.message.reply_text(text)

# ------------------- ПЛАТЕЖИ -------------------

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
prices = [LabeledPrice("Votes 25", 100 * 100)]
await context.bot.send_invoice(
chat_id=update.effective_chat.id,
title="Покупка голосов",
description="25 голосов",
payload="votes_25",
provider_token=PROVIDER_TOKEN,
currency="RUB",
prices=prices,
start_parameter="buy"
)

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text("Платёж прошёл!")

# ------------------- ЗАПУСК -------------------

def main():
init_db()

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("menu", menu))
app.add_handler(CommandHandler("daily", daily))
app.add_handler(CommandHandler("casino", casino))
app.add_handler(CommandHandler("battle", create_battle))
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CommandHandler("buy", buy))

app.add_handler(CallbackQueryHandler(buttons))
app.add_handler(PreCheckoutQueryHandler(precheckout))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

print("Bot started")
app.run_polling()

if __name__ == "__main__":
main()