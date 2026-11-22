import asyncio, os, uuid, re, json
from decimal import Decimal
from contextlib import contextmanager
import httpx, psycopg2, psycopg2.extras
from psycopg2 import pool
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from lavepay import LavePay
from lavepay.integrations.fastapi import install as install_lavepay
from fastapi import FastAPI, Request
import uvicorn

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LAVEPAY_API_KEY = os.environ.get("LAVEPAY_API_KEY", "")
LAVEPAY_WEBHOOK_SECRET = os.environ.get("LAVEPAY_WEBHOOK_SECRET", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))
LAVEPAY_BASE = "https://laveshka.io/merchant/v1"

if DATABASE_URL.startswith("postgres://"): DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
lp = LavePay(LAVEPAY_API_KEY, webhook_secret=LAVEPAY_WEBHOOK_SECRET)
app = FastAPI(title="Duel Games")

_pool = None
def get_pool():
    global _pool
    if _pool is None: _pool = pool.ThreadedConnectionPool(1, 20, DATABASE_URL, sslmode="require")
    return _pool

@contextmanager
def db():
    p = get_pool()
    conn = p.getconn()
    try:
        with conn: yield conn
    finally: p.putconn(conn)

def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, balance NUMERIC(18,2) DEFAULT 0, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, games INTEGER DEFAULT 0, lang TEXT DEFAULT 'kz', is_vip BOOLEAN DEFAULT FALSE, is_anonymous BOOLEAN DEFAULT FALSE, referrer_id BIGINT, has_deposited BOOLEAN DEFAULT FALSE)")
        cur.execute("CREATE TABLE IF NOT EXISTS payments (order_id TEXT PRIMARY KEY, user_id BIGINT, amount NUMERIC(18,2), status TEXT DEFAULT 'pending', paid_at TIMESTAMP)")
        cur.execute("CREATE TABLE IF NOT EXISTS withdrawals (id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount NUMERIC(18,2), address TEXT, network TEXT, status TEXT DEFAULT 'pending', tx_id TEXT)")
        cur.execute("CREATE TABLE IF NOT EXISTS lobbies (lobby_id TEXT PRIMARY KEY, host_id BIGINT, chat_id BIGINT, game_type TEXT, bet_amount NUMERIC(18,2), status TEXT DEFAULT 'waiting')")
        cur.execute("CREATE TABLE IF NOT EXISTS lobby_players (lobby_id TEXT REFERENCES lobbies(lobby_id) ON DELETE CASCADE, user_id BIGINT, PRIMARY KEY(lobby_id, user_id))")
        cur.execute("CREATE TABLE IF NOT EXISTS multi_checks (check_id TEXT PRIMARY KEY, amount NUMERIC(18,2), max_claims INTEGER, claimed INTEGER DEFAULT 0, creator_id BIGINT, status TEXT DEFAULT 'active')")
        cur.execute("CREATE TABLE IF NOT EXISTS check_claims (check_id TEXT REFERENCES multi_checks(check_id) ON DELETE CASCADE, user_id BIGINT, PRIMARY KEY(check_id, user_id))")

def get_user(uid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("INSERT INTO users(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
        cur.execute("SELECT * FROM users WHERE user_id=%s", (uid,))
        return cur.fetchone()
def get_lang(uid): return get_user(uid)["lang"] if get_user(uid) else "kz"
def money(v): return Decimal(str(v)).quantize(Decimal("0.01"))
def is_admin(uid): return ADMIN_ID != 0 and uid == ADMIN_ID

EMOJI_IDS = {"wallet": "5420271804411292547", "balance": "5992085874546844207", "crypto": "5940552778165264297", "play": "5766994197705921104", "dice": "5402282535755461614", "darts": "5400331881868698012", "profile": "6032693626394382504", "vip": "5251422397893989847", "win": "5449683594425410231", "lose": "5447183459602669338", "back": "5240241223632954241", "info": "5274099962655816924", "usdt": "5409048419211682843", "gift": "5197371802132800918", "join": "5449683594425410231", "search": "5420271804411292547", "admin": "5274099962655816924", "bot": "5291913773307667021", "lobby": "5282843764451195532", "games": "5282843764451195532", "id": "5291913773307667021"}
EMOJI_FALLBACK = {"wallet": "💰", "balance": "💵", "crypto": "💳", "play": "🎮", "dice": "🎲", "darts": "🎯", "profile": "👤", "vip": "👑", "win": "🏆", "lose": "❌", "back": "⬅️", "info": "ℹ️", "usdt": "💵", "gift": "🎁", "join": "✅", "search": "🔍", "admin": "🛡️", "bot": "🤖", "lobby": "🎮", "games": "🎮", "id": "🆔"}
def e(key): return f'<tg-emoji emoji-id="{EMOJI_IDS.get(key, "5274099962655816924")}">{EMOJI_FALLBACK.get(key, "⭐")}</tg-emoji>'

class S(StatesGroup):
    dep = State(); wd_amt = State(); wd_addr = State(); l_bet = State(); l_mode = State()
    chk_amt = State(); chk_cnt = State(); chk_get = State(); adm_give = State(); duel = State()

TEXT = {
"kz": {"w": "Сәлем! <b>Duel Games</b>!\n\n" + e("wallet") + " Балансыңыз: <b>${:.2f}</b>", "play": "Ойын", "prof": "Профиль", "bal": "Баланс", "dep": "Толықтыру", "wd": "Шығару", "lang": "Тіл", "adm": "Админ", "chk": "Мультичек", "dice": "Кости", "darts": "Дартс", "bet": "Ставка (USD):", "nom": "Баланс жеткіліксіз!", "dep_a": "Толықтыру сомасы (USD):", "pay": "Төлеу", "pay_c": "Төлем шоты: ${}", "wd_a": "Шығару сомасы (USD, min $1):", "wd_ad": "USDT адрес:\nTRC20: T...\nBEP20: 0x...\nTON: UQ...\n\n⚠️ Адрес қате болса, ақша қайтарылмайды!", "wd_s": "Өңделуде...", "pr_t": e("profile") + " <b>Профиль</b>\n" + e("id") + " ID: <code>{}</code>\n" + e("win") + " Жеңістер: {}\n" + e("lose") + " Жеңілістер: {}\n" + e("games") + " Ойындар: {}", "back": "Артқа", "bad": "Қате сома.", "l_c": "Лобби ашу", "l_s": "Іздеу", "l_j": "Кіру", "m_b": " Ботпен", "m_p": "Ойыншылармен", "b_w": "Бот лақтырып жатыр...", "b_t": "Сіз лақтырыңыз:", "b_d": "Тең! Қайтарылды.", "b_win": "Жеңдіңіз! ${}", "b_lose": "Жеңілдіңіз.", "chk_m": "Мультичек", "chk_c": "Жасау", "chk_g": "Алу", "chk_a": "Сома (USD):", "chk_cn": "Адам саны:", "adm_g": "ID мен сома (мыс: 123 5.5):"},
"ru": {"w": "Привет! <b>Duel Games</b>!\n\n" + e("wallet") + " Баланс: <b>${:.2f}</b>", "play": "Играть", "prof": "Профиль", "bal": "Баланс", "dep": "Пополнить", "wd": "Вывод", "lang": "Язык", "adm": "Админ", "chk": "Мультичек", "dice": "Кости", "darts": "Дартс", "bet": "Ставка (USD):", "nom": "Недостаточно средств!", "dep_a": "Сумма пополнения (USD):", "pay": "Оплатить", "pay_c": "Счёт: ${}", "wd_a": "Сумма вывода (USD, min $1):", "wd_ad": "Адрес USDT:\nTRC20: T...\nBEP20: 0x...\nTON: UQ...\n\n⚠️ Если адрес неверный — деньги не возвращаются!", "wd_s": "Обрабатывается...", "pr_t": e("profile") + " <b>Профиль</b>\n" + e("id") + " ID: <code>{}</code>\n" + e("win") + " Победы: {}\n" + e("lose") + " Поражения: {}\n" + e("games") + " Игры: {}", "back": "Назад", "bad": "Неверная сумма.", "l_c": "Создать лобби", "l_s": "Поиск", "l_j": "Войти", "m_b": " С ботом", "m_p": "С игроками", "b_w": "Бот бросает...", "b_t": "Ваш ход:", "b_d": "Ничья! Возврат.", "b_win": "Победа! ${}", "b_lose": "Проигрыш.", "chk_m": "Мультичек", "chk_c": "Создать", "chk_g": "Активировать", "chk_a": "Сумма (USD):", "chk_cn": "Кол-во людей:", "adm_g": "ID и сумма (напр: 123 5.5):"},
"en": {"w": "Hi! <b>Duel Games</b>!\n\n" + e("wallet") + " Balance: <b>${:.2f}</b>", "play": "Play", "prof": "Profile", "bal": "Balance", "dep": "Deposit", "wd": "Withdraw", "lang": "Language", "adm": "Admin", "chk": "Multi-check", "dice": "Dice", "darts": "Darts", "bet": "Bet (USD):", "nom": "Insufficient balance!", "dep_a": "Deposit amount (USD):", "pay": "Pay", "pay_c": "Invoice: ${}", "wd_a": "Withdraw amount (USD, min $1):", "wd_ad": "USDT address:\nTRC20: T...\nBEP20: 0x...\nTON: UQ...\n\n⚠️ Wrong address = no refund!", "wd_s": "Processing...", "pr_t": e("profile") + " <b>Profile</b>\n" + e("id") + " ID: <code>{}</code>\n" + e("win") + " Wins: {}\n" + e("lose") + " Losses: {}\n" + e("games") + " Games: {}", "back": "Back", "bad": "Invalid amount.", "l_c": "Create lobby", "l_s": "Search", "l_j": "Join", "m_b": " With bot", "m_p": "With players", "b_w": "Bot rolling...", "b_t": "Your turn:", "b_d": "Draw! Refunded.", "b_win": "You won! ${}", "b_lose": "You lost.", "chk_m": "Multi-check", "chk_c": "Create", "chk_g": "Claim", "chk_a": "Amount (USD):", "chk_cn": "Number of people:", "adm_g": "ID and amount (e.g. 123 5.5):"}
}

def main_kb(uid, l):
    t = TEXT[l]; kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=t['play'], callback_data="play", icon_custom_emoji_id=EMOJI_IDS["play"]), InlineKeyboardButton(text=t['prof'], callback_data="profile", icon_custom_emoji_id=EMOJI_IDS["profile"]))
    kb.row(InlineKeyboardButton(text=t['bal'], callback_data="balance", icon_custom_emoji_id=EMOJI_IDS["balance"]), InlineKeyboardButton(text=t['dep'], callback_data="deposit", icon_custom_emoji_id=EMOJI_IDS["wallet"]), InlineKeyboardButton(text=t['wd'], callback_data="withdraw", icon_custom_emoji_id=EMOJI_IDS["crypto"]))
    kb.row(InlineKeyboardButton(text=t['chk'], callback_data="check_menu", icon_custom_emoji_id=EMOJI_IDS["gift"]))
    if is_admin(uid): kb.row(InlineKeyboardButton(text=t['lang'], callback_data="lang", icon_custom_emoji_id=EMOJI_IDS["info"]), InlineKeyboardButton(text=t['adm'], callback_data="adm_h", icon_custom_emoji_id=EMOJI_IDS["admin"]))
    else: kb.row(InlineKeyboardButton(text=t['lang'], callback_data="lang", icon_custom_emoji_id=EMOJI_IDS["info"]))
    return kb.as_markup()

def back_kb(l, d="home"): return InlineKeyboardBuilder().row(InlineKeyboardButton(text=TEXT[l]["back"], callback_data=d, icon_custom_emoji_id=EMOJI_IDS["back"])).as_markup()

@dp.message(CommandStart())
async def start(m: Message):
    u = get_user(m.from_user.id)
    if len(m.text.split()) > 1:
        a = m.text.split()[1]
        if a.startswith("ref_"):
            try:
                r = int(a[4:])
                if r != m.from_user.id:
                    with db() as c, c.cursor() as cur: cur.execute("UPDATE users SET referrer_id=%s WHERE user_id=%s AND referrer_id IS NULL", (r, m.from_user.id))
            except: pass
        elif a.startswith("lobby_"): return await join_lobby(m, m.from_user.id, a[6:].upper())
        elif a.startswith("check_"): return await claim_chk(m, m.from_user.id, a[6:].upper())
    await m.answer(TEXT[u["lang"]]["w"].format(float(u["balance"])), reply_markup=main_kb(m.from_user.id, u["lang"]), parse_mode="HTML")

@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    u = get_user(c.from_user.id); await c.message.edit_text(TEXT[u["lang"]]["w"].format(float(u["balance"])), reply_markup=main_kb(c.from_user.id, u["lang"]), parse_mode="HTML"); await c.answer()

@dp.callback_query(F.data == "profile")
async def prof(c: CallbackQuery):
    u = get_user(c.from_user.id)
    await c.message.edit_text(TEXT[u["lang"]]["pr_t"].format(u["user_id"], u["wins"], u["losses"], u["games"]), reply_markup=back_kb(u["lang"]), parse_mode="HTML"); await c.answer()

@dp.callback_query(F.data == "balance")
async def bal(c: CallbackQuery):
    u = get_user(c.from_user.id); await c.message.edit_text(TEXT[u["lang"]]["w"].format(float(u["balance"])), reply_markup=back_kb(u["lang"]), parse_mode="HTML"); await c.answer()

@dp.callback_query(F.data == "lang")
async def lang(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for code, name in [("kz", "Қазақша"), ("ru", "Русский"), ("en", "English")]: kb.button(text=name, callback_data=f"sl_{code}", icon_custom_emoji_id=EMOJI_IDS["info"])
    kb.adjust(1); await c.message.edit_text("Тіл / Язык / Language:", reply_markup=kb.as_markup()); await c.answer()

@dp.callback_query(F.data.startswith("sl_"))
async def set_lang(c: CallbackQuery):
    l = c.data.split("_")[1]
    with db() as cn, cn.cursor() as cur: cur.execute("UPDATE users SET lang=%s WHERE user_id=%s", (l, c.from_user.id))
    u = get_user(c.from_user.id); await c.message.edit_text(TEXT[l]["w"].format(float(u["balance"])), reply_markup=main_kb(c.from_user.id, l), parse_mode="HTML"); await c.answer()

@dp.callback_query(F.data == "play")
async def play(c: CallbackQuery):
    t = TEXT[get_lang(c.from_user.id)]; kb = InlineKeyboardBuilder()
    kb.button(text=t["l_c"], callback_data="lc", icon_custom_emoji_id=EMOJI_IDS["join"]); kb.button(text=t["l_s"], callback_data="ls", icon_custom_emoji_id=EMOJI_IDS["search"]); kb.button(text=t["back"], callback_data="home", icon_custom_emoji_id=EMOJI_IDS["back"]); kb.adjust(1)
    await c.message.edit_text("🎮", reply_markup=kb.as_markup()); await c.answer()

@dp.callback_query(F.data == "lc")
async def lobby_create(c: CallbackQuery, state: FSMContext):
    l = get_lang(c.from_user.id); kb = InlineKeyboardBuilder()
    kb.button(text=TEXT[l]["dice"], callback_data="lcd", icon_custom_emoji_id=EMOJI_IDS["dice"]); kb.button(text=TEXT[l]["darts"], callback_data="lct", icon_custom_emoji_id=EMOJI_IDS["darts"]); kb.button(text=TEXT[l]["back"], callback_data="play", icon_custom_emoji_id=EMOJI_IDS["back"]); kb.adjust(1)
    await c.message.edit_text("🎲/🎯", reply_markup=kb.as_markup()); await c.answer()

@dp.callback_query(F.data.in_({"lcd", "lct"}))
async def lc_game(c: CallbackQuery, state: FSMContext):
    await state.update_data(g=c.data[2:]); await state.set_state(S.l_bet); await c.message.edit_text(TEXT[get_lang(c.from_user.id)]["bet"], parse_mode="HTML"); await c.answer()

@dp.message(S.l_bet)
async def lc_bet(m: Message, state: FSMContext):
    l = get_lang(m.from_user.id)
    try: a = money(m.text.replace(",", "."))
    except: return await m.answer(TEXT[l]["bad"])
    u = get_user(m.from_user.id)
    if Decimal(u["balance"]) < a: await m.answer(TEXT[l]["nom"]); return await state.clear()
    await state.update_data(b=str(a)); await state.set_state(S.l_mode)
    kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text=TEXT[l]["m_b"], callback_data="mb", icon_custom_emoji_id=EMOJI_IDS["bot"]), InlineKeyboardButton(text=TEXT[l]["m_p"], callback_data="mp", icon_custom_emoji_id=EMOJI_IDS["join"])).row(InlineKeyboardButton(text=TEXT[l]["back"], callback_data="home", icon_custom_emoji_id=EMOJI_IDS["back"]))
    await m.answer("Режим:", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "mb")
async def mode_bot(c: CallbackQuery, state: FSMContext):
    d = await state.get_data(); b = Decimal(d["b"]); g = d["g"]; u = get_user(c.from_user.id)
    if Decimal(u["balance"]) < b: return await c.answer(TEXT[u["lang"]]["nom"], show_alert=True)
    with db() as cn, cn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s", (b, c.from_user.id, b))
        if cur.rowcount != 1: return await c.answer("Err", show_alert=True)
    await state.set_state(S.duel); await state.update_data(b=str(b), g=g)
    await c.message.edit_text(TEXT[u["lang"]]["b_w"])
    ds = "🎲" if g == "dice" else "🎯"
    await asyncio.sleep(1); bd = await bot.send_dice(c.message.chat.id, emoji=ds); await asyncio.sleep(4)
    await state.update_data(bv=bd.dice.value); await c.message.answer(TEXT[u["lang"]]["b_t"]); await c.answer()

@dp.message(S.duel, F.dice)
async def duel_roll(m: Message, state: FSMContext):
    l = get_lang(m.from_user.id); d = await state.get_data()
    if m.forward_from or m.forward_from_chat or m.forward_origin: return await m.answer("❌")
    if m.dice.emoji != ("🎲" if d["g"] == "dice" else "🎯"): return await m.answer("❌")
    b = Decimal(d["b"]); bv = d.get("bv", 0); uv = m.dice.value
    with db() as cn, cn.cursor() as cur:
        if bv == uv: cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (b, m.from_user.id)); res = TEXT[l]["b_d"]
        elif uv > bv:
            p = (b * 2 * Decimal("0.95")).quantize(Decimal("0.01")); cur.execute("UPDATE users SET balance=balance+%s,wins=wins+1,games=games+1 WHERE user_id=%s", (p, m.from_user.id)); res = TEXT[l]["b_win"].format(p)
        else: cur.execute("UPDATE users SET losses=losses+1,games=games+1 WHERE user_id=%s", (m.from_user.id,)); res = TEXT[l]["b_lose"]
    await m.answer(res); await state.clear()

@dp.callback_query(F.data == "mp")
async def mode_pl(c: CallbackQuery, state: FSMContext):
    d = await state.get_data(); l = get_lang(c.from_user.id); lid = uuid.uuid4().hex[:6].upper()
    with db() as cn, cn.cursor() as cur:
        cur.execute("INSERT INTO lobbies VALUES(%s,%s,%s,%s,%s,'waiting')", (lid, c.from_user.id, c.message.chat.id, d["g"], d["b"]))
        cur.execute("INSERT INTO lobby_players VALUES(%s,%s)", (lid, c.from_user.id))
    link = f"https://t.me/{(await bot.get_me()).username}?start=lobby_{lid}"
    kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text=TEXT[l]["l_j"], url=link, icon_custom_emoji_id=EMOJI_IDS["join"])).row(InlineKeyboardButton(text=TEXT[l]["back"], callback_data="home", icon_custom_emoji_id=EMOJI_IDS["back"]))
    await c.message.edit_text(f"Код: <code>{lid}</code>\nСтавка: ${d['b']}\n\nСілтеме: {link}", reply_markup=kb.as_markup(), parse_mode="HTML"); await state.clear(); await c.answer()

@dp.callback_query(F.data == "ls")
async def l_search(c: CallbackQuery):
    l = get_lang(c.from_user.id)
    with db() as cn, cn.cursor() as cur:
        cur.execute("SELECT lobby_id,game_type,bet_amount FROM lobbies WHERE status='waiting' AND host_id != %s LIMIT 20", (c.from_user.id,)); rows = cur.fetchall()
    if not rows: return await c.message.edit_text("Бос.", reply_markup=back_kb(l, "play"))
    kb = InlineKeyboardBuilder()
    for lid, gt, b in rows: kb.button(text=f"{'🎲' if gt=='dice' else '🎯'} ${b} — {lid}", callback_data=f"lj_{lid}")
    kb.row(InlineKeyboardButton(text=TEXT[l]["back"], callback_data="play", icon_custom_emoji_id=EMOJI_IDS["back"])); kb.adjust(1)
    await c.message.edit_text("Лоббилер:", reply_markup=kb.as_markup()); await c.answer()

async def join_lobby(t, uid, lid):
    u = get_user(uid); l = u["lang"]
    with db() as cn, cn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM lobbies WHERE lobby_id=%s AND status='waiting'", (lid,)); lb = cur.fetchone()
        if not lb: return await t.answer("Табылмады.")
        cur.execute("SELECT user_id FROM lobby_players WHERE lobby_id=%s", (lid,)); ps = [r[0] for r in cur.fetchall()]
        b = Decimal(lb["bet_amount"])
        if uid in ps or len(ps) >= 2: return await t.answer("Толы.")
        if Decimal(u["balance"]) < b: return await t.answer(TEXT[l]["nom"])
        cur.execute("INSERT INTO lobby_players VALUES(%s,%s)", (lid, uid)); ps.append(uid); cur.execute("UPDATE lobbies SET status='playing' WHERE lobby_id=%s", (lid,))
    await t.answer("Кірдіңіз!")
    try:
        with db() as cn, cn.cursor() as cur:
            cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s", (b, ps[0], b))
            if cur.rowcount != 1: raise ValueError
            cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s", (b, ps[1], b))
            if cur.rowcount != 1: raise ValueError
    except:
        with db() as cn, cn.cursor() as cur: cur.execute("UPDATE lobbies SET status='waiting' WHERE lobby_id=%s", (lid,))
        return await bot.send_message(lb["chat_id"], "Баланс жеткіліксіз.")
    ds = "🎲" if lb["game_type"] == "dice" else "🎯"
    a = await bot.send_dice(lb["chat_id"], emoji=ds); await asyncio.sleep(4); bb = await bot.send_dice(lb["chat_id"], emoji=ds); await asyncio.sleep(4)
    with db() as cn, cn.cursor() as cur:
        if a.dice.value == bb.dice.value: cur.execute("UPDATE users SET balance=balance+%s WHERE user_id IN (%s,%s)", (b, ps[0], ps[1])); r = "Тең!"
        else:
            w, lo = (ps[0], ps[1]) if a.dice.value > bb.dice.value else (ps[1], ps[0]); p = (b * 2 * Decimal("0.95")).quantize(Decimal("0.01"))
            cur.execute("UPDATE users SET balance=balance+%s,wins=wins+1,games=games+1 WHERE user_id=%s", (p, w)); cur.execute("UPDATE users SET losses=losses+1,games=games+1 WHERE user_id=%s", (lo,)); r = f"Жеңімпаз: <code>{w}</code>\n${p}"
        cur.execute("DELETE FROM lobbies WHERE lobby_id=%s", (lid,))
    await bot.send_message(lb["chat_id"], r, parse_mode="HTML")

@dp.callback_query(F.data.startswith("lj_"))
async def j_btn(c: CallbackQuery): await join_lobby(c.message, c.from_user.id, c.data[3:]); await c.answer()
@dp.message(Command("join"))
async def j_cmd(m: Message):
    p = m.text.split()
    if len(p) == 2: await join_lobby(m, m.from_user.id, p[1].upper())

@dp.callback_query(F.data == "deposit")
async def dep(c: CallbackQuery, state: FSMContext): await state.set_state(S.dep); await c.message.edit_text(TEXT[get_lang(c.from_user.id)]["dep_a"]); await c.answer()
@dp.message(S.dep)
async def dep_a(m: Message, state: FSMContext):
    l = get_lang(m.from_user.id)
    try: a = money(m.text.replace(",", "."))
    except: return await m.answer(TEXT[l]["bad"])
    inv = await lp.invoices.create(str(a), order_id=f"dg_{m.from_user.id}_{uuid.uuid4().hex}", description="Dep")
    with db() as cn, cn.cursor() as cur: cur.execute("INSERT INTO payments(order_id,user_id,amount) VALUES(%s,%s,%s)", (inv.order_id, m.from_user.id, a))
    kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text=TEXT[l]["pay"], url=inv.pay_url, icon_custom_emoji_id=EMOJI_IDS["crypto"]))
    await m.answer(TEXT[l]["pay_c"].format(a), reply_markup=kb.as_markup()); await state.clear()

@lp.invoice_paid()
async def paid(inv):
    with db() as cn, cn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM payments WHERE order_id=%s FOR UPDATE", (str(inv.order_id),)); p = cur.fetchone()
        if not p or p["status"] == "paid": return
        if Decimal(str(inv.amount)) != Decimal(str(p["amount"])): return
        cur.execute("UPDATE payments SET status='paid',paid_at=NOW() WHERE order_id=%s", (p["order_id"],))
        cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (p["amount"], p["user_id"]))
    try: await bot.send_message(p["user_id"], "✅ Толықтырылды!")
    except: pass

ADDRESS_PATTERNS = {"TRX": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"), "BSC": re.compile(r"^0x[a-fA-F0-9]{40}$"), "GRAM": re.compile(r"^(EQ|UQ)[A-Za-z0-9_-]{46,48}$")}
def detect_net(a):
    for k, v in ADDRESS_PATTERNS.items():
        if v.match(a): return k
    return None

@dp.callback_query(F.data == "withdraw")
async def wd(c: CallbackQuery, state: FSMContext): await state.set_state(S.wd_amt); await c.message.edit_text(TEXT[get_lang(c.from_user.id)]["wd_a"]); await c.answer()

@dp.message(S.wd_amt)
async def wd_a(m: Message, state: FSMContext):
    l = get_lang(m.from_user.id)
    try: a = money(m.text.replace(",", "."))
    except: return await m.answer(TEXT[l]["bad"])
    if Decimal(get_user(m.from_user.id)["balance"]) < a: await m.answer(TEXT[l]["nom"]); return await state.clear()
    await state.update_data(a=str(a)); await state.set_state(S.wd_addr); await m.answer(TEXT[l]["wd_ad"], parse_mode="HTML")

@dp.message(S.wd_addr)
async def wd_ad(m: Message, state: FSMContext):
    l = get_lang(m.from_user.id); ad = m.text.strip(); net = detect_net(ad)
    if not net or net not in ("TRX", "BSC", "GRAM"): return await m.answer("❌ Формат қате (TRC20/BEP20/TON)")
    d = await state.get_data(); a = Decimal(d["a"]); uid = m.from_user.id
    async with httpx.AsyncClient() as cl:
        r = await cl.get(f"{LAVEPAY_BASE}/withdrawals/networks", headers={"X-Api-Key": LAVEPAY_API_KEY})
        info = next((i for i in r.json().get("data", {}).get("networks", []) if i["asset"] == "USDT" and i["network"] == net), None)
    if not info: return await m.answer("⚠️ Желі қолжетімсіз.")
    fee = Decimal(info.get("fee", "0")); total = a + fee
    if a < Decimal(info.get("min_amount", "0")): return await m.answer(f"❌ Min: {info['min_amount']}")
    with db() as cn, cn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s RETURNING balance", (total, uid, total))
        if not cur.fetchone(): await m.answer(TEXT[l]["nom"]); return await state.clear()
        cur.execute("INSERT INTO withdrawals(user_id,amount,address,network) VALUES(%s,%s,%s,%s) RETURNING id", (uid, a, ad, net)); wid = cur.fetchone()[0]
    await m.answer(TEXT[l]["wd_s"])
    async with httpx.AsyncClient(timeout=30) as cl:
        res = await cl.post(f"{LAVEPAY_BASE}/withdrawals", headers={"X-Api-Key": LAVEPAY_API_KEY, "Content-Type": "application/json"}, json={"asset": "USDT", "network": net, "address": ad, "amount": str(a), "idempotency_key": f"wd-{wid}-{uid}"})
    r = res.json()
    with db() as cn, cn.cursor() as cur:
        if r.get("ok") and r["data"].get("status") != "failed":
            cur.execute("UPDATE withdrawals SET status=%s,tx_id=%s WHERE id=%s", (r["data"].get("status"), r["data"].get("tx_hash"), wid)); await m.answer(f"✅ Жіберілді! Tx: {r['data'].get('tx_hash') or '...'}")
        else: cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (total, uid)); cur.execute("UPDATE withdrawals SET status='failed' WHERE id=%s", (wid,)); await m.answer("❌ Қате, баланс қайтарылды.")
    await state.clear()

@dp.callback_query(F.data == "check_menu")
async def chk_menu(c: CallbackQuery):
    l = get_lang(c.from_user.id); kb = InlineKeyboardBuilder()
    if is_admin(c.from_user.id): kb.button(text=TEXT[l]["chk_c"], callback_data="chk_c", icon_custom_emoji_id=EMOJI_IDS["gift"])
    kb.button(text=TEXT[l]["chk_g"], callback_data="chk_g", icon_custom_emoji_id=EMOJI_IDS["join"]).button(text=TEXT[l]["back"], callback_data="home", icon_custom_emoji_id=EMOJI_IDS["back"]).adjust(1)
    await c.message.edit_text(TEXT[l]["chk_m"], reply_markup=kb.as_markup()); await c.answer()

@dp.callback_query(F.data == "chk_c")
async def chk_create(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔")
    await state.set_state(S.chk_amt); await c.message.edit_text(TEXT[get_lang(c.from_user.id)]["chk_a"]); await c.answer()

@dp.message(S.chk_amt)
async def chk_amt(m: Message, state: FSMContext):
    try: a = money(m.text.replace(",", "."))
    except: return await m.answer(TEXT[get_lang(m.from_user.id)]["bad"])
    await state.update_data(a=str(a)); await state.set_state(S.chk_cnt); await m.answer(TEXT[get_lang(m.from_user.id)]["chk_cn"])

@dp.message(S.chk_cnt)
async def chk_cnt(m: Message, state: FSMContext):
    try: cn = int(m.text)
    except: return await m.answer("Сан енгізіңіз.")
    d = await state.get_data(); a = Decimal(d["a"]); cid = uuid.uuid4().hex[:8].upper(); uid = m.from_user.id
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s", (a, uid, a))
        if cur.rowcount != 1: await m.answer("Баланс жеткіліксіз."); return await state.clear()
        cur.execute("INSERT INTO multi_checks VALUES(%s,%s,%s,0,%s,'active')", (cid, a, cn, uid))
    await m.answer(f"✅ Чек: https://t.me/{(await bot.get_me()).username}?start=check_{cid}"); await state.clear()

@dp.callback_query(F.data == "chk_g")
async def chk_get_p(c: CallbackQuery, state: FSMContext): await state.set_state(S.chk_get); await c.message.edit_text("Код:"); await c.answer()

@dp.message(S.chk_get)
async def chk_get_c(m: Message, state: FSMContext):
    t = m.text.strip()
    cid = t.split("start=check_")[1].split()[0].upper() if "start=check_" in t else t.upper()
    await claim_chk(m, m.from_user.id, cid); await state.clear()

async def claim_chk(t, uid, cid):
    l = get_lang(uid)
    with db() as cn, cn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM multi_checks WHERE check_id=%s FOR UPDATE", (cid,)); c = cur.fetchone()
        if not c or c["status"] != "active": return await t.answer("Табылмады.")
        if c["claimed"] >= c["max_claims"]: return await t.answer("Бітті.")
        cur.execute("SELECT 1 FROM check_claims WHERE check_id=%s AND user_id=%s", (cid, uid))
        if cur.fetchone(): return await t.answer("Алынған.")
        p = (Decimal(c["amount"]) / c["max_claims"]).quantize(Decimal("0.01"))
        cur.execute("INSERT INTO check_claims VALUES(%s,%s)", (cid, uid)); cur.execute("UPDATE multi_checks SET claimed=claimed+1 WHERE check_id=%s", (cid,))
        if c["claimed"] + 1 >= c["max_claims"]: cur.execute("UPDATE multi_checks SET status='finished' WHERE check_id=%s", (cid,))
        cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (p, uid))
    await t.answer(f"🎉 ${p} алдыңыз!")

@dp.message(Command("admin"))
async def adm(m: Message):
    if not is_admin(m.from_user.id): return
    kb = InlineKeyboardBuilder().button(text="Баланс беру", callback_data="adm_g").button(text="Шығарулар", callback_data="adm_w").button(text="⬅️", callback_data="home").adjust(1)
    await m.answer("Админ панель:", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "adm_h")
async def adm_h(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔")
    kb = InlineKeyboardBuilder().button(text="Баланс беру", callback_data="adm_g").button(text="Шығарулар", callback_data="adm_w").button(text="⬅️", callback_data="home").adjust(1)
    await c.message.edit_text("Админ панель:", reply_markup=kb.as_markup()); await c.answer()

@dp.callback_query(F.data == "adm_g")
async def adm_g(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔")
    await state.set_state(S.adm_give); await c.message.edit_text(TEXT[get_lang(c.from_user.id)]["adm_g"]); await c.answer()

@dp.message(S.adm_give)
async def adm_give_p(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    try: p = m.text.split(); tid = int(p[0]); a = money(p[1].replace(",", "."))
    except: return await m.answer("Қате формат.")
    get_user(tid)
    with db() as cn, cn.cursor() as cur: cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (a, tid))
    await m.answer("✅"); await state.clear()
    try: await bot.send_message(tid, f"🎁 +${a}")
    except: pass

@dp.callback_query(F.data == "adm_w")
async def adm_w(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔")
    with db() as cn, cn.cursor() as cur:
        cur.execute("SELECT id,user_id,amount,address FROM withdrawals WHERE status='pending'"); rows = cur.fetchall()
    if not rows: return await c.message.edit_text("Бос.", reply_markup=back_kb("kz", "adm_h"))
    txt = "".join(f"#{r[0]} | {r[1]} | ${r[2]}\n<code>{r[3]}</code>\n\n" for r in rows); kb = InlineKeyboardBuilder()
    for r in rows: kb.button(text=f"✅#{r[0]}", callback_data=f"wa_{r[0]}").button(text=f"❌#{r[0]}", callback_data=f"wr_{r[0]}")
    kb.row(InlineKeyboardButton(text="⬅️", callback_data="adm_h"))
    await c.message.edit_text(txt, reply_markup=kb.as_markup(), parse_mode="HTML"); await c.answer()

@dp.callback_query(F.data.startswith("wa_"))
async def wa(c: CallbackQuery):
    wid = int(c.data[3:])
    with db() as cn, cn.cursor() as cur: cur.execute("UPDATE withdrawals SET status='approved' WHERE id=%s RETURNING user_id,amount", (wid,)); r = cur.fetchone()
    if r: await bot.send_message(r[0], f"✅ #{wid} расталды. ${r[1]}")
    await c.message.edit_text("✅"); await c.answer()

@dp.callback_query(F.data.startswith("wr_"))
async def wr(c: CallbackQuery):
    wid = int(c.data[3:])
    with db() as cn, cn.cursor() as cur:
        cur.execute("UPDATE withdrawals SET status='rejected' WHERE id=%s RETURNING user_id,amount,network", (wid,)); r = cur.fetchone()
        if r:
            fee = Decimal("0")
            cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (r[1] + fee, r[0]))
    if r: await bot.send_message(r[0], f"❌ #{wid} қабылданбады.")
    await c.message.edit_text("❌"); await c.answer()

async def handle_wd_done(data):
    try: wid = int(data.get("idempotency_key", "").split("-")[1])
    except: return
    with db() as cn, cn.cursor() as cur: cur.execute("UPDATE withdrawals SET status='approved',tx_id=%s WHERE id=%s", (data.get("tx_hash"), wid))

async def handle_wd_fail(data):
    try: wid = int(data.get("idempotency_key", "").split("-")[1])
    except: return
    with db() as cn, cn.cursor() as cur:
        cur.execute("SELECT amount,user_id FROM withdrawals WHERE id=%s", (wid,)); r = cur.fetchone()
        if r: cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (r[0], r[1])); cur.execute("UPDATE withdrawals SET status='failed' WHERE id=%s", (wid,))

@app.post("/lavepay/withdrawals")
async def wh(request: Request):
    b = await request.body(); 
    try: d = json.loads(b)
    except: return {"ok": False}
    if d.get("type") == "withdrawal.completed": await handle_wd_done(d.get("data", {}))
    elif d.get("type") == "withdrawal.failed": await handle_wd_fail(d.get("data", {}))
    return {"ok": True}

@app.get("/")
async def h(): return {"ok": True}

install_lavepay(app, lp)

@app.on_event("startup")
async def st():
    init_db(); app.state.bt = asyncio.create_task(dp.start_polling(bot))

@app.on_event("shutdown")
async def sd():
    t = getattr(app.state, "bt", None)
    if t: t.cancel(); await bot.session.close(); _pool.closeall()

if __name__ == "__main__": uvicorn.run(app, host="0.0.0.0", port=PORT)
