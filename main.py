import asyncio
import os
import random
import uuid
from decimal import Decimal
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from lavepay import LavePay

# ================= БАПТАУЛАР =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
LAVEPAY_API_KEY = os.getenv("LAVEPAY_API_KEY")
LAVEPAY_WEBHOOK_SECRET = os.getenv("LAVEPAY_WEBHOOK_SECRET")
DB_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789")) # Өз ID-іңізді осында қойыңыз!

if DB_URL and DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# ================= СІЗ ЖІБЕРГЕН ПРЕМИУМ ЭМОДЗИ ID-ЛЕРІ =================
EMOJI_WALLET = "5420271804411292547"
EMOJI_BALANCE = "5992085874546844207"
EMOJI_CRYPTO = "5940552778165264297"
EMOJI_PLAY = "5766994197705921104"
EMOJI_DICE = "5402282535755461614"
EMOJI_DARTS = "5400331881868698012"
EMOJI_PROFILE = "6032693626394382504"
EMOJI_VIP = "5251422397893989847"
EMOJI_WIN = "5449683594425410231"
EMOJI_LOSE = "5447183459602669338"
EMOJI_BACK = "5240241223632954241"
EMOJI_INFO = "5274099962655816924"
EMOJI_USDT = "5409048419211682843"
EMOJI_RUB = "5231449120635370684"
EMOJI_KZT = "5364045318092504570"
EMOJI_ID = "5291913773307667021"
EMOJI_GAMES = "5282843764451195532"
EMOJI_ADMIN = "5274099962655816924" 
EMOJI_STATS = "5282843764451195532" 
EMOJI_ANON = "5251422397893989847" 

COMMISSION_PERCENT = Decimal("0.05")
REFERRAL_PERCENT = Decimal("0.10")

# ================= БОТ =================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
lp = LavePay(LAVEPAY_API_KEY, webhook_secret=LAVEPAY_WEBHOOK_SECRET)

# ================= ДЕРЕКҚОР =================
def get_db():
    return psycopg2.connect(DB_URL)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    balance REAL DEFAULT 0.0,
                    lang TEXT DEFAULT 'kz',
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    games INTEGER DEFAULT 0,
                    is_vip INTEGER DEFAULT 0,
                    is_anonymous INTEGER DEFAULT 0,
                    total_bet REAL DEFAULT 0.0,
                    last_bet_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    referrer_id BIGINT,
                    has_deposited INTEGER DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lobbies (
                    lobby_id TEXT PRIMARY KEY,
                    host_id BIGINT,
                    chat_id BIGINT,
                    game_type TEXT,
                    bet_amount REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'waiting'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lobby_players (
                    lobby_id TEXT REFERENCES lobbies(lobby_id) ON DELETE CASCADE,
                    user_id BIGINT,
                    PRIMARY KEY (lobby_id, user_id)
                )
            """)
            conn.commit()

def get_user(user_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
                conn.commit()
                cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
            return row

def update_balance(user_id, amount):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
            conn.commit()

def update_deposit_status(user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET has_deposited = 1 WHERE user_id = %s", (user_id,))
            conn.commit()

def set_lang(user_id, lang):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET lang = %s WHERE user_id = %s", (lang, user_id))
            conn.commit()

def toggle_vip(user_id, status):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_vip = %s WHERE user_id = %s", (status, user_id))
            conn.commit()

def toggle_anon(user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_anonymous = CASE WHEN is_anonymous = 1 THEN 0 ELSE 1 END WHERE user_id = %s", (user_id,))
            conn.commit()

def update_profile(user_id, win: bool, bet_amount: float):
    with get_db() as conn:
        with conn.cursor() as cur:
            if win:
                cur.execute("UPDATE users SET wins = wins + 1, games = games + 1, total_bet = total_bet + %s, last_bet_time = CURRENT_TIMESTAMP WHERE user_id = %s", (bet_amount, user_id))
            else:
                cur.execute("UPDATE users SET losses = losses + 1, games = games + 1, total_bet = total_bet + %s, last_bet_time = CURRENT_TIMESTAMP WHERE user_id = %s", (bet_amount, user_id))
            conn.commit()

def get_top_users(period: str):
    if period == 'hour': time_cond = "last_bet_time >= NOW() - INTERVAL '1 hour'"
    elif period == 'day': time_cond = "last_bet_time >= NOW() - INTERVAL '1 day'"
    elif period == 'week': time_cond = "last_bet_time >= NOW() - INTERVAL '7 days'"
    elif period == 'month': time_cond = "last_bet_time >= NOW() - INTERVAL '30 days'"
    else: time_cond = "1=1"
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(f"SELECT user_id, total_bet, is_anonymous FROM users WHERE {time_cond} ORDER BY total_bet DESC LIMIT 10")
            return cur.fetchall()

# ================= FSM =================
class WithdrawState(StatesGroup):
    amount = State()
    address = State()

class LobbyState(StatesGroup):
    bet_amount = State()

class AdminState(StatesGroup):
    user_id = State()
    amount = State()
    vip_action = State()

# ================= ТІЛДЕР =================
TEXTS = {
    "kz": {
        "welcome": f'Сәлем! <b>Duel Games</b>!\n\n<tg-emoji emoji-id="{EMOJI_BALANCE}">💵</tg-emoji> Балансыңыз: <b>${{:.2f}}</b>\n\nНе істейміз?',
        "btn_play": "Ойын ойнау",
        "btn_profile": "Профиль",
        "btn_balance": "Баланс",
        "btn_lang": "Тіл",
        "btn_deposit": "Пополнить",
        "btn_withdraw": "Вывод",
        "btn_rating": "Рейтинг",
        "btn_admin": "Админ панель",
        "lang_select": f'<tg-emoji emoji-id="{EMOJI_INFO}">🌐</tg-emoji> Тілді таңдаңыз:',
        "profile_text": f'<tg-emoji emoji-id="{EMOJI_PROFILE}">👤</tg-emoji> <b>Профиль</b>\n\n<tg-emoji emoji-id="{EMOJI_ID}">🆔</tg-emoji> ID: <code>{{}}</code>\n<tg-emoji emoji-id="{EMOJI_WIN}">🏆</tg-emoji> Жеңістер: {{}}\n<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Жеңілістер: {{}}\n<tg-emoji emoji-id="{EMOJI_GAMES}">🎮</tg-emoji> Ойындар: {{}}\nVIP: {{}}\nАнонимділік: {{}}',
        "btn_toggle_anon": "Анонимділікті өзгерту",
        "btn_ref_link": "Реферал сілтеме",
        "ref_text": "Сіздің реферал сілтемеңіз:\n<code>https://t.me/{bot_username}?start=ref_{user_id}</code>\n\nШақырған адам пополнить етсе, сізге 10% тиеді.",
        "balance_text": f'<tg-emoji emoji-id="{EMOJI_BALANCE}">💰</tg-emoji> <b>Балансыңыз:</b>\n\n<tg-emoji emoji-id="{EMOJI_USDT}">💵</tg-emoji> USD: <b>${{:.2f}}</b>\n<tg-emoji emoji-id="{EMOJI_KZT}">🇰🇿</tg-emoji> KZT: <b>{{:.2f}} ₸</b>\n<tg-emoji emoji-id="{EMOJI_RUB}">🇷🇺</tg-emoji> RUB: <b>{{:.2f}} ₽</b>',
        "choose_game": f'<tg-emoji emoji-id="{EMOJI_PLAY}">🎮</tg-emoji> Қай ойынды ойнаймыз?',
        "btn_dice_lobby": "Кости Дуэль",
        "btn_darts_lobby": "Дартс Дуэль",
        "btn_bj_lobby": "Блекджек",
        "enter_bet": f'<tg-emoji emoji-id="{EMOJI_INFO}">💵</tg-emoji> <b>Ставканы енгізіңіз</b> (USD):',
        "lobby_created": f'<tg-emoji emoji-id="{EMOJI_WIN}">✅</tg-emoji> <b>Лобби құрылды!</b>\n\nЛобби ID: <code>{{}}</code>\nОйын: {{}}\nСтавка: <b>${{}}</b>\n\nДостарыңызды шақырыңыз:\n<code>https://t.me/{{bot_username}}?start=lobby_{{lobby_id}}</code>',
        "lobby_joined": f'<tg-emoji emoji-id="{EMOJI_WIN}">✅</tg-emoji> Сіз лоббиге қосылдыңыз! Лобби ID: <code>{{}}</code>\n\nОйын басталуын күтіңіз...',
        "lobby_not_found": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Мұндай лобби табылмады.',
        "lobby_full": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Лобби толы!',
        "lobby_started": f'<tg-emoji emoji-id="{EMOJI_PLAY}">🎮</tg-emoji> <b>Ойын басталды!</b>\n\nЛобби: {{}}',
        "no_money": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Балансыңыз жеткіліксіз!',
        "win_bet": f'<tg-emoji emoji-id="{EMOJI_WIN}">🏆</tg-emoji> <b>Сіз жеңдіңіз!</b>\n\nБанк: <b>${{}}</b>\nКомиссия: -${{}}\nҚолыңызға тиді: <b>+${{}}</b>',
        "lose_bet": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> <b>Сіз жеңілдіңіз.</b>\n\nСтавка: <b>-${{}}</b>',
        "draw": f'<tg-emoji emoji-id="{EMOJI_INFO}">🤝</tg-emoji> <b>Тең ойын!</b> Ставка қайтарылды.',
        "deposit_enter": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> <b>Пополнить</b>\n\nСоманы енгізіңіз (USD):',
        "deposit_invoice": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> <b>Төлем: ${{}}</b>\n\nТөлеу үшін басыңыз:',
        "deposit_paid": f'<tg-emoji emoji-id="{EMOJI_WIN}">✅</tg-emoji> <b>Төлем қабылданды!</b>\n\nБалансыңыз: <b>${{:.2f}}</b>',
        "withdraw_enter_amount": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💸</tg-emoji> <b>Вывод</b>\n\nБалансыңыз: <b>${{:.2f}}</b>\n\nЕң аз сома: $1.00\n\nШығарғыңыз келетін соманы енгізіңіз:',
        "withdraw_enter_address": f'<tg-emoji emoji-id="{EMOJI_INFO}">📍</tg-emoji> <b>Әмиян мекенжайын енгізіңіз</b> (USDT TRC20):',
        "withdraw_error_min": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Ең аз сома $1.00.',
        "withdraw_error_bal": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Балансыңыз жеткіліксіз!',
        "withdraw_error_addr": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Қате әмиян мекенжайы. Қайталап көріңіз.',
        "withdraw_success": f'<tg-emoji emoji-id="{EMOJI_WIN}">✅</tg-emoji> <b>Өтінім қабылданды!</b>\n\nСома: <b>${{}}</b> (комиссия шегерілді)\n\nАқша 5-10 минут ішінде келеді.',
        "withdraw_fail": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> <b>Қате!</b> Lave Pay API жауап бермеді.',
        "rating_menu": f'<tg-emoji emoji-id="{EMOJI_STATS}">📊</tg-emoji> <b>Рейтинг</b>\n\nҚай уақыт аралығын көргіңіз келеді?',
        "btn_hour": "За час",
        "btn_day": "За день",
        "btn_week": "За неделю",
        "btn_month": "За месяц",
        "rating_title": f'<tg-emoji emoji-id="{EMOJI_STATS}">📊</tg-emoji> <b>Рейтинг ({{}})</b>\n\n',
        "anon_msg": "Аноним",
        "admin_menu": f'<tg-emoji emoji-id="{EMOJI_ADMIN}">🛠</tg-emoji> <b>Админ панель</b>',
        "admin_ask_id": "Пайдаланушының ID-ін енгізіңіз:",
        "admin_ask_amount": "Соманы енгізіңіз (USD):",
        "admin_success": "✅ Сәтті орындалды!",
        "admin_vip": "VIP беру/алу",
        "admin_money": "Ақша беру",
        "group_help": "Топтық чатта ойнау үшін:\n/duel - Кости дуэлін бастау\n/darts - Дартс дуэлін бастау\n\nНемесе жеке чатта ойнаңыз.",
        "group_duel_start": "🎲 Кости дуэлі басталды! Ставка: ${}\nҚатысу үшін төмендегі батырманы басыңыз.",
        "group_join_btn": "Қатысу",
        "group_joined": "✅ {} қосылды!",
        "group_waiting": "Екінші ойыншы күтілуде...",
    },
    "ru": {
        "welcome": f'Привет! <b>Duel Games</b>!\n\n<tg-emoji emoji-id="{EMOJI_BALANCE}">💵</tg-emoji> Баланс: <b>${{:.2f}}</b>\n\nЧто делаем?',
        "btn_play": "Играть",
        "btn_profile": "Профиль",
        "btn_balance": "Баланс",
        "btn_lang": "Язык",
        "btn_deposit": "Пополнить",
        "btn_withdraw": "Вывод",
        "btn_rating": "Рейтинг",
        "btn_admin": "Админ панель",
        "lang_select": f'<tg-emoji emoji-id="{EMOJI_INFO}">🌐</tg-emoji> Выберите язык:',
        "profile_text": f'<tg-emoji emoji-id="{EMOJI_PROFILE}">👤</tg-emoji> <b>Профиль</b>\n\n<tg-emoji emoji-id="{EMOJI_ID}">🆔</tg-emoji> ID: <code>{{}}</code>\n<tg-emoji emoji-id="{EMOJI_WIN}">🏆</tg-emoji> Победы: {{}}\n<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Поражения: {{}}\n<tg-emoji emoji-id="{EMOJI_GAMES}">🎮</tg-emoji> Игры: {{}}\nVIP: {{}}\nАнонимность: {{}}',
        "btn_toggle_anon": "Изменить анонимность",
        "btn_ref_link": "Реферальная ссылка",
        "ref_text": "Ваша реферальная ссылка:\n<code>https://t.me/{bot_username}?start=ref_{user_id}</code>\n\nЕсли приглашенный пополнит баланс, вы получите 10%.",
        "balance_text": f'<tg-emoji emoji-id="{EMOJI_BALANCE}">💰</tg-emoji> <b>Ваш баланс:</b>\n\n<tg-emoji emoji-id="{EMOJI_USDT}">💵</tg-emoji> USD: <b>${{:.2f}}</b>\n<tg-emoji emoji-id="{EMOJI_KZT}">🇰🇿</tg-emoji> KZT: <b>{{:.2f}} ₸</b>\n<tg-emoji emoji-id="{EMOJI_RUB}">🇷🇺</tg-emoji> RUB: <b>{{:.2f}} ₽</b>',
        "choose_game": f'<tg-emoji emoji-id="{EMOJI_PLAY}">🎮</tg-emoji> Какую игру выберем?',
        "btn_dice_lobby": "Дуэль на костях",
        "btn_darts_lobby": "Дуэль в дартс",
        "btn_bj_lobby": "Блэкджек",
        "enter_bet": f'<tg-emoji emoji-id="{EMOJI_INFO}">💵</tg-emoji> <b>Введите ставку</b> (USD):',
        "lobby_created": f'<tg-emoji emoji-id="{EMOJI_WIN}">✅</tg-emoji> <b>Лобби создано!</b>\n\nID лобби: <code>{{}}</code>\nИгра: {{}}\nСтавка: <b>${{}}</b>\n\nОтправьте этот ID друзьям:\n<code>https://t.me/{{bot_username}}?start=lobby_{{lobby_id}}</code>',
        "lobby_joined": f'<tg-emoji emoji-id="{EMOJI_WIN}">✅</tg-emoji> Вы присоединились к лобби! ID: <code>{{}}</code>\n\nОжидайте начала игры...',
        "lobby_not_found": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Лобби не найдено.',
        "lobby_full": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Лобби заполнено!',
        "lobby_started": f'<tg-emoji emoji-id="{EMOJI_PLAY}">🎮</tg-emoji> <b>Игра началась!</b>\n\nЛобби: {{}}',
        "no_money": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Недостаточно средств!',
        "win_bet": f'<tg-emoji emoji-id="{EMOJI_WIN}">🏆</tg-emoji> <b>Вы победили!</b>\n\nБанк: <b>${{}}</b>\nКомиссия: -${{}}\nВаш выигрыш: <b>+${{}}</b>',
        "lose_bet": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> <b>Вы проиграли.</b>\n\nСтавка: <b>-${{}}</b>',
        "draw": f'<tg-emoji emoji-id="{EMOJI_INFO}">🤝</tg-emoji> <b>Ничья!</b> Ставка возвращена.',
        "deposit_enter": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> <b>Пополнение</b>\n\nВведите сумму (USD):',
        "deposit_invoice": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> <b>К оплате: ${{}}</b>',
        "deposit_paid": f'<tg-emoji emoji-id="{EMOJI_WIN}">✅</tg-emoji> <b>Платеж получен!</b>\n\nБаланс: <b>${{:.2f}}</b>',
        "withdraw_enter_amount": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💸</tg-emoji> <b>Вывод средств</b>\n\nБаланс: <b>${{:.2f}}</b>\n\nМин. сумма: $1.00\n\nВведите сумму для вывода:',
        "withdraw_enter_address": f'<tg-emoji emoji-id="{EMOJI_INFO}">📍</tg-emoji> <b>Введите адрес кошелька</b> (USDT TRC20):',
        "withdraw_error_min": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Минимальная сумма $1.00.',
        "withdraw_error_bal": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Недостаточно средств!',
        "withdraw_error_addr": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> Неверный адрес кошелька. Попробуйте снова.',
        "withdraw_success": f'<tg-emoji emoji-id="{EMOJI_WIN}">✅</tg-emoji> <b>Заявка принята!</b>\n\nСумма: <b>${{}}</b> (с учетом комиссии)\n\nСредства придут в течение 5-10 минут.',
        "withdraw_fail": f'<tg-emoji emoji-id="{EMOJI_LOSE}">❌</tg-emoji> <b>Ошибка!</b> Lave Pay API не ответил.',
        "rating_menu": f'<tg-emoji emoji-id="{EMOJI_STATS}">📊</tg-emoji> <b>Рейтинг</b>\n\nКакой период вас интересует?',
        "btn_hour": "За час",
        "btn_day": "За день",
        "btn_week": "За неделю",
        "btn_month": "За месяц",
        "rating_title": f'<tg-emoji emoji-id="{EMOJI_STATS}">📊</tg-emoji> <b>Рейтинг ({{}})</b>\n\n',
        "anon_msg": "Аноним",
        "admin_menu": f'<tg-emoji emoji-id="{EMOJI_ADMIN}">🛠</tg-emoji> <b>Админ панель</b>',
        "admin_ask_id": "Введите ID пользователя:",
        "admin_ask_amount": "Введите сумму (USD):",
        "admin_success": "✅ Успешно выполнено!",
        "admin_vip": "Выдать/Забрать VIP",
        "admin_money": "Выдать деньги",
        "group_help": "Для игры в группе:\n/duel - Начать дуэль на костях\n/darts - Начать дуэль в дартс\n\nИли играйте в личных сообщениях.",
        "group_duel_start": "🎲 Дуэль на костях началась! Ставка: ${}\nНажмите кнопку ниже, чтобы участвовать.",
        "group_join_btn": "Участвовать",
        "group_joined": "✅ {} присоединился!",
        "group_waiting": "Ожидание второго игрока...",
    },
    "en": {
        # ... (English translations similar to above)
        "group_help": "To play in a group:\n/duel - Start dice duel\n/darts - Start darts duel\n\nOr play in private messages.",
        "group_duel_start": "🎲 Dice duel started! Bet: ${}\nClick the button below to join.",
        "group_join_btn": "Join",
        "group_joined": "✅ {} joined!",
        "group_waiting": "Waiting for the second player...",
    }
}

# ================= КЛАВИАТУРАЛАР =================
def lang_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🇰🇿 Қазақша", callback_data="lang_kz")
    kb.button(text="🇷🇺 Русский", callback_data="lang_ru")
    kb.button(text="🇬🇧 English", callback_data="lang_en")
    kb.adjust(1)
    return kb.as_markup()

def main_menu_kb(lang, is_admin=False):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXTS[lang]["btn_play"], callback_data="play_menu", icon_custom_emoji_id=EMOJI_PLAY)
    kb.button(text=TEXTS[lang]["btn_profile"], callback_data="profile", icon_custom_emoji_id=EMOJI_PROFILE)
    kb.button(text=TEXTS[lang]["btn_balance"], callback_data="balance", icon_custom_emoji_id=EMOJI_BALANCE)
    kb.button(text=TEXTS[lang]["btn_rating"], callback_data="rating_menu", icon_custom_emoji_id=EMOJI_STATS)
    
    kb.row(
        InlineKeyboardButton(text=TEXTS[lang]["btn_deposit"], callback_data="deposit", icon_custom_emoji_id=EMOJI_WALLET),
        InlineKeyboardButton(text=TEXTS[lang]["btn_withdraw"], callback_data="withdraw", icon_custom_emoji_id=EMOJI_WALLET)
    )
    
    kb.row(InlineKeyboardButton(text=TEXTS[lang]["btn_lang"], callback_data="change_lang", icon_custom_emoji_id=EMOJI_INFO))
    
    if is_admin:
        kb.row(InlineKeyboardButton(text=TEXTS[lang]["btn_admin"], callback_data="admin_panel", icon_custom_emoji_id=EMOJI_ADMIN))
        
    return kb.as_markup()

# ================= ХЭНДЛЕРЛЕР =================
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    args = message.text.split()
    
    # Рефералды өңдеу
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_id = int(args[1].replace("ref_", ""))
        if referrer_id != message.from_user.id:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET referrer_id = %s WHERE user_id = %s AND referrer_id IS NULL", (referrer_id, message.from_user.id))
                    conn.commit()

    # Лоббиге қосылу
    if len(args) > 1 and args[1].startswith("lobby_"):
        lobby_id = args[1].replace("lobby_", "")
        lobby = get_lobby(lobby_id)
        if lobby:
            players = get_lobby_players(lobby_id)
            bet_amount = float(lobby["bet_amount"])
            
            if float(user["balance"]) < bet_amount:
                lang = user["lang"]
                await message.answer(TEXTS[lang]["no_money"], parse_mode="HTML")
                return

            if message.from_user.id not in players:
                if len(players) >= 2:
                    lang = user["lang"]
                    await message.answer(TEXTS[lang]["lobby_full"], parse_mode="HTML")
                    return
                join_lobby_db(lobby_id, message.from_user.id)
                players.append(message.from_user.id)
            
            lang = user["lang"]
            await message.answer(TEXTS[lang]["lobby_joined"].format(lobby_id), parse_mode="HTML")
            
            host_id = lobby["host_id"]
            host = get_user(host_id)
            host_lang = host["lang"]
            
            player_mentions = ", ".join([f"[{p}](tg://user?id={p})" for p in players])
            await bot.send_message(host_id, TEXTS[host_lang]["lobby_waiting"].format(player_mentions), parse_mode="HTML")
            
            if len(players) == 2:
                await start_game(lobby_id, players, lobby["game_type"], bet_amount, lobby["chat_id"])
            return
        else:
            lang = user["lang"]
            await message.answer(TEXTS[lang]["lobby_not_found"], parse_mode="HTML")
            return

    if user["lang"]:
        lang = user["lang"]
        balance = user["balance"]
        is_admin = message.from_user.id == ADMIN_ID
        await message.answer(TEXTS[lang]["welcome"].format(balance), reply_markup=main_menu_kb(lang, is_admin), parse_mode="HTML")
    else:
        await message.answer(TEXTS["kz"]["lang_select"], reply_markup=lang_kb(), parse_mode="HTML")

# ================= ТОПТЫҚ ЧАТТАРҒА АРНАЛҒАН ФУНКЦИЯЛАР =================
@dp.message(Command("duel"))
async def group_duel(message: Message):
    if message.chat.type == "private":
        await message.answer("Бұл команда тек топтарда жұмыс істейді!")
        return
    
    args = message.text.split()
    bet_amount = 1.0 # Әдепкі ставка
    if len(args) > 1:
        try:
            bet_amount = float(args[1])
        except:
            pass
            
    lobby_id = str(uuid.uuid4())[:6].upper()
    create_lobby_db(lobby_id, message.from_user.id, "dice", bet_amount, message.chat.id)
    
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXTS["kz"]["group_join_btn"], callback_data=f"join_group_{lobby_id}")
    
    await message.answer(
        TEXTS["kz"]["group_duel_start"].format(bet_amount),
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("join_group_"))
async def join_group_game(call: CallbackQuery):
    lobby_id = call.data.split("_")[2]
    lobby = get_lobby(lobby_id)
    if not lobby:
        await call.answer("Лобби табылмады", show_alert=True)
        return
        
    user = get_user(call.from_user.id)
    bet_amount = float(lobby["bet_amount"])
    
    if float(user["balance"]) < bet_amount:
        await call.answer("Балансыңыз жеткіліксіз!", show_alert=True)
        return
        
    players = get_lobby_players(lobby_id)
    if call.from_user.id in players:
        await call.answer("Сіз қазірдің өзінде қосылдыңыз!", show_alert=True)
        return
        
    if len(players) >= 2:
        await call.answer("Лобби толы!", show_alert=True)
        return
        
    join_lobby_db(lobby_id, call.from_user.id)
    players.append(call.from_user.id)
    
    await call.answer("Сіз қосылдыңыз!")
    
    if len(players) == 2:
        await start_game(lobby_id, players, lobby["game_type"], bet_amount, lobby["chat_id"])
    else:
        await bot.send_message(lobby["chat_id"], TEXTS["kz"]["group_waiting"], parse_mode="HTML")

# ================= БАСҚА ДА ХЭНДЛЕРЛЕР (Өткендегідей) =================
# ... (Барлық ойын логикалары, профиль, баланс, пополнение, вывод, админ панель, рейтинг, анонимділік)

# Пополнение кезінде рефералға 10% беру
@lp.invoice_paid()
async def on_payment(invoice):
    try:
        parts = invoice.order_id.split("_")
        user_id = int(parts[1])
        amount = float(invoice.amount)
        
        # Балансты жаңарту
        update_balance(user_id, amount)
        update_deposit_status(user_id)
        
        # Рефералға 10% беру
        user = get_user(user_id)
        if user["referrer_id"] and not user["has_deposited"]:
            ref_bonus = amount * float(REFERRAL_PERCENT)
            update_balance(user["referrer_id"], ref_bonus)
            try:
                await bot.send_message(user["referrer_id"], f"🎉 Сіздің рефералыңыз пополнить етті! Сізге ${ref_bonus:.2f} түсті.", parse_mode="HTML")
            except:
                pass
        
        lang = user["lang"]
        await bot.send_message(user_id, TEXTS[lang]["deposit_paid"].format(user["balance"]), parse_mode="HTML")
    except Exception as e:
        print(f"Төлемді өңдеу қатесі: {e}")

# ================= ІСКЕ ҚОСУ =================
async def main():
    try:
        init_db()
        print("Duel Games + PvP + Group Chats + Referrals іске қосылды!")
    except Exception as e:
        print(f"Дерекқорға қосылу қатесі: {e}")
        return

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
