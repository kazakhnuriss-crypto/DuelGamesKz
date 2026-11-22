# ===== БӨЛІК 1 БАСЫ =====
import asyncio
import os
import uuid
import re
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager

import httpx
import psycopg2
import psycopg2.extras
from psycopg2 import pool
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

LAVEPAY_BASE = "https://laveshka.io/merchant/v1"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
lp = LavePay(LAVEPAY_API_KEY, webhook_secret=LAVEPAY_WEBHOOK_SECRET)
app = FastAPI(title="Duel Games")

# ===== CONNECTION POOL =====
_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(1, 20, DATABASE_URL, sslmode="require")
    return _pool

@contextmanager
def db():
    p = get_pool()
    conn = p.getconn()
    try:
        with conn:
            yield conn
    finally:
        p.putconn(conn)

# ===== DB INIT — DROP TABLE ЖОҚ! =====
def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, balance NUMERIC(18,2) NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
            games INTEGER NOT NULL DEFAULT 0, lang TEXT NOT NULL DEFAULT 'kz',
            is_vip BOOLEAN NOT NULL DEFAULT FALSE, is_anonymous BOOLEAN NOT NULL DEFAULT FALSE,
            referrer_id BIGINT, has_deposited BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            order_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, amount NUMERIC(18,2) NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMP NOT NULL DEFAULT NOW(), paid_at TIMESTAMP
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, amount NUMERIC(18,2) NOT NULL,
            address TEXT NOT NULL, network TEXT NOT NULL DEFAULT 'TRX',
            status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            processed_at TIMESTAMP, tx_id TEXT
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS lobbies (
            lobby_id TEXT PRIMARY KEY, host_id BIGINT NOT NULL, chat_id BIGINT NOT NULL,
            game_type TEXT NOT NULL, bet_amount NUMERIC(18,2) NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting', created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS lobby_players (
            lobby_id TEXT REFERENCES lobbies(lobby_id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL, PRIMARY KEY(lobby_id, user_id)
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS multi_checks (
            check_id TEXT PRIMARY KEY, amount NUMERIC(18,2) NOT NULL, max_claims INTEGER NOT NULL,
            claimed INTEGER NOT NULL DEFAULT 0, creator_id BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS check_claims (
            check_id TEXT REFERENCES multi_checks(check_id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL, claimed_at TIMESTAMP NOT NULL DEFAULT NOW(),
            PRIMARY KEY(check_id, user_id)
        )""")

# ===== USER FUNCTIONS =====
def ensure_user(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO users(user_id) VALUES(%s) ON CONFLICT (user_id) DO NOTHING", (uid,))

def get_user(uid):
    ensure_user(uid)
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM users WHERE user_id=%s", (uid,))
        return cur.fetchone()

def get_lang(uid):
    try:
        return get_user(uid)["lang"] or "kz"
    except Exception:
        return "kz"

def money(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid amount")

def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID

# ===== АДРЕС ТЕКСЕРУ =====
ADDRESS_PATTERNS = {
    "TRX": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
    "BSC": re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "GRAM": re.compile(r"^(EQ|UQ)[A-Za-z0-9_-]{46,48}$"),
    "ETH": re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "SOL": re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
    "LTC": re.compile(r"^(ltc1[a-z0-9]{39,59}|[LM][1-9A-HJ-NP-Za-km-z]{26,33})$"),
}

def detect_network(address: str):
    if ADDRESS_PATTERNS["TRX"].match(address):
        return "TRX"
    if ADDRESS_PATTERNS["GRAM"].match(address):
        return "GRAM"
    if ADDRESS_PATTERNS["BSC"].match(address):
        return "BSC"
    if ADDRESS_PATTERNS["LTC"].match(address):
        return "LTC"
    if ADDRESS_PATTERNS["SOL"].match(address):
        return "SOL"
    return None

# ===== LAVEPAY АВТО-ШЫҒАРУ =====
async def lavepay_withdraw(asset: str, network: str, address: str, amount: Decimal, idem_key: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{LAVEPAY_BASE}/withdrawals",
            headers={"X-Api-Key": LAVEPAY_API_KEY, "Content-Type": "application/json"},
            json={
                "asset": asset, "network": network, "address": address,
                "amount": str(amount), "idempotency_key": idem_key[:64]
            }
        )
    return r.json()

async def get_network_info(asset: str = "USDT", network: str = "TRX"):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{LAVEPAY_BASE}/withdrawals/networks",
                                 headers={"X-Api-Key": LAVEPAY_API_KEY})
        data = r.json()
        if data.get("ok"):
            for item in data["data"].get("networks", []):
                if item.get("asset") == asset and item.get("network") == network:
                    return item
    except Exception:
        pass
    return None

# ===== PREMIUM EMOJI =====
EMOJI_IDS = {
    "wallet": "5420271804411292547", "balance": "5992085874546844207",
    "crypto": "5940552778165264297", "play": "5766994197705921104",
    "dice": "5402282535755461614", "darts": "5400331881868698012",
    "profile": "6032693626394382504", "vip": "5251422397893989847",
    "win": "5449683594425410231", "lose": "5447183459602669338",
    "back": "5240241223632954241", "info": "5274099962655816924",
    "usdt": "5409048419211682843", "gift": "5197371802132800918",
    "join": "5449683594425410231", "search": "5420271804411292547",
    "admin": "5274099962655816924", "bot": "5291913773307667021",
    "lobby": "5282843764451195532", "games": "5282843764451195532",
    "id": "5291913773307667021", "anonymous": "5251422397893989847",
}

EMOJI_FALLBACK = {
    "wallet": "💰", "balance": "💵", "crypto": "💳", "play": "🎮",
    "dice": "🎲", "darts": "🎯", "profile": "👤", "vip": "👑",
    "win": "🏆", "lose": "❌", "back": "⬅️", "info": "ℹ️",
    "usdt": "💵", "gift": "🎁", "join": "✅", "search": "🔍",
    "admin": "🛡️", "bot": "🤖", "lobby": "🎮", "games": "🎮",
    "id": "🆔", "anonymous": "👤",
}

def e(key):
    return f'<tg-emoji emoji-id="{EMOJI_IDS.get(key, "5274099962655816924")}">{EMOJI_FALLBACK.get(key, "⭐")}</tg-emoji>'

# ===== FSM STATES =====
class AmountState(StatesGroup):
    deposit = State()
    withdraw = State()
    address = State()
    lobby_bet = State()
    lobby_mode = State()
    check_create_amount = State()
    check_create_count = State()
    check_get = State()
    admin_give = State()
    duel_wait_dice = State()

# ===== TEXT (3 ТІЛ) =====
TEXT = {
    "kz": {
        "welcome": "Сәлем! <b>Duel Games</b>!\n\n" + e("wallet") + " Балансыңыз: <b>${:.2f}</b>\n\nНе істейміз?",
        "play": "Ойын", "profile": "Профиль", "balance": "Баланс",
        "rating": "Рейтинг", "referrals": "Реферал", "deposit": "Толықтыру",
        "withdraw": "Шығару", "lang": "Тіл", "admin_btn": "Админ",
        "check_btn": "Мультичек", "choose": e("play") + " Ойынды таңдаңыз:",
        "dice": "Кости", "darts": "Дартс",
        "bet": "Ставканы енгізіңіз (USD):", "no_money": "Балансыңыз жеткіліксіз!",
        "deposit_amount": e("wallet") + " Толықтырғыңыз келетін соманы USD түрінде енгізіңіз:",
        "pay": "Төлеу", "created": e("crypto") + " Төлем шоты жасалды: ${}\nТөлеу үшін батырманы басыңыз.",
        "paid": e("wallet") + " Төлем қабылданды! Жаңа баланс: ${:.2f}",
        "withdraw_amount": e("crypto") + " Шығарылатын соманы енгізіңіз (ең азы $1):",
        "withdraw_address": e("usdt") + " USDT әмиян мекенжайын енгізіңіз:\n\nTRC20: T... (34 таңба)\nBEP20: 0x... (42 таңба)\nTON: UQ.../EQ...\n\n⚠️ Адрес қате болса, ақша қайтарылмайды!",
        "withdraw_sent": "Өтінім қабылданды. Өңделуде...",
        "withdraw_success": "✅ Шығару #{wid} жіберілді!\nСома: ${amount} USDT\nЖелі: {network}\nTx: <code>{tx}</code>",
        "withdraw_failed": "❌ Шығару #{wid} сәтсіз. Баланс қайтарылды.",
        "bad_address": "❌ Адрес форматы танылмады.\n\nTRC20: T... (34)\nBEP20: 0x... (42)\nTON: UQ.../EQ...",
        "profile_text": e("profile") + " <b>Профиль</b>\n" + e("id") + " ID: <code>{}</code>\n" + e("win") + " Жеңістер: {}\n" + e("lose") + " Жеңілістер: {}\n" + e("games") + " Ойындар: {}\n" + e("vip") + " VIP: {}\n" + e("anonymous") + " Аноним: {}\n" + e("info") + " Рефералдар: {}",
        "balance_text": e("balance") + " Баланс: ${:.2f}",
        "rating_text": e("games") + " <b>Рейтинг ойыншылар</b>",
        "referral_text": e("info") + " <b>Реферал жүйесі</b>\n\nДосыңды шақыр. Тіркелген реферал саны: <b>{}</b>\n\nСенің сілтемең:\n<code>{}</code>",
        "language": e("info") + " Тілді таңдаңыз:", "back": "Артқа",
        "bad_amount": "Соманы дұрыс енгізіңіз.", "admin_only": "Бұл команда тек әкімшіге арналған.",
        "lobby_menu": e("lobby") + " <b>Лобби</b>\n\nЛобби ашыңыз немесе онлайн лобби іздеңіз:",
        "lobby_create": "Лобби ашу", "lobby_search": "Онлайн лобби іздеу",
        "lobby_created": "Лобби ашылды!\nКод: <code>{}</code>\nСтавка: ${}\nОйын: {}\n\nСілтеме: {}",
        "lobby_search_empty": "Қазір онлайн лобби жоқ. Өзіңіз ашыңыз!",
        "lobby_search_title": e("search") + " <b>Онлайн лоббилер</b>\n\nЛобби таңдаңыз:",
        "lobby_joined": "Лоббиге кірдіңіз! Ойын басталады...",
        "lobby_not_found": "Лобби табылмады немесе толы.", "lobby_join_btn": "Лоббиге кіру",
        "lobby_mode": "Ойын режимін таңдаңыз:",
        "mode_bot": e("bot") + " Ботпен ойнау", "mode_players": "👥 Ойыншылармен ойнау",
        "bot_waiting": "🤖 Бот кости лақтырып жатыр... Күте тұрыңыз.",
        "bot_your_turn": "🎲 Енді сіз лақтырыңыз! Төмендегі костиді басыңыз:",
        "bot_draw": "🤝 Тең ойын! Ставка қайтарылды.",
        "bot_win": "🏆 Сіз жеңдіңіз! Ұтыс: ${}",
        "bot_lose": "❌ Сіз жеңілдіңіз. Боттың нәтижесі жоғарырақ болды.",
        "bot_invalid": "⚠️ Тек өзіңіз лақтырған костиді жіберіңіз!",
        "check_menu": e("gift") + " <b>Мультичек</b>\n\nМультичек дегеніміз — белгілі бір соманы көп адамға бөлу.",
        "check_create_btn": "Мультичек жасау", "check_get_btn": "Мультичекті алу",
        "check_amount": "Жалпы соманы енгізіңіз (USD):",
        "check_count": "Қанша адам ала алады? (санын енгізіңіз):",
        "check_created": "✅ Мультичек жасалды!\nЖалпы сома: ${}\nАдам саны: {}\nБір адамға: ${}\n\nСілтеме: {}",
        "check_get_prompt": "🎁 Мультичек сілтемесін немесе кодын жіберіңіз:",
        "check_not_found": "Мультичек табылмады немесе аяқталды.",
        "check_already": "Сіз бұл мультичектен ақша алып қойғансыз!",
        "check_empty": "Бұл мультичек бітті, ақша қалмады.",
        "check_success": "🎉 Сіз мультичектен ${} алдыңыз! Балансыңыз: ${:.2f}",
        "admin_give_prompt": "Қай ID-ге ақша бересіз? (ID мен соманы бірге жазыңыз, мысалы: 12345678 5.5):",
        "admin_give_success": "✅ Баланс толықтырылды!"
    },
    "ru": {
        "welcome": "Привет! <b>Duel Games</b>!\n\n" + e("wallet") + " Баланс: <b>${:.2f}</b>\n\nЧто делаем?",
        "play": "Играть", "profile": "Профиль", "balance": "Баланс",
        "rating": "Рейтинг", "referrals": "Рефералы", "deposit": "Пополнить",
        "withdraw": "Вывод", "lang": "Язык", "admin_btn": "Админ",
        "check_btn": "Мультичек", "choose": e("play") + " Выберите игру:",
        "dice": "Кости", "darts": "Дартс",
        "bet": "Введите ставку (USD):", "no_money": "Недостаточно средств!",
        "deposit_amount": e("wallet") + " Введите сумму пополнения в USD:",
        "pay": "Оплатить", "created": e("crypto") + " Счёт создан на ${}. Нажмите кнопку оплаты.",
        "paid": e("wallet") + " Платёж получен! Новый баланс: ${:.2f}",
        "withdraw_amount": e("crypto") + " Введите сумму вывода (минимум $1):",
        "withdraw_address": e("usdt") + " Введите адрес USDT:\n\nTRC20: T... (34)\nBEP20: 0x... (42)\nTON: UQ.../EQ...\n\n⚠️ Если адрес неверный — деньги не возвращаются!",
        "withdraw_sent": "Заявка принята. Обрабатывается...",
        "withdraw_success": "✅ Вывод #{wid} отправлен!\nСумма: ${amount} USDT\nСеть: {network}\nTx: <code>{tx}</code>",
        "withdraw_failed": "❌ Вывод #{wid} не удался. Баланс возвращён.",
        "bad_address": "❌ Формат адреса не распознан.\n\nTRC20: T... (34)\nBEP20: 0x... (42)\nTON: UQ.../EQ...",
        "profile_text": e("profile") + " <b>Профиль</b>\n" + e("id") + " ID: <code>{}</code>\n" + e("win") + " Победы: {}\n" + e("lose") + " Поражения: {}\n" + e("games") + " Игры: {}\n" + e("vip") + " VIP: {}\n" + e("anonymous") + " Аноним: {}\n" + e("info") + " Рефералы: {}",
        "balance_text": e("balance") + " Баланс: ${:.2f}",
        "rating_text": e("games") + " <b>Рейтинг игроков</b>",
        "referral_text": e("info") + " <b>Реферальная система</b>\n\nПриглашай друзей. Зарегистрировано рефералов: <b>{}</b>\n\nТвоя ссылка:\n<code>{}</code>",
        "language": e("info") + " Выберите язык:", "back": "Назад",
        "bad_amount": "Введите корректную сумму.", "admin_only": "Только для администратора.",
        "lobby_menu": e("lobby") + " <b>Лобби</b>\n\nСоздайте лобби или найдите онлайн:",
        "lobby_create": "Создать лобби", "lobby_search": "Поиск онлайн лобби",
        "lobby_created": "Лобби создано!\nКод: <code>{}</code>\nСтавка: ${}\nИгра: {}\n\nСсылка: {}",
        "lobby_search_empty": "Сейчас нет онлайн лобби. Создайте своё!",
        "lobby_search_title": e("search") + " <b>Онлайн лобби</b>\n\nВыберите лобби:",
        "lobby_joined": "Вы вошли в лобби! Игра начинается...",
        "lobby_not_found": "Лобби не найдено или заполнено.", "lobby_join_btn": "Войти в лобби",
        "lobby_mode": "Выберите режим игры:",
        "mode_bot": e("bot") + " Играть с ботом", "mode_players": "👥 Играть с игроками",
        "bot_waiting": "🤖 Бот бросает кости... Подождите.",
        "bot_your_turn": "🎲 Теперь ваш ход! Нажмите на кости ниже:",
        "bot_draw": "🤝 Ничья! Ставка возвращена.",
        "bot_win": "🏆 Вы победили! Выигрыш: ${}",
        "bot_lose": "❌ Вы проиграли. У бота результат выше.",
        "bot_invalid": "⚠️ Отправляйте только свои кости!",
        "check_menu": e("gift") + " <b>Мультичек</b>\n\nМультичек — это разделение суммы на многих людей.",
        "check_create_btn": "Создать мультичек", "check_get_btn": "Активировать чек",
        "check_amount": "Введите общую сумму (USD):",
        "check_count": "Сколько человек могут получить? (введите число):",
        "check_created": "✅ Мультичек создан!\nОбщая сумма: ${}\nКол-во людей: {}\nНа человека: ${}\n\nСсылка: {}",
        "check_get_prompt": "🎁 Отправьте ссылку или код мультичека:",
        "check_not_found": "Чек не найден или уже недействителен.",
        "check_already": "Вы уже получали деньги с этого чека!",
        "check_empty": "Этот чек уже пуст.",
        "check_success": "🎉 Вы получили ${} с мультичека! Ваш баланс: ${:.2f}",
        "admin_give_prompt": "Кому дать баланс? (Напишите ID и сумму вместе, например: 12345678 5.5):",
        "admin_give_success": "✅ Баланс пополнен!"
    },
    "en": {
        "welcome": "Hi! <b>Duel Games</b>!\n\n" + e("wallet") + " Balance: <b>${:.2f}</b>\n\nWhat do we do?",
        "play": "Play", "profile": "Profile", "balance": "Balance",
        "rating": "Rating", "referrals": "Referrals", "deposit": "Deposit",
        "withdraw": "Withdraw", "lang": "Language", "admin_btn": "Admin",
        "check_btn": "Multi-check", "choose": e("play") + " Choose a game:",
        "dice": "Dice", "darts": "Darts",
        "bet": "Enter bet (USD):", "no_money": "Insufficient balance!",
        "deposit_amount": e("wallet") + " Enter deposit amount in USD:",
        "pay": "Pay", "created": e("crypto") + " Invoice created for ${}. Tap to pay.",
        "paid": e("wallet") + " Payment received! New balance: ${:.2f}",
        "withdraw_amount": e("crypto") + " Enter withdrawal amount (minimum $1):",
        "withdraw_address": e("usdt") + " Enter USDT address:\n\nTRC20: T... (34)\nBEP20: 0x... (42)\nTON: UQ.../EQ...\n\n⚠️ Wrong address = no refund!",
        "withdraw_sent": "Request received. Processing...",
        "withdraw_success": "✅ Withdrawal #{wid} sent!\nAmount: ${amount} USDT\nNetwork: {network}\nTx: <code>{tx}</code>",
        "withdraw_failed": "❌ Withdrawal #{wid} failed. Balance refunded.",
        "bad_address": "❌ Address format not recognized.\n\nTRC20: T... (34)\nBEP20: 0x... (42)\nTON: UQ.../EQ...",
        "profile_text": e("profile") + " <b>Profile</b>\n" + e("id") + " ID: <code>{}</code>\n" + e("win") + " Wins: {}\n" + e("lose") + " Losses: {}\n" + e("games") + " Games: {}\n" + e("vip") + " VIP: {}\n" + e("anonymous") + " Anonymous: {}\n" + e("info") + " Referrals: {}",
        "balance_text": e("balance") + " Balance: ${:.2f}",
        "rating_text": e("games") + " <b>Player leaderboard</b>",
        "referral_text": e("info") + " <b>Referral system</b>\n\nInvite friends. Registered referrals: <b>{}</b>\n\nYour link:\n<code>{}</code>",
        "language": e("info") + " Choose language:", "back": "Back",
        "bad_amount": "Enter a valid amount.", "admin_only": "Admin only.",
        "lobby_menu": e("lobby") + " <b>Lobbies</b>\n\nCreate a lobby or find online:",
        "lobby_create": "Create lobby", "lobby_search": "Search online lobbies",
        "lobby_created": "Lobby created!\nCode: <code>{}</code>\nBet: ${}\nGame: {}\n\nLink: {}",
        "lobby_search_empty": "No online lobbies right now. Create one!",
        "lobby_search_title": e("search") + " <b>Online lobbies</b>\n\nChoose a lobby:",
        "lobby_joined": "You joined the lobby! Game starts...",
        "lobby_not_found": "Lobby not found or full.", "lobby_join_btn": "Join lobby",
        "lobby_mode": "Choose game mode:",
        "mode_bot": e("bot") + " Play with bot", "mode_players": "👥 Play with players",
        "bot_waiting": "🤖 Bot is rolling... Please wait.",
        "bot_your_turn": "🎲 Your turn! Tap the dice below:",
        "bot_draw": "🤝 Draw! Bet refunded.",
        "bot_win": "🏆 You won! Prize: ${}",
        "bot_lose": "❌ You lost. Bot's roll was higher.",
        "bot_invalid": "⚠️ Send only YOUR OWN dice!",
        "check_menu": e("gift") + " <b>Multi-check</b>\n\nMulti-check splits a sum among multiple people.",
        "check_create_btn": "Create multi-check", "check_get_btn": "Claim multi-check",
        "check_amount": "Enter total amount (USD):",
        "check_count": "How many people can claim? (enter number):",
        "check_created": "✅ Multi-check created!\nTotal: ${}\nUsers: {}\nPer user: ${}\n\nLink: {}",
        "check_get_prompt": "🎁 Send the multi-check link or code:",
        "check_not_found": "Check not found or expired.",
        "check_already": "You already claimed from this check!",
        "check_empty": "This check is empty.",
        "check_success": "🎉 You claimed ${} from multi-check! Your balance: ${:.2f}",
        "admin_give_prompt": "Who to give balance? (Send ID and amount together, e.g. 12345678 5.5):",
        "admin_give_success": "✅ Balance added!"
    },
}

# ===== ДИЗАЙН — ТОПТАСТЫРЫЛҒАН КНОПКАЛАР =====
def main_kb(uid, lang):
    t = TEXT[lang]
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=f"🎮 {t['play']}", callback_data="play",
                             icon_custom_emoji_id=EMOJI_IDS["play"]),
        InlineKeyboardButton(text=f"👤 {t['profile']}", callback_data="profile",
                             icon_custom_emoji_id=EMOJI_IDS["profile"]),
    )
    kb.row(
        InlineKeyboardButton(text=f"💵 {t['balance']}", callback_data="balance",
                             icon_custom_emoji_id=EMOJI_IDS["balance"]),
        InlineKeyboardButton(text=f"💰 {t['deposit']}", callback_data="deposit",
                             icon_custom_emoji_id=EMOJI_IDS["wallet"]),
        InlineKeyboardButton(text=f"💸 {t['withdraw']}", callback_data="withdraw",
                             icon_custom_emoji_id=EMOJI_IDS["crypto"]),
    )
    kb.row(
        InlineKeyboardButton(text=f"🔗 {t['referrals']}", callback_data="referrals",
                             icon_custom_emoji_id=EMOJI_IDS["info"]),
        InlineKeyboardButton(text=f"🏆 {t['rating']}", callback_data="rating",
                             icon_custom_emoji_id=EMOJI_IDS["games"]),
    )
    kb.row(
        InlineKeyboardButton(text=f"🎁 {t['check_btn']}", callback_data="check_menu",
                             icon_custom_emoji_id=EMOJI_IDS["gift"]),
    )
    if is_admin(uid):
        kb.row(
            InlineKeyboardButton(text=f"🌐 {t['lang']}", callback_data="language",
                                 icon_custom_emoji_id=EMOJI_IDS["info"]),
            InlineKeyboardButton(text=f"🛡️ {t['admin_btn']}", callback_data="admin_home",
                                 icon_custom_emoji_id=EMOJI_IDS["admin"]),
        )
    else:
        kb.row(InlineKeyboardButton(text=f"🌐 {t['lang']}", callback_data="language",
                                    icon_custom_emoji_id=EMOJI_IDS["info"]))
    return kb.as_markup()

def back_kb(lang, data="home"):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT[lang]["back"], callback_data=data, icon_custom_emoji_id=EMOJI_IDS["back"])
    return kb.as_markup()

def profile_kb(lang):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT[lang]["back"], callback_data="home", icon_custom_emoji_id=EMOJI_IDS["back"])
    return kb.as_markup()

# ===== REFERRAL =====
async def get_referral_data(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM users WHERE referrer_id=%s", (uid,))
        count = cur.fetchone()[0]
    try:
        me = await bot.get_me()
        username = me.username
    except Exception:
        username = "DuelGamesBot"
    return count, f"https://t.me/{username}?start=ref_{uid}"

# ===== БАСҚА ФУНКЦИЯЛАР — БӨЛІК 2-ДЕ =====
# ===== БӨЛІК 1 СОҢЫ =====
# ===== БӨЛІК 2 БАСЫ =====
# Бұл бөлікті БӨЛІК 1-дің соңына қосасыз

# ===== START =====
@dp.message(CommandStart())
async def start(message: Message):
    u = get_user(message.from_user.id)
    args = message.text.split()

    if len(args) > 1:
        if args[1].startswith("ref_"):
            try:
                ref = int(args[1][4:])
                if ref != message.from_user.id:
                    with db() as conn, conn.cursor() as cur:
                        cur.execute(
                            "UPDATE users AS u SET referrer_id=%s WHERE u.user_id=%s AND u.referrer_id IS NULL AND EXISTS (SELECT 1 FROM users WHERE user_id=%s)",
                            (ref, message.from_user.id, ref)
                        )
            except (ValueError, TypeError):
                pass
        elif args[1].startswith("lobby_"):
            await try_join_lobby(message, message.from_user.id, args[1][6:].upper())
            return
        elif args[1].startswith("check_"):
            await claim_check(message, message.from_user.id, args[1][6:].upper())
            return

    await message.answer(
        TEXT[u["lang"]]["welcome"].format(float(u["balance"])),
        reply_markup=main_kb(message.from_user.id, u["lang"]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    u = get_user(c.from_user.id)
    await c.message.edit_text(
        TEXT[u["lang"]]["welcome"].format(float(u["balance"])),
        reply_markup=main_kb(c.from_user.id, u["lang"]),
        parse_mode="HTML"
    )
    await c.answer()

# ===== PROFILE =====
@dp.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    try:
        u = get_user(c.from_user.id)
        lang = u["lang"]
        count, _ = await get_referral_data(c.from_user.id)
        text = TEXT[lang]["profile_text"].format(
            u["user_id"], u["wins"], u["losses"], u["games"],
            "✅" if u.get("is_vip") else "❌",
            "✅" if u.get("is_anonymous") else "❌", count
        )
        await c.message.edit_text(text, reply_markup=profile_kb(lang), parse_mode="HTML")
    except Exception as ex:
        print("Profile Error:", ex)
    await c.answer()

# ===== BALANCE =====
@dp.callback_query(F.data == "balance")
async def balance(c: CallbackQuery):
    u = get_user(c.from_user.id)
    await c.message.edit_text(
        TEXT[u["lang"]]["balance_text"].format(float(u["balance"])),
        reply_markup=back_kb(u["lang"]),
        parse_mode="HTML"
    )
    await c.answer()

# ===== RATING =====
@dp.callback_query(F.data == "rating")
async def rating(c: CallbackQuery):
    lang = get_lang(c.from_user.id)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, wins FROM users ORDER BY wins DESC, games DESC, user_id ASC LIMIT 10")
        rows = cur.fetchall()
    lines = [TEXT[lang]["rating_text"]]
    if not rows:
        lines.append("\nӘзірге бос.")
    else:
        for pos, (uid, wins) in enumerate(rows, 1):
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
    await c.message.edit_text("".join(lines), reply_markup=back_kb(lang), parse_mode="HTML")
    await c.answer()

# ===== REFERRALS =====
@dp.callback_query(F.data == "referrals")
async def referrals(c: CallbackQuery):
    lang = get_lang(c.from_user.id)
    count, link = await get_referral_data(c.from_user.id)
    await c.message.edit_text(
        TEXT[lang]["referral_text"].format(count, link),
        reply_markup=back_kb(lang), parse_mode="HTML", disable_web_page_preview=True
    )
    await c.answer()

# ===== LANGUAGE =====
@dp.callback_query(F.data == "language")
async def language(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for code, name in [("kz", "Қазақша"), ("ru", "Русский"), ("en", "English")]:
        kb.button(text=name, callback_data=f"setlang_{code}", icon_custom_emoji_id=EMOJI_IDS["info"])
    kb.adjust(1)
    await c.message.edit_text(TEXT[get_lang(c.from_user.id)]["language"], reply_markup=kb.as_markup(), parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("setlang_"))
async def set_language(c: CallbackQuery):
    lang = c.data.split("_", 1)[1]
    if lang not in TEXT:
        await c.answer("Тіл табылмады.", show_alert=True)
        return
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET lang=%s WHERE user_id=%s", (lang, c.from_user.id))
    u = get_user(c.from_user.id)
    await c.message.edit_text(
        TEXT[lang]["welcome"].format(float(u["balance"])),
        reply_markup=main_kb(c.from_user.id, lang), parse_mode="HTML"
    )
    await c.answer()

# ===== PLAY =====
@dp.callback_query(F.data == "play")
async def play(c: CallbackQuery):
    t = TEXT[get_lang(c.from_user.id)]
    kb = InlineKeyboardBuilder()
    kb.button(text=t["lobby_create"], callback_data="lobby_create", icon_custom_emoji_id=EMOJI_IDS["join"])
    kb.button(text=t["lobby_search"], callback_data="lobby_search", icon_custom_emoji_id=EMOJI_IDS["search"])
    kb.button(text=t["back"], callback_data="home", icon_custom_emoji_id=EMOJI_IDS["back"])
    kb.adjust(1)
    await c.message.edit_text(t["lobby_menu"], reply_markup=kb.as_markup(), parse_mode="HTML")
    await c.answer()

# ===== LOBBY CREATE =====
@dp.callback_query(F.data == "lobby_create")
async def lobby_create(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT[lang]["dice"], callback_data="lc_dice", icon_custom_emoji_id=EMOJI_IDS["dice"])
    kb.button(text=TEXT[lang]["darts"], callback_data="lc_darts", icon_custom_emoji_id=EMOJI_IDS["darts"])
    kb.button(text=TEXT[lang]["back"], callback_data="play", icon_custom_emoji_id=EMOJI_IDS["back"])
    kb.adjust(1)
    await c.message.edit_text(TEXT[lang]["choose"], reply_markup=kb.as_markup(), parse_mode="HTML")
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
    await state.update_data(bet_amount=str(amount))
    await state.set_state(AmountState.lobby_mode)

    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT[lang]["mode_bot"], callback_data="mode_bot", icon_custom_emoji_id=EMOJI_IDS["bot"])
    kb.button(text=TEXT[lang]["mode_players"], callback_data="mode_players", icon_custom_emoji_id=EMOJI_IDS["join"])
    kb.button(text=TEXT[lang]["back"], callback_data="home", icon_custom_emoji_id=EMOJI_IDS["back"])
    kb.adjust(1)
    await message.answer(TEXT[lang]["lobby_mode"], reply_markup=kb.as_markup(), parse_mode="HTML")

# ===== BOT DUEL =====
@dp.callback_query(F.data == "mode_bot")
async def start_bot_duel(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id)
    data = await state.get_data()
    game = data.get("game")
    bet = Decimal(data.get("bet_amount"))
    uid = c.from_user.id
    chat_id = c.message.chat.id

    u = get_user(uid)
    if Decimal(u["balance"]) < bet:
        await c.answer(TEXT[lang]["no_money"], show_alert=True)
        await state.clear()
        return

    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
                    (bet, uid, bet))
        if cur.rowcount != 1:
            await c.answer("Қате", show_alert=True)
            await state.clear()
            return

    await state.set_state(AmountState.duel_wait_dice)
    await state.update_data(bet_amount=str(bet), game=game)

    await c.message.edit_text(TEXT[lang]["bot_waiting"])

    dice_symbol = "🎲" if game == "dice" else "🎯"
    await asyncio.sleep(1)
    bot_dice = await bot.send_dice(chat_id, emoji=dice_symbol)
    await asyncio.sleep(4)

    await state.update_data(bot_dice_val=bot_dice.dice.value)
    await c.message.answer(TEXT[lang]["bot_your_turn"])
    await c.answer()

@dp.message(AmountState.duel_wait_dice, F.dice)
async def duel_user_roll(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    data = await state.get_data()

    if message.forward_from or message.forward_from_chat or message.forward_sender_name or message.forward_origin:
        await message.answer(TEXT[lang]["bot_invalid"])
        return

    game = data.get("game")
    expected_emoji = "🎲" if game == "dice" else "🎯"
    if message.dice.emoji != expected_emoji:
        await message.answer(TEXT[lang]["bot_invalid"])
        return

    bet = Decimal(data.get("bet_amount"))
    bot_val = data.get("bot_dice_val", 0)
    user_val = message.dice.value
    uid = message.from_user.id

    with db() as conn, conn.cursor() as cur:
        if bot_val == user_val:
            cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (bet, uid))
            result = TEXT[lang]["bot_draw"]
        elif user_val > bot_val:
            prize = (bet * 2 * Decimal("0.95")).quantize(Decimal("0.01"))
            cur.execute("UPDATE users SET balance=balance+%s, wins=wins+1, games=games+1 WHERE user_id=%s",
                        (prize, uid))
            result = TEXT[lang]["bot_win"].format(prize)
        else:
            cur.execute("UPDATE users SET losses=losses+1, games=games+1 WHERE user_id=%s", (uid,))
            result = TEXT[lang]["bot_lose"]

    await message.answer(result, parse_mode="HTML")
    await state.clear()

# ===== PLAYER DUEL =====
@dp.callback_query(F.data == "mode_players")
async def start_player_duel(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id)
    data = await state.get_data()
    game = data.get("game")
    amount = Decimal(data.get("bet_amount"))
    lobby_id = uuid.uuid4().hex[:6].upper()

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lobbies(lobby_id,host_id,chat_id,game_type,bet_amount) VALUES(%s,%s,%s,%s,%s)",
            (lobby_id, c.from_user.id, c.message.chat.id, game, amount)
        )
        cur.execute("INSERT INTO lobby_players(lobby_id,user_id) VALUES(%s,%s)",
                    (lobby_id, c.from_user.id))

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=lobby_{lobby_id}"
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT[lang]["lobby_join_btn"], url=link, icon_custom_emoji_id=EMOJI_IDS["join"])
    kb.button(text=TEXT[lang]["back"], callback_data="home", icon_custom_emoji_id=EMOJI_IDS["back"])
    kb.adjust(1)

    await c.message.edit_text(
        TEXT[lang]["lobby_created"].format(lobby_id, amount, TEXT[lang][game], link),
        reply_markup=kb.as_markup(), parse_mode="HTML", disable_web_page_preview=True
    )
    await state.clear()
    await c.answer()

# ===== LOBBY SEARCH =====
@dp.callback_query(F.data == "lobby_search")
async def lobby_search(c: CallbackQuery):
    lang = get_lang(c.from_user.id)
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT lobby_id,game_type,bet_amount FROM lobbies WHERE status='waiting' AND host_id != %s ORDER BY created_at DESC LIMIT 20",
                (c.from_user.id,)
            )
            rows = cur.fetchall()

        if not rows:
            await c.message.edit_text(
                TEXT[lang]["lobby_search_empty"],
                reply_markup=back_kb(lang, "play"), parse_mode="HTML"
            )
            await c.answer()
            return

        kb = InlineKeyboardBuilder()
        for lid, gt, bet in rows:
            label = f"{'🎲' if gt=='dice' else '🎯'} ${bet} — {lid}"
            kb.button(text=label, callback_data=f"lj_{lid}")
        kb.button(text=TEXT[lang]["back"], callback_data="play", icon_custom_emoji_id=EMOJI_IDS["back"])
        kb.adjust(1)

        await c.message.edit_text(
            TEXT[lang]["lobby_search_title"],
            reply_markup=kb.as_markup(), parse_mode="HTML"
        )
    except Exception as ex:
        print("Lobby Search Error:", ex)
    await c.answer()

async def try_join_lobby(target, uid, lobby_id):
    u = get_user(uid)
    lang = u["lang"]
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM lobbies WHERE lobby_id=%s AND status='waiting'", (lobby_id,))
        lobby = cur.fetchone()
        if not lobby:
            await target.answer(TEXT[lang]["lobby_not_found"])
            return
        cur.execute("SELECT user_id FROM lobby_players WHERE lobby_id=%s", (lobby_id,))
        players = [r[0] for r in cur.fetchall()]
        bet = Decimal(lobby["bet_amount"])

        if uid in players or len(players) >= 2:
            await target.answer(TEXT[lang]["lobby_not_found"])
            return
        if Decimal(u["balance"]) < bet:
            await target.answer(TEXT[lang]["no_money"])
            return

        cur.execute("INSERT INTO lobby_players(lobby_id,user_id) VALUES(%s,%s)", (lobby_id, uid))
        players.append(uid)
        cur.execute("UPDATE lobbies SET status='playing' WHERE lobby_id=%s", (lobby_id,))

    await target.answer(TEXT[lang]["lobby_joined"])
    await run_duel(lobby_id, players, lobby["game_type"], bet, lobby["chat_id"])

@dp.callback_query(F.data.startswith("lj_"))
async def lobby_join_by_btn(c: CallbackQuery):
    await try_join_lobby(c.message, c.from_user.id, c.data.removeprefix("lj_"))
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
    # Атомды шегеру — rollback бар
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
                (bet, p1, bet)
            )
            if cur.rowcount != 1:
                raise ValueError("P1 insufficient")
            cur.execute(
                "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
                (bet, p2, bet)
            )
            if cur.rowcount != 1:
                raise ValueError("P2 insufficient")
    except ValueError:
        with db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lobbies SET status='waiting' WHERE lobby_id=%s", (lobby_id,))
        await bot.send_message(chat_id, "Баланс жеткіліксіз. Ойын тоқтатылды.")
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
                "UPDATE users SET balance=balance+%s,wins=wins+1,games=games+1 WHERE user_id=%s",
                (prize, winner)
            )
            cur.execute(
                "UPDATE users SET losses=losses+1,games=games+1 WHERE user_id=%s",
                (loser,)
            )
            result = f"🏆 Жеңімпаз: <code>{winner}</code>\n💰 Ұтыс: ${prize}"
        cur.execute("DELETE FROM lobbies WHERE lobby_id=%s", (lobby_id,))

    await bot.send_message(chat_id, result, parse_mode="HTML")

# ===== DEPOSIT =====
@dp.callback_query(F.data == "deposit")
async def deposit_start(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id)
    await state.set_state(AmountState.deposit)
    await c.message.edit_text(TEXT[lang]["deposit_amount"], parse_mode="HTML")
    await c.answer()

@dp.message(AmountState.deposit)
async def make_invoice(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    try:
        amount = money(message.text.replace(",", "."))
        if amount < Decimal("1.00"):
            raise ValueError
    except Exception:
        await message.answer(TEXT[lang]["bad_amount"])
        return

    order_id = f"dg_{message.from_user.id}_{uuid.uuid4().hex}"
    invoice = await lp.invoices.create(
        str(amount), order_id=order_id,
        description=f"Duel Games deposit user {message.from_user.id}"
    )

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO payments(order_id,user_id,amount) VALUES(%s,%s,%s)",
            (order_id, message.from_user.id, amount)
        )

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=TEXT[lang]["pay"], url=invoice.pay_url,
                                icon_custom_emoji_id=EMOJI_IDS["crypto"]))
    await message.answer(TEXT[lang]["created"].format(amount),
                         reply_markup=kb.as_markup(), parse_mode="HTML")
    await state.clear()

@lp.invoice_paid()
async def on_invoice_paid(invoice):
    order_id = str(invoice.order_id)
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM payments WHERE order_id=%s FOR UPDATE", (order_id,))
        payment = cur.fetchone()
        if not payment or payment["status"] == "paid":
            return
        if Decimal(str(invoice.amount)) != Decimal(str(payment["amount"])):
            return
        cur.execute("UPDATE payments SET status='paid',paid_at=NOW() WHERE order_id=%s AND status='pending'",
                    (order_id,))
        if cur.rowcount != 1:
            return
        cur.execute("UPDATE users SET balance=balance+%s,has_deposited=TRUE WHERE user_id=%s",
                    (payment["amount"], payment["user_id"]))
        cur.execute("SELECT balance,lang FROM users WHERE user_id=%s", (payment["user_id"],))
        user = cur.fetchone()
    try:
        await bot.send_message(payment["user_id"],
            TEXT[user["lang"]]["paid"].format(float(user["balance"])), parse_mode="HTML")
    except Exception as exc:
        print("Telegram notify error:", repr(exc))

# ===== WITHDRAW — АВТО-ШЫҒАРУ (3 желі) =====
@dp.callback_query(F.data == "withdraw")
async def withdraw_start(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id)
    await state.set_state(AmountState.withdraw)
    await c.message.edit_text(TEXT[lang]["withdraw_amount"], parse_mode="HTML")
    await c.answer()

@dp.message(AmountState.withdraw)
async def withdraw_amount(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    try:
        amount = money(message.text.replace(",", "."))
        if amount < Decimal("1.00"):
            raise ValueError
    except Exception:
        await message.answer(TEXT[lang]["bad_amount"])
        return

    u = get_user(message.from_user.id)
    if Decimal(u["balance"]) < amount:
        await message.answer(TEXT[lang]["no_money"])
        await state.clear()
        return

    await state.update_data(withdraw_amount=str(amount))
    await state.set_state(AmountState.address)
    await message.answer(TEXT[lang]["withdraw_address"], parse_mode="HTML")

@dp.message(AmountState.address)
async def withdraw_address(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    address = message.text.strip()

    network = detect_network(address)
    if not network:
        await message.answer(TEXT[lang]["bad_address"])
        return

    if network not in ("TRX", "BSC", "GRAM"):
        await message.answer(f"❌ {network} қолдау көрсетілмейді. TRC20, BEP20 немесе TON қолданыңыз.")
        return

    data = await state.get_data()
    amount = Decimal(data["withdraw_amount"])
    uid = message.from_user.id

    info = await get_network_info("USDT", network)
    if not info:
        await message.answer("⚠️ Бұл желі қазір қолжетімсіз. Кейінірек қайталаңыз.")
        await state.clear()
        return

    fee = Decimal(info.get("fee", "0"))
    min_amt = Decimal(info.get("min_amount", "0"))
    if amount < min_amt:
        await message.answer(f"❌ Минимум: {min_amt} USDT ({network})")
        return

    total = amount + fee

    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s RETURNING balance",
                    (total, uid, total))
        if not cur.fetchone():
            await message.answer(TEXT[lang]["no_money"])
            await state.clear()
            return
        cur.execute("INSERT INTO withdrawals(user_id, amount, address, network, status) VALUES(%s,%s,%s,%s,'pending') RETURNING id",
                    (uid, amount, address, network))
        wid = cur.fetchone()[0]

    await message.answer(TEXT[lang]["withdraw_sent"])

    idem_key = f"wd-{wid}-{uid}"
    result = await lavepay_withdraw("USDT", network, address, amount, idem_key)

    with db() as conn, conn.cursor() as cur:
        if result.get("ok"):
            wd = result["data"]
            status = wd.get("status", "processing")
            tx = wd.get("tx_hash")
            cur.execute("UPDATE withdrawals SET status=%s, tx_id=%s, processed_at=NOW() WHERE id=%s",
                        (status if status != "failed" else "failed", tx, wid))
            if status == "failed":
                cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (total, uid))
                await message.answer(TEXT[lang]["withdraw_failed"].format(wid=wid))
            else:
                await message.answer(
                    TEXT[lang]["withdraw_success"].format(
                        wid=wid, amount=amount, network=network, tx=tx or "өңделуде"
                    ), parse_mode="HTML"
                )
        else:
            err = result.get("error", {})
            cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (total, uid))
            cur.execute("UPDATE withdrawals SET status='failed' WHERE id=%s", (wid,))
            await message.answer(f"❌ {err.get('message', 'Қате')}\nБаланс қайтарылды.")
            if ADMIN_ID:
                await bot.send_message(ADMIN_ID, f"⚠️ Вывод #{wid} қате: {err}")

    await state.clear()

# ===== МУЛЬТИЧЕК — БӨЛІК 3-ТЕ =====
# ===== БӨЛІК 2 СОҢЫ =====
# ===== БӨЛІК 3 БАСЫ =====
# Бұл бөлікті БӨЛІК 2-нің соңына қосасыз

# ===== МУЛЬТИЧЕК — АҚША ШЕГЕРУ ТҮЗЕТІЛДІ =====
@dp.callback_query(F.data == "check_menu")
async def check_menu(c: CallbackQuery):
    lang = get_lang(c.from_user.id)
    kb = InlineKeyboardBuilder()
    if is_admin(c.from_user.id):
        kb.button(text=TEXT[lang]["check_create_btn"], callback_data="check_create",
                  icon_custom_emoji_id=EMOJI_IDS["gift"])
    kb.button(text=TEXT[lang]["check_get_btn"], callback_data="check_get",
              icon_custom_emoji_id=EMOJI_IDS["join"])
    kb.button(text=TEXT[lang]["back"], callback_data="home",
              icon_custom_emoji_id=EMOJI_IDS["back"])
    kb.adjust(1)
    await c.message.edit_text(TEXT[lang]["check_menu"], reply_markup=kb.as_markup(), parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data == "check_create")
async def check_create(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    lang = get_lang(c.from_user.id)
    await state.set_state(AmountState.check_create_amount)
    await c.message.edit_text(TEXT[lang]["check_amount"], parse_mode="HTML")
    await c.answer()

@dp.message(AmountState.check_create_amount)
async def check_create_amount(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    try:
        amount = money(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except Exception:
        await message.answer(TEXT[lang]["bad_amount"])
        return
    await state.update_data(total_amount=str(amount))
    await state.set_state(AmountState.check_create_count)
    await message.answer(TEXT[lang]["check_count"])

@dp.message(AmountState.check_create_count)
async def check_create_count(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    data = await state.get_data()
    try:
        count = int(message.text)
        if count <= 0:
            raise ValueError
    except Exception:
        await message.answer("Санды дұрыс енгізіңіз.")
        return

    total_amount = Decimal(data["total_amount"])
    per_user = (total_amount / count).quantize(Decimal("0.01"))
    check_id = uuid.uuid4().hex[:8].upper()
    uid = message.from_user.id

    # АҚША ШЕГЕРУ — атомды
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s RETURNING balance",
                    (total_amount, uid, total_amount))
        if not cur.fetchone():
            await message.answer(TEXT[lang]["no_money"])
            await state.clear()
            return
        cur.execute("INSERT INTO multi_checks(check_id, amount, max_claims, creator_id) VALUES(%s,%s,%s,%s)",
                    (check_id, total_amount, count, uid))

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=check_{check_id}"
    await message.answer(
        TEXT[lang]["check_created"].format(total_amount, count, per_user, link),
        parse_mode="HTML", disable_web_page_preview=True
    )
    await state.clear()

@dp.callback_query(F.data == "check_get")
async def check_get_prompt(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id)
    await state.set_state(AmountState.check_get)
    await c.message.edit_text(TEXT[lang]["check_get_prompt"], parse_mode="HTML")
    await c.answer()

@dp.message(AmountState.check_get)
async def check_get_code(message: Message, state: FSMContext):
    text = message.text.strip()
    if "start=check_" in text:
        try:
            check_id = text.split("start=check_")[1].split()[0].upper()
        except IndexError:
            await message.answer("Код табылмады.")
            return
    else:
        check_id = text.upper()
    await claim_check(message, message.from_user.id, check_id)
    await state.clear()

async def claim_check(target, uid, check_id):
    lang = get_lang(uid)
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM multi_checks WHERE check_id=%s FOR UPDATE", (check_id,))
        check = cur.fetchone()
        if not check or check["status"] != "active":
            return await target.answer(TEXT[lang]["check_not_found"])
        if check["claimed"] >= check["max_claims"]:
            return await target.answer(TEXT[lang]["check_empty"])
        cur.execute("SELECT 1 FROM check_claims WHERE check_id=%s AND user_id=%s", (check_id, uid))
        if cur.fetchone():
            return await target.answer(TEXT[lang]["check_already"])

        per_user = (Decimal(check["amount"]) / check["max_claims"]).quantize(Decimal("0.01"))
        cur.execute("INSERT INTO check_claims(check_id, user_id) VALUES(%s,%s)", (check_id, uid))
        cur.execute("UPDATE multi_checks SET claimed = claimed + 1 WHERE check_id=%s", (check_id,))
        if check["claimed"] + 1 >= check["max_claims"]:
            cur.execute("UPDATE multi_checks SET status='finished' WHERE check_id=%s", (check_id,))
        cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (per_user, uid))
        cur.execute("SELECT balance FROM users WHERE user_id=%s", (uid,))
        new_bal = cur.fetchone()[0]

    await target.answer(TEXT[lang]["check_success"].format(per_user, new_bal), parse_mode="HTML")

# ===== АДМИН ПАНЕЛЬ =====
@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer(TEXT[get_lang(message.from_user.id)]["admin_only"])
    await admin_show_home(message, edit=False)

@dp.callback_query(F.data == "admin_home")
async def admin_home_cb(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔ Тек әкімшіге", show_alert=True)
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
    kb.button(text="💳 Баланс беру", callback_data="adm_give_balance")
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

@dp.callback_query(F.data == "adm_give_balance")
async def adm_give_balance(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    lang = get_lang(c.from_user.id)
    await state.set_state(AmountState.admin_give)
    await c.message.edit_text(TEXT[lang]["admin_give_prompt"], parse_mode="HTML")
    await c.answer()

@dp.message(AmountState.admin_give)
async def admin_give_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    lang = get_lang(message.from_user.id)
    try:
        parts = message.text.split()
        target_id = int(parts[0])
        amount = money(parts[1].replace(",", "."))
        if amount <= 0:
            raise ValueError
    except Exception:
        return await message.answer("Қате формат. Мысалы: 12345678 5.5")

    get_user(target_id)
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, target_id))
    await message.answer(TEXT[lang]["admin_give_success"])
    try:
        await bot.send_message(target_id, f"🎁 Сізге әкімшіден ${amount} баланс қосылды!")
    except Exception:
        pass
    await state.clear()

@dp.callback_query(F.data == "adm_withdrawals")
async def adm_withdrawals(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id,user_id,amount,address,network FROM withdrawals WHERE status='pending' ORDER BY id")
        rows = cur.fetchall()
    if not rows:
        return await c.message.edit_text("✅ Күтіп тұрған шығару жоқ.", reply_markup=back_kb("kz", "admin_home"))

    text = "💸 <b>Күтіп тұрған шығарулар</b>\n\n"
    kb = InlineKeyboardBuilder()
    for wid, uid, amt, addr, net in rows[:10]:
        text += f"#{wid} | <code>{uid}</code> | <b>${amt}</b> | {net}\n<code>{addr}</code>\n\n"
        kb.button(text=f"✅ #{wid}", callback_data=f"wapprove_{wid}")
        kb.button(text=f"❌ #{wid}", callback_data=f"wreject_{wid}")
    kb.button(text="⬅️ Артқа", callback_data="admin_home")
    kb.adjust(2, 2, 2, 2, 2, 1)
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("wapprove_"))
async def wapprove(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    wid = int(c.data.split("_")[1])
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE withdrawals SET status='approved', processed_at=NOW() WHERE id=%s AND status='pending' RETURNING user_id,amount",
                    (wid,))
        row = cur.fetchone()
    if not row:
        return await c.answer("Өңделген", show_alert=True)
    await bot.send_message(row[0], f"✅ Шығару #{wid} расталды: ${row[1]}")
    await c.message.edit_text(f"✅ #{wid} approved", parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("wreject_"))
async def wreject(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    wid = int(c.data.split("_")[1])
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE withdrawals SET status='rejected' WHERE id=%s AND status='pending' RETURNING user_id,amount,network",
                    (wid,))
        row = cur.fetchone()
        if not row:
            return await c.answer("Өңделген", show_alert=True)
        # Қайтару — fee-мен бірге
        info = await get_network_info("USDT", row[2])
        fee = Decimal(info["fee"]) if info else Decimal("0")
        cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (row[1] + fee, row[0]))
    await bot.send_message(row[0], f"❌ Шығару #{wid} қабылданбады. ${row[1]} қайтарылды.")
    await c.message.edit_text(f"❌ #{wid} rejected", parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data == "adm_lobbies")
async def adm_lobbies(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT lobby_id,host_id,game_type,bet_amount,status FROM lobbies ORDER BY created_at DESC LIMIT 20")
        rows = cur.fetchall()
    if not rows:
        text = "🎮 Лобби жоқ."
    else:
        text = "🎮 <b>Лоббилер</b>\n\n"
        for lid, hid, gt, bet, st in rows:
            text += f"<code>{lid}</code> | {gt} | ${bet} | {st} | host {hid}\n"
    await c.message.edit_text(text, reply_markup=back_kb("kz", "admin_home"), parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data == "adm_users")
async def adm_users(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*),COALESCE(SUM(balance),0) FROM users")
        users, total = cur.fetchone()
    await c.message.edit_text(
        f"👥 Юзерлер: <b>{users}</b>\n💰 Жалпы баланс: <b>${total}</b>",
        reply_markup=back_kb("kz", "admin_home"), parse_mode="HTML"
    )
    await c.answer()

# ===== ВЕБХУК ШЫҒАРУ НӘТИЖЕСІ =====
@lp.webhook("withdrawal.completed")
async def on_withdrawal_completed(event):
    data = event.data
    idem = data.get("idempotency_key", "")
    try:
        wid = int(idem.split("-")[1])
    except Exception:
        return
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE withdrawals SET status='approved', tx_id=%s, processed_at=NOW() WHERE id=%s RETURNING user_id, amount",
                    (data.get("tx_hash"), wid))
        row = cur.fetchone()
    if row:
        try:
            await bot.send_message(row[0], f"✅ Шығару #{wid}: ${row[1]} USDT жіберілді.")
        except Exception:
            pass

@lp.webhook("withdrawal.failed")
async def on_withdrawal_failed(event):
    data = event.data
    idem = data.get("idempotency_key", "")
    try:
        wid = int(idem.split("-")[1])
    except Exception:
        return
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE withdrawals SET status='failed' WHERE id=%s RETURNING user_id, amount, network", (wid,))
        row = cur.fetchone()
        if row:
            info = await get_network_info("USDT", row[2])
            fee = Decimal(info["fee"]) if info else Decimal("0")
            cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (row[1] + fee, row[0]))
    if row:
        try:
            await bot.send_message(row[0], f"❌ Шығару #{wid} сәтсіз. Баланс қайтарылды.")
        except Exception:
            pass

# ===== FASTAPI =====
@app.get("/")
async def health():
    return {"status": "ok", "service": "Duel Games", "webhook": "/lavepay"}

install_lavepay(app, lp)

@app.on_event("startup")
async def startup():
    init_db()
    app.state.bot_task = asyncio.create_task(dp.start_polling(bot))
    print("Duel Games started; LavePay webhook: /lavepay")

@app.on_event("shutdown")
async def shutdown():
    task = getattr(app.state, "bot_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await bot.session.close()
    if _pool:
        _pool.closeall()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

# ===== БӨЛІК 3 СОҢЫ =====
