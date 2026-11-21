import asyncio
import os
import uuid
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from lavepay import LavePay
from lavepay.integrations.fastapi import install as install_lavepay
from fastapi import FastAPI
import uvicorn

# Railway Variables:
# BOT_TOKEN, LAVEPAY_API_KEY, LAVEPAY_WEBHOOK_SECRET, DATABASE_URL, ADMIN_ID
BOT_TOKEN = os.environ["BOT_TOKEN"]
LAVEPAY_API_KEY = os.environ["LAVEPAY_API_KEY"]
LAVEPAY_WEBHOOK_SECRET = os.environ["LAVEPAY_WEBHOOK_SECRET"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
lp = LavePay(LAVEPAY_API_KEY, webhook_secret=LAVEPAY_WEBHOOK_SECRET)
app = FastAPI(title="Duel Games LavePay webhook")

# ---------- Database ----------
@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            balance NUMERIC(18,2) NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            games INTEGER NOT NULL DEFAULT 0,
            lang TEXT NOT NULL DEFAULT 'kz',
            is_vip BOOLEAN NOT NULL DEFAULT FALSE,
            is_anonymous BOOLEAN NOT NULL DEFAULT FALSE,
            referrer_id BIGINT,
            has_deposited BOOLEAN NOT NULL DEFAULT FALSE
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            order_id TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            amount NUMERIC(18,2) NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            paid_at TIMESTAMP
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            amount NUMERIC(18,2) NOT NULL,
            address TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS lobbies (
            lobby_id TEXT PRIMARY KEY,
            host_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            game_type TEXT NOT NULL,
            bet_amount NUMERIC(18,2) NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting'
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS lobby_players (
            lobby_id TEXT REFERENCES lobbies(lobby_id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            PRIMARY KEY(lobby_id,user_id)
        )""")

def ensure_user(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO users(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))

def get_user(uid):
    ensure_user(uid)
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM users WHERE user_id=%s", (uid,))
        return cur.fetchone()

def change_balance(uid, amount):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, uid))

def get_lang(uid):
    return get_user(uid)["lang"] or "kz"

def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))

# ---------- Text / keyboards ----------
TEXT = {
 "kz": {
  "welcome":"Сәлем! <b>Duel Games</b>!\n\n💵 Балансыңыз: <b>${:.2f}</b>\n\nНе істейміз?",
  "play":"Ойын ойнау","profile":"Профиль","balance":"Баланс","deposit":"Пополнить","withdraw":"Вывод","lang":"Тіл",
  "choose":"Ойынды таңдаңыз:","dice":"Кости дуэль","darts":"Дартс дуэль","bet":"Ставканы енгізіңіз (USD):",
  "no_money":"❌ Балансыңыз жеткіліксіз!","deposit_amount":"Толықтырғыңыз келетін соманы USD түрінде енгізіңіз:",
  "pay":"💳 Төлеу","created":"✅ Төлем шоты жасалды: ${}\nТөлеу үшін батырманы басыңыз.",
  "paid":"✅ Төлем қабылданды! Жаңа баланс: ${:.2f}",
  "withdraw_amount":"Шығарылатын соманы енгізіңіз (ең азы $1):",
  "withdraw_address":"USDT TRC20 әмиян мекенжайын енгізіңіз:",
  "withdraw_sent":"Өтінім қабылданды. Әкімші тексергеннен кейін өңдейді.",
  "profile_text":"👤 <b>Профиль</b>\nID: <code>{}</code>\nЖеңістер: {}\nЖеңілістер: {}\nОйындар: {}\nVIP: {}\nАноним: {}",
  "balance_text":"💰 Баланс: ${:.2f}",
  "language":"Тілді таңдаңыз:",
  "back":"Артқа","bad_amount":"❌ Соманы дұрыс енгізіңіз.","admin_only":"Бұл команда тек әкімшіге арналған.",
 },
 "ru": {
  "welcome":"Привет! <b>Duel Games</b>!\n\n💵 Баланс: <b>${:.2f}</b>\n\nЧто делаем?",
  "play":"Играть","profile":"Профиль","balance":"Баланс","deposit":"Пополнить","withdraw":"Вывод","lang":"Язык",
  "choose":"Выберите игру:","dice":"Дуэль на костях","darts":"Дуэль в дартс","bet":"Введите ставку (USD):",
  "no_money":"❌ Недостаточно средств!","deposit_amount":"Введите сумму пополнения в USD:",
  "pay":"💳 Оплатить","created":"✅ Счёт создан на ${}. Нажмите кнопку оплаты.",
  "paid":"✅ Платёж получен! Новый баланс: ${:.2f}",
  "withdraw_amount":"Введите сумму вывода (минимум $1):",
  "withdraw_address":"Введите адрес USDT TRC20:",
  "withdraw_sent":"Заявка принята и будет обработана администратором.",
  "profile_text":"👤 <b>Профиль</b>\nID: <code>{}</code>\nПобеды: {}\nПоражения: {}\nИгры: {}\nVIP: {}\nАноним: {}",
  "balance_text":"💰 Баланс: ${:.2f}",
  "language":"Выберите язык:",
  "back":"Назад","bad_amount":"❌ Введите корректную сумму.","admin_only":"Только для администратора.",
 },
 "en": {
  "welcome":"Hi! <b>Duel Games</b>!\n\n💵 Balance: <b>${:.2f}</b>\n\nWhat do we do?",
  "play":"Play","profile":"Profile","balance":"Balance","deposit":"Deposit","withdraw":"Withdraw","lang":"Language",
  "choose":"Choose a game:","dice":"Dice duel","darts":"Darts duel","bet":"Enter bet (USD):",
  "no_money":"❌ Insufficient balance!","deposit_amount":"Enter deposit amount in USD:",
  "pay":"💳 Pay","created":"✅ Invoice created for ${}. Tap to pay.",
  "paid":"✅ Payment received! New balance: ${:.2f}",
  "withdraw_amount":"Enter withdrawal amount (minimum $1):",
  "withdraw_address":"Enter your USDT TRC20 wallet address:",
  "withdraw_sent":"Request received. An administrator will process it.",
  "profile_text":"👤 <b>Profile</b>\nID: <code>{}</code>\nWins: {}\nLosses: {}\nGames: {}\nVIP: {}\nAnonymous: {}",
  "balance_text":"💰 Balance: ${:.2f}",
  "language":"Choose language:",
  "back":"Back","bad_amount":"❌ Enter a valid amount.","admin_only":"Admin only.",
 }
}

class AmountState(StatesGroup):
    deposit = State()
    withdraw = State()
    address = State()
    bet = State()

def main_kb(lang):
    t = TEXT[lang]
    kb = InlineKeyboardBuilder()
    for label, data in [(t["play"],"play"),(t["profile"],"profile"),(t["balance"],"balance"),
                        (t["deposit"],"deposit"),(t["withdraw"],"withdraw"),(t["lang"],"language")]:
        kb.button(text=label, callback_data=data)
    kb.adjust(2,2,2)
    return kb.as_markup()

def back_kb(lang, data="home"):
    kb=InlineKeyboardBuilder()
    kb.button(text=TEXT[lang]["back"], callback_data=data)
    return kb.as_markup()

async def show_home(target, uid):
    u=get_user(uid); lang=u["lang"]
    await target.answer(TEXT[lang]["welcome"].format(float(u["balance"])),
                        reply_markup=main_kb(lang), parse_mode="HTML")

@dp.message(CommandStart())
async def start(message: Message):
    u=get_user(message.from_user.id)
    args=message.text.split()
    if len(args)>1 and args[1].startswith("ref_"):
        try:
            ref=int(args[1][4:])
            if ref != message.from_user.id:
                with db() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE users SET referrer_id=%s WHERE user_id=%s AND referrer_id IS NULL",
                                (ref,message.from_user.id))
        except ValueError:
            pass
    await message.answer(TEXT[u["lang"]]["welcome"].format(float(u["balance"])),
                         reply_markup=main_kb(u["lang"]), parse_mode="HTML")

@dp.callback_query(F.data=="home")
async def home(c: CallbackQuery):
    u=get_user(c.from_user.id); t=TEXT[u["lang"]]
    await c.message.edit_text(t["welcome"].format(float(u["balance"])),
                              reply_markup=main_kb(u["lang"]),parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data=="play")
async def play(c: CallbackQuery):
    t=TEXT[get_lang(c.from_user.id)]
    kb=InlineKeyboardBuilder()
    kb.button(text=t["dice"],callback_data="game_dice")
    kb.button(text=t["darts"],callback_data="game_darts")
    kb.button(text=t["back"],callback_data="home")
    kb.adjust(1)
    await c.message.edit_text(t["choose"],reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.in_({"game_dice","game_darts"}))
async def choose_game(c: CallbackQuery,state: FSMContext):
    lang=get_lang(c.from_user.id)
    await state.update_data(game=c.data.removeprefix("game_"))
    await state.set_state(AmountState.bet)
    await c.message.edit_text(TEXT[lang]["bet"])
    await c.answer()

@dp.message(AmountState.bet)
async def create_lobby(message: Message,state: FSMContext):
    lang=get_lang(message.from_user.id)
    try:
        amount=money(message.text.replace(",","."))
        if amount<=0: raise ValueError
    except Exception:
        await message.answer(TEXT[lang]["bad_amount"]); return
    u=get_user(message.from_user.id)
    if Decimal(u["balance"])<amount:
        await message.answer(TEXT[lang]["no_money"]); await state.clear(); return
    data=await state.get_data()
    lobby_id=uuid.uuid4().hex[:6].upper()
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO lobbies(lobby_id,host_id,chat_id,game_type,bet_amount) VALUES(%s,%s,%s,%s,%s)",
                    (lobby_id,message.from_user.id,message.chat.id,data["game"],amount))
        cur.execute("INSERT INTO lobby_players(lobby_id,user_id) VALUES(%s,%s)",(lobby_id,message.from_user.id))
    me=await bot.get_me()
    await message.answer(f"Лобби: <code>{lobby_id}</code>\nСтавка: ${amount}\n"
                         f"Досыңды шақыр: https://t.me/{me.username}?start=lobby_{lobby_id}",
                         parse_mode="HTML")
    await state.clear()

@dp.message(Command("join"))
async def join_by_code(message: Message):
    parts=message.text.split()
    if len(parts)!=2:
        await message.answer("Қолдану: /join LOBBY_ID"); return
    lobby_id=parts[1].upper()
    u=get_user(message.from_user.id)
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM lobbies WHERE lobby_id=%s AND status='waiting'",(lobby_id,))
        lobby=cur.fetchone()
        if not lobby:
            await message.answer("Лобби табылмады."); return
        cur.execute("SELECT user_id FROM lobby_players WHERE lobby_id=%s",(lobby_id,))
        players=[r[0] for r in cur.fetchall()]
        bet=Decimal(lobby["bet_amount"])
        if message.from_user.id in players or len(players)>=2:
            await message.answer("Лобби толы немесе сіз кіріп қойғансыз."); return
        if Decimal(u["balance"])<bet:
            await message.answer(TEXT[u["lang"]]["no_money"]); return
        cur.execute("INSERT INTO lobby_players(lobby_id,user_id) VALUES(%s,%s)",(lobby_id,message.from_user.id))
        players.append(message.from_user.id)
        cur.execute("UPDATE lobbies SET status='playing' WHERE lobby_id=%s",(lobby_id,))
    await message.answer("Ойын басталды!")
    await run_duel(lobby_id,players,lobby["game_type"],bet,lobby["chat_id"])

async def run_duel(lobby_id,players,game,bet,chat_id):
    p1,p2=players
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance-%s WHERE user_id IN (%s,%s) AND balance >= %s",
                    (bet,p1,p2,bet))
        if cur.rowcount!=2:
            cur.execute("UPDATE lobbies SET status='waiting' WHERE lobby_id=%s",(lobby_id,))
            await bot.send_message(chat_id,"Ойыншылардың балансын тексеру сәтсіз болды.")
            return
    emoji="🎲" if game=="dice" else "🎯"
    a=await bot.send_dice(chat_id,emoji=emoji); await asyncio.sleep(4)
    b=await bot.send_dice(chat_id,emoji=emoji); await asyncio.sleep(4)
    with db() as conn, conn.cursor() as cur:
        if a.dice.value==b.dice.value:
            cur.execute("UPDATE users SET balance=balance+%s WHERE user_id IN (%s,%s)",(bet,p1,p2))
            result="Тең ойын! Ставкалар қайтарылды."
        else:
            winner,loser=(p1,p2) if a.dice.value>b.dice.value else (p2,p1)
            prize=(bet*2*Decimal("0.95")).quantize(Decimal("0.01"))
            cur.execute("UPDATE users SET balance=balance+%s,wins=wins+1,games=games+1 WHERE user_id=%s",(prize,winner))
            cur.execute("UPDATE users SET losses=losses+1,games=games+1 WHERE user_id=%s",(loser,))
            result=f"🏆 Жеңімпаз: {winner}. Ұтыс: ${prize}"
        cur.execute("DELETE FROM lobbies WHERE lobby_id=%s",(lobby_id,))
    await bot.send_message(chat_id,result)

@dp.callback_query(F.data=="profile")
async def profile(c: CallbackQuery):
    u=get_user(c.from_user.id); t=TEXT[u["lang"]]
    await c.message.edit_text(t["profile_text"].format(u["user_id"],u["wins"],u["losses"],u["games"],
        "✅" if u["is_vip"] else "❌","✅" if u["is_anonymous"] else "❌"),
        reply_markup=back_kb(u["lang"]),parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data=="balance")
async def balance(c: CallbackQuery):
    u=get_user(c.from_user.id); t=TEXT[u["lang"]]
    await c.message.edit_text(t["balance_text"].format(float(u["balance"])),
                              reply_markup=back_kb(u["lang"]))
    await c.answer()

@dp.callback_query(F.data=="language")
async def language(c: CallbackQuery):
    kb=InlineKeyboardBuilder()
    for code,name in [("kz","🇰🇿 Қазақша"),("ru","🇷🇺 Русский"),("en","🇬🇧 English")]:
        kb.button(text=name,callback_data=f"setlang_{code}")
    kb.adjust(1)
    await c.message.edit_text("Тілді таңдаңыз / Выберите язык:",reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("setlang_"))
async def set_language(c: CallbackQuery):
    lang=c.data.split("_",1)[1]
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET lang=%s WHERE user_id=%s",(lang,c.from_user.id))
    u=get_user(c.from_user.id)
    await c.message.edit_text(TEXT[lang]["welcome"].format(float(u["balance"])),
                              reply_markup=main_kb(lang),parse_mode="HTML")
    await c.answer()

# ---------- LavePay deposit ----------
@dp.callback_query(F.data=="deposit")
async def deposit_start(c: CallbackQuery,state: FSMContext):
    lang=get_lang(c.from_user.id)
    await state.set_state(AmountState.deposit)
    await c.message.edit_text(TEXT[lang]["deposit_amount"])
    await c.answer()

@dp.message(AmountState.deposit)
async def make_invoice(message: Message,state: FSMContext):
    lang=get_lang(message.from_user.id)
    try:
        amount=money(message.text.replace(",","."))
        if amount<Decimal("1.00"): raise ValueError
    except Exception:
        await message.answer(TEXT[lang]["bad_amount"]); return
    order_id=f"dg_{message.from_user.id}_{uuid.uuid4().hex}"
    # LavePay SDK requires str/Decimal, not float.
    invoice=await lp.invoices.create(
        str(amount), order_id=order_id,
        description=f"Duel Games deposit user {message.from_user.id}"
    )
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO payments(order_id,user_id,amount) VALUES(%s,%s,%s)",
                    (order_id,message.from_user.id,amount))
    kb=InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=TEXT[lang]["pay"],url=invoice.pay_url))
    await message.answer(TEXT[lang]["created"].format(amount),reply_markup=kb.as_markup())
    await state.clear()

# The FastAPI integration verifies the HMAC signature and exposes POST /lavepay.
@lp.invoice_paid()
async def on_invoice_paid(invoice):
    order_id=str(invoice.order_id)
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM payments WHERE order_id=%s FOR UPDATE",(order_id,))
        payment=cur.fetchone()
        if not payment or payment["status"]=="paid":
            return
        # Validate the paid invoice against our own order and expected amount.
        if not invoice.matches(order_id=order_id, amount=str(payment["amount"])):
            return
        cur.execute("UPDATE payments SET status='paid',paid_at=NOW() WHERE order_id=%s AND status='pending'",
                    (order_id,))
        if cur.rowcount != 1:
            return
        cur.execute("UPDATE users SET balance=balance+%s,has_deposited=TRUE WHERE user_id=%s",
                    (payment["amount"],payment["user_id"]))
        cur.execute("SELECT balance,lang FROM users WHERE user_id=%s",(payment["user_id"],))
        user=cur.fetchone()
    try:
        await bot.send_message(payment["user_id"],
            TEXT[user["lang"]]["paid"].format(float(user["balance"])))
    except Exception as exc:
        print("Telegram notify error:",repr(exc))

# ---------- Withdrawals: admin-reviewed requests ----------
@dp.callback_query(F.data=="withdraw")
async def withdraw_start(c: CallbackQuery,state: FSMContext):
    lang=get_lang(c.from_user.id)
    await state.set_state(AmountState.withdraw)
    await c.message.edit_text(TEXT[lang]["withdraw_amount"])
    await c.answer()

@dp.message(AmountState.withdraw)
async def withdraw_amount(message: Message,state: FSMContext):
    lang=get_lang(message.from_user.id)
    try:
        amount=money(message.text.replace(",","."))
        if amount<1: raise ValueError
    except Exception:
        await message.answer(TEXT[lang]["bad_amount"]); return
    u=get_user(message.from_user.id)
    if Decimal(u["balance"])<amount:
        await message.answer(TEXT[lang]["no_money"]); await state.clear(); return
    await state.update_data(withdraw_amount=str(amount))
    await state.set_state(AmountState.address)
    await message.answer(TEXT[lang]["withdraw_address"])

@dp.message(AmountState.address)
async def withdraw_address(message: Message,state: FSMContext):
    address=message.text.strip()
    if len(address)<25 or len(address)>80:
        await message.answer("Әмиян мекенжайы қате сияқты. Қайталап енгізіңіз."); return
    data=await state.get_data()
    amount=Decimal(data["withdraw_amount"])
    uid=message.from_user.id
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
                    (amount,uid,amount))
        if cur.rowcount!=1:
            await message.answer(TEXT[get_lang(uid)]["no_money"]); await state.clear(); return
        cur.execute("INSERT INTO withdrawals(user_id,amount,address) VALUES(%s,%s,%s) RETURNING id",
                    (uid,amount,address))
        wid=cur.fetchone()[0]
    await message.answer(TEXT[get_lang(uid)]["withdraw_sent"])
    if ADMIN_ID:
        await bot.send_message(ADMIN_ID,
            f"Жаңа шығару өтінімі #{wid}\nUser: {uid}\nAmount: ${amount}\nAddress: {address}\n"
            f"Растау/қайтару үшін дерекқордағы withdrawals кестесін тексеріңіз.")
    await state.clear()

# ---------- Admin ----------
@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if message.from_user.id!=ADMIN_ID:
        await message.answer(TEXT[get_lang(message.from_user.id)]["admin_only"]); return
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*),COALESCE(SUM(balance),0) FROM users")
        users,total=cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")
        pending=cur.fetchone()[0]
    await message.answer(f"Admin panel\nUsers: {users}\nUser balances total: ${total}\nPending withdrawals: {pending}")

@app.get("/")
async def health():
    return {"status":"ok","service":"Duel Games","webhook":"/lavepay"}

install_lavepay(app, lp)  # LavePay webhook endpoint: POST /lavepay

@app.on_event("startup")
async def startup():
    init_db()
    app.state.bot_task=asyncio.create_task(dp.start_polling(bot))
    print("Duel Games started; LavePay webhook: /lavepay")

@app.on_event("shutdown")
async def shutdown():
    task=getattr(app.state,"bot_task",None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await bot.session.close()

if __name__=="__main__":
    uvicorn.run(app,host="0.0.0.0",port=PORT)
