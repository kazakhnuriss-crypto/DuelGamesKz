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

EMOJI_IDS = {
    "wallet": "5420271804411292547",
    "balance": "5992085874546844207",
    "crypto": "5940552778165264297",
    "play": "5766994197705921104",
    "dice": "5402282535755461614",
    "darts": "5400331881868698012",
    "profile": "6032693626394382504",
    "vip": "5251422397893989847",
    "win": "5449683594425410231",
    "lose": "5447183459602669338",
    "back": "5240241223632954241",
    "info": "5274099962655816924",
    "admin": "5274099962655816924",
    "usdt": "5409048419211682843",
    "rub": "5231449120635370684",
    "kzt": "5364045318092504570",
    "id": "5291913773307667021",
    "games": "5282843764451195532",
    "anonymous": "5251422397893989847",
    "lobby": "5282843764451195532",
    "search": "5420271804411292547",
    "join": "5449683594425410231",
}

EMOJI_FALLBACK = {
    "wallet": "💰", "balance": "💵", "crypto": "💳", "play": "🎮",
    "dice": "🎲", "darts": "🎯", "profile": "👤", "vip": "👑",
    "win": "🏆", "lose": "❌", "back": "⬅️", "info": "ℹ️",
    "admin": "🛡️", "usdt": "💵", "rub": "💴", "kzt": "💸",
    "id": "🆔", "games": "🎮", "anonymous": "👤",
    "lobby": "🎮", "search": "🔍", "join": "✅",
}

def e(key):
    emoji = EMOJI_FALLBACK.get(key, "⭐")
    emoji_id = EMOJI_IDS[key]
    return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'


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
            status TEXT NOT NULL DEFAULT 'waiting',
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS lobby_players (
            lobby_id TEXT REFERENCES lobbies(lobby_id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            PRIMARY KEY(lobby_id, user_id)
        )""")


def ensure_user(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users(user_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (uid,)
        )


def get_user(uid):
    with db() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.DictCursor
    ) as cur:
        cur.execute("SELECT * FROM users WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        if row:
            return row
    ensure_user(uid)
    with db() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.DictCursor
    ) as cur:
        cur.execute("SELECT * FROM users WHERE user_id=%s", (uid,))
        return cur.fetchone()


def get_lang(uid):
    return get_user(uid)["lang"] or "kz"


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


async def get_referral_data(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM users WHERE referrer_id=%s", (uid,)
        )
        count = cur.fetchone()[0]
    me = await bot.get_me()
    return count, f"https://t.me/{me.username}?start=ref_{uid}"


def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID
    TEXT = {
    "kz": {
        "welcome": "Сәлем! <b>Duel Games</b>!\n\n" + e("wallet") + " Балансыңыз: <b>${:.2f}</b>\n\nНе істейміз?",
        "play": "Ойын ойнау",
        "profile": "Профиль",
        "balance": "Баланс",
        "rating": "Рейтинг",
        "referrals": "Реферал",
        "deposit": "Пополнить",
        "withdraw": "Вывод",
        "lang": "Тіл",
        "admin_btn": "Админ панель",
        "choose": e("play") + " Ойынды таңдаңыз:",
        "dice": "Кости дуэль",
        "darts": "Дартс дуэль",
        "bet": "Ставканы енгізіңіз (USD):",
        "no_money": "Балансыңыз жеткіліксіз!",
        "deposit_amount": e("wallet") + " Толықтырғыңыз келетін соманы USD түрінде енгізіңіз:",
        "pay": "Төлеу",
        "created": e("crypto") + " Төлем шоты жасалды: ${}\nТөлеу үшін батырманы басыңыз.",
        "paid": e("wallet") + " Төлем қабылданды! Жаңа баланс: ${:.2f}",
        "withdraw_amount": e("crypto") + " Шығарылатын соманы енгізіңіз (ең азы $1):",
        "withdraw_address": e("usdt") + " USDT TRC20 әмиян мекенжайын енгізіңіз:",
        "withdraw_sent": "Өтінім қабылданды. Әкімші тексергеннен кейін өңдейді.",
        "profile_text": e("profile") + " <b>Профиль</b>\n" + e("id") + " ID: <code>{}</code>\n" + e("win") + " Жеңістер: {}\n" + e("lose") + " Жеңілістер: {}\n" + e("games") + " Ойындар: {}\n" + e("vip") + " VIP: {}\n" + e("anonymous") + " Аноним: {}\n" + e("info") + " Рефералдар: {}",
        "balance_text": e("balance") + " Баланс: ${:.2f}",
        "rating_text": e("games") + " <b>Рейтинг ойыншылар</b>",
        "referral_text": e("info") + " <b>Реферал жүйесі</b>\n\nДосыңды шақыр. Тіркелген реферал саны: <b>{}</b>\n\nСенің сілтемең:\n<code>{}</code>",
        "language": e("info") + " Тілді таңдаңыз:",
        "back": "Артқа",
        "home": "Басты мәзір",
        "bad_amount": "Соманы дұрыс енгізіңіз.",
        "admin_only": "Бұл команда тек әкімшіге арналған.",
        "lobby_menu": e("lobby") + " <b>Лобби</b>\n\nЛобби ашыңыз немесе онлайн лобби іздеңіз:",
        "lobby_create": "Лобби ашу",
        "lobby_search": "Онлайн лобби іздеу",
        "lobby_created": "Лобби ашылды!\nКод: <code>{}</code>\nСтавка: ${}\nОйын: {}\n\nСілтеме: {}",
        "lobby_search_empty": "Қазір онлайн лобби жоқ. Өзіңіз ашыңыз!",
        "lobby_search_title": e("search") + " <b>Онлайн лоббилер</b>\n\nЛобби таңдаңыз:",
        "lobby_joined": "Лоббиге кірдіңіз! Ойын басталады...",
        "lobby_not_found": "Лобби табылмады немесе толы.",
    },
    "ru": {
        "welcome": "Привет! <b>Duel Games</b>!\n\n" + e("wallet") + " Баланс: <b>${:.2f}</b>\n\nЧто делаем?",
        "play": "Играть",
        "profile": "Профиль",
        "balance": "Баланс",
        "rating": "Рейтинг",
        "referrals": "Рефералы",
        "deposit": "Пополнить",
        "withdraw": "Вывод",
        "lang": "Язык",
        "admin_btn": "Админ панель",
        "choose": e("play") + " Выберите игру:",
        "dice": "Дуэль на костях",
        "darts": "Дуэль в дартс",
        "bet": "Введите ставку (USD):",
        "no_money": "Недостаточно средств!",
        "deposit_amount": e("wallet") + " Введите сумму пополнения в USD:",
        "pay": "Оплатить",
        "created": e("crypto") + " Счёт создан на ${}. Нажмите кнопку оплаты.",
        "paid": e("wallet") + " Платёж получен! Новый баланс: ${:.2f}",
        "withdraw_amount": e("crypto") + " Введите сумму вывода (минимум $1):",
        "withdraw_address": e("usdt") + " Введите адрес USDT TRC20:",
        "withdraw_sent": "Заявка принята и будет обработана администратором.",
        "profile_text": e("profile") + " <b>Профиль</b>\n" + e("id") + " ID: <code>{}</code>\n" + e("win") + " Победы: {}\n" + e("lose") + " Поражения: {}\n" + e("games") + " Игры: {}\n" + e("vip") + " VIP: {}\n" + e("anonymous") + " Аноним: {}\n" + e("info") + " Рефералы: {}",
        "balance_text": e("balance") + " Баланс: ${:.2f}",
        "rating_text": e("games") + " <b>Рейтинг игроков</b>",
        "referral_text": e("info") + " <b>Реферальная система</b>\n\nПриглашай друзей. Зарегистрировано рефералов: <b>{}</b>\n\nТвоя ссылка:\n<code>{}</code>",
        "language": e("info") + " Выберите язык:",
        "back": "Назад",
        "home": "Главное меню",
        "bad_amount": "Введите корректную сумму.",
        "admin_only": "Только для администратора.",
        "lobby_menu": e("lobby") + " <b>Лобби</b>\n\nСоздайте лобби или найдите онлайн:",
        "lobby_create": "Создать лобби",
        "lobby_search": "Поиск онлайн лобби",
        "lobby_created": "Лобби создано!\nКод: <code>{}</code>\nСтавка: ${}\nИгра: {}\n\nСсылка: {}",
        "lobby_search_empty": "Сейчас нет онлайн лобби. Создайте своё!",
        "lobby_search_title": e("search") + " <b>Онлайн лобби</b>\n\nВыберите лобби:",
        "lobby_joined": "Вы вошли в лобби! Игра начинается...",
        "lobby_not_found": "Лобби не найдено или заполнено.",
    },
    "en": {
        "welcome": "Hi! <b>Duel Games</b>!\n\n" + e("wallet") + " Balance: <b>${:.2f}</b>\n\nWhat do we do?",
        "play": "Play",
        "profile": "Profile",
        "balance": "Balance",
        "rating": "Rating",
        "referrals": "Referrals",
        "deposit": "Deposit",
        "withdraw": "Withdraw",
        "lang": "Language",
        "admin_btn": "Admin panel",
        "choose": e("play") + " Choose a game:",
        "dice": "Dice duel",
        "darts": "Darts duel",
        "bet": "Enter bet (USD):",
        "no_money": "Insufficient balance!",
        "deposit_amount": e("wallet") + " Enter deposit amount in USD:",
        "pay": "Pay",
        "created": e("crypto") + " Invoice created for ${}. Tap to pay.",
        "paid": e("wallet") + " Payment received! New balance: ${:.2f}",
        "withdraw_amount": e("crypto") + " Enter withdrawal amount (minimum $1):",
        "withdraw_address": e("usdt") + " Enter your USDT TRC20 wallet address:",
        "withdraw_sent": "Request received. An administrator will process it.",
        "profile_text": e("profile") + " <b>Profile</b>\n" + e("id") + " ID: <code>{}</code>\n" + e("win") + " Wins: {}\n" + e("lose") + " Losses: {}\n" + e("games") + " Games: {}\n" + e("vip") + " VIP: {}\n" + e("anonymous") + " Anonymous: {}\n" + e("info") + " Referrals: {}",
        "balance_text": e("balance") + " Balance: ${:.2f}",
        "rating_text": e("games") + " <b>Player leaderboard</b>",
        "referral_text": e("info") + " <b>Referral system</b>\n\nInvite friends. Registered referrals: <b>{}</b>\n\nYour link:\n<code>{}</code>",
        "language": e("info") + " Choose language:",
        "back": "Back",
        "home": "Main menu",
        "bad_amount": "Enter a valid amount.",
        "admin_only": "Admin only.",
        "lobby_menu": e("lobby") + " <b>Lobbies</b>\n\nCreate a lobby or find online:",
        "lobby_create": "Create lobby",
        "lobby_search": "Search online lobbies",
        "lobby_created": "Lobby created!\nCode: <code>{}</code>\nBet: ${}\nGame: {}\n\nLink: {}",
        "lobby_search_empty": "No online lobbies right now. Create one!",
        "lobby_search_title": e("search") + " <b>Online lobbies</b>\n\nChoose a lobby:",
        "lobby_joined": "You joined the lobby! Game starts...",
        "lobby_not_found": "Lobby not found or full.",
    },
}


class AmountState(StatesGroup):
    deposit = State()
    withdraw = State()
    address = State()
    bet = State()
    lobby_bet = State()


def main_kb(uid, lang):
    """Басты мәзір. Админ батырмасы тек ADMIN_ID-ге."""
    t = TEXT[lang]
    kb = InlineKeyboardBuilder()
    kb.button(text=t["play"], callback_data="play",
              icon_custom_emoji_id=EMOJI_IDS["play"])
    kb.button(text=t["profile"], callback_data="profile",
              icon_custom_emoji_id=EMOJI_IDS["profile"])
    kb.button(text=t["balance"], callback_data="balance",
              icon_custom_emoji_id=EMOJI_IDS["balance"])
    kb.button(text=t["referrals"], callback_data="referrals",
              icon_custom_emoji_id=EMOJI_IDS["info"])
    kb.button(text=t["rating"], callback_data="rating",
              icon_custom_emoji_id=EMOJI_IDS["games"])
    kb.button(text=t["deposit"], callback_data="deposit",
              icon_custom_emoji_id=EMOJI_IDS["wallet"])
    kb.button(text=t["withdraw"], callback_data="withdraw",
              icon_custom_emoji_id=EMOJI_IDS["wallet"])
    kb.button(text=t["lang"], callback_data="language",
              icon_custom_emoji_id=EMOJI_IDS["info"])

    # 🆕 Админ батырмасы — тек админге
    if is_admin(uid):
        kb.button(text=t["admin_btn"], callback_data="admin_home",
                  icon_custom_emoji_id=EMOJI_IDS["admin"])

    kb.adjust(4, 4)
    if is_admin(uid):
        kb.adjust(4, 4, 1)
    return kb.as_markup()


def back_kb(lang, data="home"):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT[lang]["back"], callback_data=data,
              icon_custom_emoji_id=EMOJI_IDS["back"])
    return kb.as_markup()


def profile_kb(lang):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT[lang]["back"], callback_data="home",
              icon_custom_emoji_id=EMOJI_IDS["back"])
    return kb.as_markup()


def lobby_menu_kb(lang):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT[lang]["lobby_create"], callback_data="lobby_create",
              icon_custom_emoji_id=EMOJI_IDS["join"])
    kb.button(text=TEXT[lang]["lobby_search"], callback_data="lobby_search",
              icon_custom_emoji_id=EMOJI_IDS["search"])
    kb.button(text=TEXT[lang]["back"], callback_data="home",
              icon_custom_emoji_id=EMOJI_IDS["back"])
    kb.adjust(1)
    return kb.as_markup()
    @dp.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    u = get_user(c.from_user.id)
    lang = u["lang"]
    t = TEXT[lang]

    referral_count, _ = await get_referral_data(c.from_user.id)

    text = t["profile_text"].format(
        u["user_id"], u["wins"], u["losses"], u["games"],
        "✅" if u["is_vip"] else "❌",
        "✅" if u["is_anonymous"] else "❌",
        referral_count
    )

    await c.message.edit_text(text, reply_markup=profile_kb(lang),
                              parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "referrals")
async def referrals(c: CallbackQuery):
    lang = get_lang(c.from_user.id)
    count, link = await get_referral_data(c.from_user.id)

    await c.message.edit_text(
        TEXT[lang]["referral_text"].format(count, link),
        reply_markup=back_kb(lang),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await c.answer()


@dp.callback_query(F.data == "balance")
async def balance(c: CallbackQuery):
    u = get_user(c.from_user.id)
    lang = u["lang"]
    await c.message.edit_text(
        TEXT[lang]["balance_text"].format(float(u["balance"])),
        reply_markup=back_kb(lang),
        parse_mode="HTML"
    )
    await c.answer()


@dp.callback_query(F.data == "rating")
async def rating(c: CallbackQuery):
    lang = get_lang(c.from_user.id)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, wins, games FROM users "
            "ORDER BY wins DESC, games DESC, user_id ASC LIMIT 10"
        )
        rows = cur.fetchall()

    lines = [TEXT[lang]["rating_text"]]
    if not rows:
        lines.append("\nӘзірге бос.")
    else:
        for pos, (uid, wins, games) in enumerate(rows, 1):
            name = f"<code>{uid}</code>"
            try:
                chat = await bot.get_chat(uid)
                if chat.username:
                    name = f"@{chat.username}"
                elif chat.first_name:
                    name = chat.first_name
            except Exception:
                pass
            lines.append(f"\n{pos}. {name} — {wins}")

    await c.message.edit_text("".join(lines),
                              reply_markup=back_kb(lang),
                              parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "language")
async def language(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for code, name in [("kz", "Қазақша"), ("ru", "Русский"), ("en", "English")]:
        kb.button(text=name, callback_data=f"setlang_{code}",
                  icon_custom_emoji_id=EMOJI_IDS["info"])
    kb.adjust(1)
    await c.message.edit_text(
        TEXT[get_lang(c.from_user.id)]["language"],
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )
    await c.answer()


@dp.callback_query(F.data.startswith("setlang_"))
async def set_language(c: CallbackQuery):
    lang = c.data.split("_", 1)[1]
    if lang not in TEXT:
        await c.answer("Тіл табылмады.", show_alert=True)
        return
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET lang=%s WHERE user_id=%s",
                    (lang, c.from_user.id))
    u = get_user(c.from_user.id)
    await c.message.edit_text(
        TEXT[lang]["welcome"].format(float(u["balance"])),
        reply_markup=main_kb(c.from_user.id, lang),
        parse_mode="HTML"
    )
    await c.answer()
    # ============ ЛОББИ ============

@dp.callback_query(F.data == "play")
async def play(c: CallbackQuery):
    t = TEXT[get_lang(c.from_user.id)]
    kb = InlineKeyboardBuilder()
    kb.button(text=t["lobby_create"], callback_data="lobby_create",
              icon_custom_emoji_id=EMOJI_IDS["join"])
    kb.button(text=t["lobby_search"], callback_data="lobby_search",
              icon_custom_emoji_id=EMOJI_IDS["search"])
    kb.button(text=t["back"], callback_data="home",
              icon_custom_emoji_id=EMOJI_IDS["back"])
    kb.adjust(1)
    await c.message.edit_text(t["lobby_menu"],
                              reply_markup=kb.as_markup(),
                              parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "lobby_create")
async def lobby_create(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT[lang]["dice"], callback_data="lc_dice",
              icon_custom_emoji_id=EMOJI_IDS["dice"])
    kb.button(text=TEXT[lang]["darts"], callback_data="lc_darts",
              icon_custom_emoji_id=EMOJI_IDS["darts"])
    kb.button(text=TEXT[lang]["back"], callback_data="play",
              icon_custom_emoji_id=EMOJI_IDS["back"])
    kb.adjust(1)
    await c.message.edit_text(TEXT[lang]["choose"],
                              reply_markup=kb.as_markup(),
                              parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.in_({"lc_dice", "lc_darts"}))
async def lobby_choose_game(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id)
    game = c.data.removeprefix("lc_")
    await state.update_data(game=game)
    await state.set_state(AmountState.lobby_bet)
    await c.message.edit_text(
        (e("dice") if game == "dice" else e("darts")) + " " + TEXT[lang]["bet"],
        parse_mode="HTML"
    )
    await c.answer()


@dp.message(AmountState.lobby_bet)
async def lobby_set_bet(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    try:
        amount = money(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except Exception:
        await message.answer(TEXT[lang]["bad_amount"])
        return

    u = get_user(message.from_user.id)
    if Decimal(u["balance"]) < amount:
        await message.answer(TEXT[lang]["no_money"])
        await state.clear()
        return

    data = await state.get_data()
    game = data["game"]
    lobby_id = uuid.uuid4().hex[:6].upper()

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lobbies(lobby_id,host_id,chat_id,game_type,bet_amount) "
            "VALUES(%s,%s,%s,%s,%s)",
            (lobby_id, message.from_user.id, message.chat.id, game, amount)
        )
        cur.execute(
            "INSERT INTO lobby_players(lobby_id,user_id) VALUES(%s,%s)",
            (lobby_id, message.from_user.id)
        )

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=lobby_{lobby_id}"

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Лоббиге кіру", url=link,
              icon_custom_emoji_id=EMOJI_IDS["join"])
    kb.button(text=TEXT[lang]["back"], callback_data="home",
              icon_custom_emoji_id=EMOJI_IDS["back"])
    kb.adjust(1)

    await message.answer(
        TEXT[lang]["lobby_created"].format(
            lobby_id, amount,
            TEXT[lang][game], link
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await state.clear()


@dp.callback_query(F.data == "lobby_search")
async def lobby_search(c: CallbackQuery):
    lang = get_lang(c.from_user.id)
    uid = c.from_user.id

    with db() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.DictCursor
    ) as cur:
        cur.execute("""
            SELECT l.*,
                (SELECT COUNT(*) FROM lobby_players lp
                 WHERE lp.lobby_id=l.lobby_id) AS cnt
            FROM lobbies l
            WHERE l.status='waiting' AND l.host_id != %s
            ORDER BY l.created_at DESC LIMIT 20
        """, (uid,))
        rows = cur.fetchall()

    if not rows:
        await c.message.edit_text(
            TEXT[lang]["lobby_search_empty"],
            reply_markup=back_kb(lang, "play"),
            parse_mode="HTML"
        )
        await c.answer()
        return

    kb = InlineKeyboardBuilder()
    for r in rows:
        label = (
            f"{'🎲' if r['game_type']=='dice' else '🎯'} "
            f"${r['bet_amount']} — {r['lobby_id']}"
        )
        kb.button(text=label, callback_data=f"lj_{r['lobby_id']}")
    kb.button(text=TEXT[lang]["back"], callback_data="play",
              icon_custom_emoji_id=EMOJI_IDS["back"])
    kb.adjust(1)

    await c.message.edit_text(
        TEXT[lang]["lobby_search_title"],
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await c.answer()


async def try_join_lobby(target, uid, lobby_id):
    """Лоббиге кіру. target — Message немесе CallbackQuery."""
    u = get_user(uid)
    lang = u["lang"]

    with db() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.DictCursor
    ) as cur:
        cur.execute(
            "SELECT * FROM lobbies WHERE lobby_id=%s AND status='waiting'",
            (lobby_id,)
        )
        lobby = cur.fetchone()
        if not lobby:
            await target.answer(TEXT[lang]["lobby_not_found"])
            return

        cur.execute(
            "SELECT user_id FROM lobby_players WHERE lobby_id=%s",
            (lobby_id,)
        )
        players = [r[0] for r in cur.fetchall()]
        bet = Decimal(lobby["bet_amount"])

        if uid in players or len(players) >= 2:
            await target.answer(TEXT[lang]["lobby_not_found"])
            return

        if Decimal(u["balance"]) < bet:
            await target.answer(TEXT[lang]["no_money"])
            return

        cur.execute(
            "INSERT INTO lobby_players(lobby_id,user_id) VALUES(%s,%s)",
            (lobby_id, uid)
        )
        players.append(uid)
        cur.execute(
            "UPDATE lobbies SET status='playing' WHERE lobby_id=%s",
            (lobby_id,)
        )

    await target.answer(TEXT[lang]["lobby_joined"])
    await run_duel(lobby_id, players, lobby["game_type"], bet, lobby["chat_id"])


@dp.callback_query(F.data.startswith("lj_"))
async def lobby_join_by_btn(c: CallbackQuery):
    lobby_id = c.data.removeprefix("lj_")
    await try_join_lobby(c.message, c.from_user.id, lobby_id)
    await c.answer()


@dp.message(Command("join"))
async def join_by_code(message: Message):
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Қолдану: /join LOBBY_ID")
        return
    await try_join_lobby(message, message.from_user.id, parts[1].upper())


async def run_duel(lobby_id, players, game, bet, chat_id):
    p1, p2 = players

    # Екі ойыншының да балансын шегеру (атомды)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET balance=balance-%s "
            "WHERE user_id IN (%s,%s) AND balance >= %s",
            (bet, p1, p2, bet)
        )
        if cur.rowcount != 2:
            cur.execute(
                "UPDATE lobbies SET status='waiting' WHERE lobby_id=%s",
                (lobby_id,)
            )
            await bot.send_message(
                chat_id, "Баланс жеткіліксіз. Ойын тоқтатылды."
            )
            return

    dice_symbol = "🎲" if game == "dice" else "🎯"
    a = await bot.send_dice(chat_id, emoji=dice_symbol)
    await asyncio.sleep(4)
    b = await bot.send_dice(chat_id, emoji=dice_symbol)
    await asyncio.sleep(4)

    with db() as conn, conn.cursor() as cur:
        if a.dice.value == b.dice.value:
            cur.execute(
                "UPDATE users SET balance=balance+%s WHERE user_id IN (%s,%s)",
                (bet, p1, p2)
            )
            result = "🤝 Тең ойын! Ставкалар қайтарылды."
        else:
            winner, loser = (p1, p2) if a.dice.value > b.dice.value else (p2, p1)
            prize = (bet * 2 * Decimal("0.95")).quantize(Decimal("0.01"))
            cur.execute(
                "UPDATE users SET balance=balance+%s,wins=wins+1,games=games+1 "
                "WHERE user_id=%s", (prize, winner)
            )
            cur.execute(
                "UPDATE users SET losses=losses+1,games=games+1 WHERE user_id=%s",
                (loser,)
            )
            result = f"🏆 Жеңімпаз: <code>{winner}</code>\n💰 Ұтыс: ${prize}"
        cur.execute("DELETE FROM lobbies WHERE lobby_id=%s", (lobby_id,))

    await bot.send_message(chat_id, result, parse_mode="HTML")


# ============ АДМИН ПАНЕЛЬ (тек ADMIN_ID) ============

@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer(TEXT[get_lang(message.from_user.id)]["admin_only"])
        return
    await admin_show_home(message, edit=False)


@dp.callback_query(F.data == "admin_home")
async def admin_home_cb(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("⛔ Тек әкімшіге", show_alert=True)
        return
    await admin_show_home(c.message, edit=True)
    await c.answer()


async def admin_show_home(message, edit=False):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*),COALESCE(SUM(balance),0) FROM users")
        users, total = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")
        pending_w = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM lobbies WHERE status='waiting'")
        open_l = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM lobbies WHERE status='playing'")
        playing = cur.fetchone()[0]

    kb = InlineKeyboardBuilder()
    kb.button(text="💸 Шығарулар", callback_data="adm_withdrawals")
    kb.button(text="👥 Юзерлер", callback_data="adm_users")
    kb.button(text="🎮 Лоббилер", callback_data="adm_lobbies")
    kb.button(text="⬅️ Басты мәзір", callback_data="home")
    kb.adjust(1)

    text = (
        e("admin") + " <b>Admin panel</b>\n\n"
        f"👥 Юзерлер: <b>{users}</b>\n"
        f"💰 Жалпы баланс: <b>${total}</b>\n"
        f"⏳ Күтіп тұрған шығару: <b>{pending_w}</b>\n"
        f"🎮 Ашық лобби: <b>{open_l}</b>\n"
        f"⚔️ Ойында: <b>{playing}</b>"
    )
    if edit:
        await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "adm_withdrawals")
async def adm_withdrawals(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("⛔", show_alert=True)
        return
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id,user_id,amount,address FROM withdrawals "
            "WHERE status='pending' ORDER BY id"
        )
        rows = cur.fetchall()

    if not rows:
        await c.message.edit_text(
            "✅ Күтіп тұрған шығару жоқ.",
            reply_markup=back_kb("kz", "admin_home")
        )
        await c.answer()
        return

    text = "💸 <b>Күтіп тұрған шығарулар</b>\n\n"
    kb = InlineKeyboardBuilder()
    for wid, uid, amt, addr in rows[:10]:
        text += f"#{wid} | <code>{uid}</code> | <b>${amt}</b>\n<code>{addr}</code>\n\n"
        kb.button(text=f"✅ #{wid}", callback_data=f"wapprove_{wid}")
        kb.button(text=f"❌ #{wid}", callback_data=f"wreject_{wid}")
    kb.button(text="⬅️ Артқа", callback_data="admin_home")
    kb.adjust(2, 2, 2, 2, 2, 1)
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.startswith("wapprove_"))
async def wapprove(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("⛔", show_alert=True)
        return
    wid = int(c.data.split("_")[1])
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE withdrawals SET status='approved' "
            "WHERE id=%s AND status='pending' RETURNING user_id,amount",
            (wid,)
        )
        row = cur.fetchone()
    if not row:
        await c.answer("Өңделген", show_alert=True)
        return
    await bot.send_message(row[0], f"✅ Шығару #{wid} расталды: ${row[1]}")
    await c.message.edit_text(f"✅ #{wid} approved", parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.startswith("wreject_"))
async def wreject(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("⛔", show_alert=True)
        return
    wid = int(c.data.split("_")[1])
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE withdrawals SET status='rejected' "
            "WHERE id=%s AND status='pending' RETURNING user_id,amount",
            (wid,)
        )
        row = cur.fetchone()
        if not row:
            await c.answer("Өңделген", show_alert=True)
            return
        cur.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (row[1], row[0])
        )
    await bot.send_message(row[0], f"❌ Шығару #{wid} қабылданбады. ${row[1]} қайтарылды.")
    await c.message.edit_text(f"❌ #{wid} rejected", parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "adm_lobbies")
async def adm_lobbies(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("⛔", show_alert=True)
        return
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT lobby_id,host_id,game_type,bet_amount,status "
            "FROM lobbies ORDER BY created_at DESC LIMIT 20"
        )
        rows = cur.fetchall()
    if not rows:
        text = "🎮 Лобби жоқ."
    else:
        text = "🎮 <b>Лоббилер</b>\n\n"
        for lid, hid, gt, bet, st in rows:
            text += f"<code>{lid}</code> | {gt} | ${bet} | {st} | host {hid}\n"
    await c.message.edit_text(text, reply_markup=back_kb("kz", "admin_home"),
                              parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "adm_users")
async def adm_users(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("⛔", show_alert=True)
        return
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*),COALESCE(SUM(balance),0) FROM users")
        users, total = cur.fetchone()
    await c.message.edit_text(
        f"👥 Юзерлер: <b>{users}</b>\n💰 Жалпы баланс: <b>${total}</b>",
        reply_markup=back_kb("kz", "admin_home"),
        parse_mode="HTML"
    )
    await c.answer()
