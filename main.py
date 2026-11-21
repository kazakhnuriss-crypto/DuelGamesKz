import asyncio
import os
import random
import uuid
from decimal import Decimal

import psycopg2
import psycopg2.extras
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from lavepay import LavePay

# ================= БАПТАУЛАР =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
LAVEPAY_API_KEY = os.getenv("LAVEPAY_API_KEY")
LAVEPAY_WEBHOOK_SECRET = os.getenv("LAVEPAY_WEBHOOK_SECRET")
DB_URL = os.getenv("DATABASE_URL")

# Егер Railway сілтемесінде postgresql:// болса, psycopg2 онымен тікелей жұмыс істейді.
# Егер postgres:// болса, оны postgresql:// деп ауыстырамыз (psycopg2 үшін).
if DB_URL and DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# ================= ПРЕМИУМ ЭМОДЗИ ID-ЛЕРІ =================
EMOJI_DICE = "5368324170671202286"
EMOJI_BJ = "5368324170671202286"
EMOJI_DARTS = "5368324170671202286"
EMOJI_WALLET = "5368324170671202286"
EMOJI_TROPHY = "5368324170671202286"
EMOJI_LANG = "5368324170671202286"
EMOJI_PROFILE = "5368324170671202286"
EMOJI_WITHDRAW = "5368324170671202286"

# ================= БОТ =================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
lp = LavePay(LAVEPAY_API_KEY, webhook_secret=LAVEPAY_WEBHOOK_SECRET)

# ================= ДЕРЕКҚОР ФУНКЦИЯЛАРЫ (psycopg2) =================
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
                    games INTEGER DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lobbies (
                    lobby_id TEXT PRIMARY KEY,
                    host_id BIGINT,
                    game_type TEXT,
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

def set_lang(user_id, lang):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET lang = %s WHERE user_id = %s", (lang, user_id))
            conn.commit()

def update_profile(user_id, win: bool):
    with get_db() as conn:
        with conn.cursor() as cur:
            if win:
                cur.execute("UPDATE users SET wins = wins + 1, games = games + 1 WHERE user_id = %s", (user_id,))
            else:
                cur.execute("UPDATE users SET losses = losses + 1, games = games + 1 WHERE user_id = %s", (user_id,))
            conn.commit()

def create_lobby_db(lobby_id, host_id, game_type):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO lobbies (lobby_id, host_id, game_type) VALUES (%s, %s, %s)", (lobby_id, host_id, game_type))
            cur.execute("INSERT INTO lobby_players (lobby_id, user_id) VALUES (%s, %s)", (lobby_id, host_id))
            conn.commit()

def join_lobby_db(lobby_id, user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM lobby_players WHERE lobby_id = %s AND user_id = %s", (lobby_id, user_id))
            if not cur.fetchone():
                cur.execute("INSERT INTO lobby_players (lobby_id, user_id) VALUES (%s, %s)", (lobby_id, user_id))
                conn.commit()

def get_lobby_players(lobby_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM lobby_players WHERE lobby_id = %s", (lobby_id,))
            return [row[0] for row in cur.fetchall()]

def get_lobby(lobby_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM lobbies WHERE lobby_id = %s", (lobby_id,))
            return cur.fetchone()

def delete_lobby(lobby_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lobbies WHERE lobby_id = %s", (lobby_id,))
            conn.commit()

# ================= FSM КҮЙЛЕРІ =================
class WithdrawState(StatesGroup):
    amount = State()
    address = State()

# ================= ТІЛДЕР =================
TEXTS = {
    "kz": {
        "welcome": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> Сәлем! **Duel Games**!\n\n💰 Балансыңыз: **${{:.2f}}**\n\nНе істейміз?',
        "btn_play": "🎮 Ойын ойнау",
        "btn_profile": f'<tg-emoji emoji-id="{EMOJI_PROFILE}">👤</tg-emoji> Профиль',
        "btn_balance": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> Баланс',
        "btn_lang": f'<tg-emoji emoji-id="{EMOJI_LANG}">🌐</tg-emoji> Тіл',
        "lang_select": "🌐 Тілді таңдаңыз:",
        "profile_text": "👤 **Профиль**\n\n🆔 ID: `{}`\n🏆 Жеңістер: {}\n❌ Жеңілістер: {}\n🎮 Ойындар: {}",
        "balance_text": "💰 **Балансыңыз:**\n\n💵 USD: **${:.2f}**\n🇰🇿 KZT: **{:.2f} ₸**\n🇷🇺 RUB: **{:.2f} ₽**\n\n(Бағам: 1 USD = 450 KZT = 90 RUB)",
        "choose_game": "🎮 Қай ойынды ойнаймыз?",
        "btn_dice_lobby": f'<tg-emoji emoji-id="{EMOJI_DICE}">🎲</tg-emoji> Кости Дуэль (Лобби)',
        "btn_darts_lobby": f'<tg-emoji emoji-id="{EMOJI_DARTS}">🎯</tg-emoji> Дартс Дуэль (Лобби)',
        "btn_bj_lobby": f'<tg-emoji emoji-id="{EMOJI_BJ}">🃏</tg-emoji> Блекджек (Лобби)',
        "lobby_created": "✅ **Лобби құрылды!**\n\nЛобби ID: `{}`\nОйын: {}\n\nДостарыңызды шақыру үшін осы ID-ді жіберіңіз немесе төмендегі сілтемемен бөлісіңіз:\n\n`https://t.me/{bot_username}?start=lobby_{lobby_id}`\n\nОйынды бастау үшін /start деп жазыңыз.",
        "lobby_joined": "✅ Сіз лоббиге қосылдыңыз! Лобби ID: `{}`\n\nОйын басталуын күтіңіз...",
        "lobby_not_found": "❌ Мұндай лобби табылмады.",
        "lobby_full": "❌ Лобби толы!",
        "lobby_started": "🎮 **Ойын басталды!**\n\nЛобби: {}",
        "lobby_waiting": "⏳ Лоббиде ойыншылар күтілуде...\nҚатысушылар: {}",
        "deposit_enter": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> **Толтыру**\n\nСоманы енгізіңіз (USD):',
        "deposit_invoice": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> **Төлем: ${{}}**\n\nТөлеу үшін басыңыз:',
        "deposit_paid": f'✅ **Төлем қабылданды!**\n\nБалансыңыз: **${{:.2f}}**',
        "withdraw_enter_amount": f'<tg-emoji emoji-id="{EMOJI_WITHDRAW}">💸</tg-emoji> **Ақша шығару**\n\nБалансыңыз: **${{:.2f}}**\n\nЕң аз сома: $1.00\nКомиссия: 5%\n\nШығарғыңыз келетін соманы енгізіңіз:',
        "withdraw_enter_address": "📍 **Әмиян мекенжайын енгізіңіз** (USDT TRC20):",
        "withdraw_error_min": "❌ Ең аз сома $1.00.",
        "withdraw_error_bal": "❌ Балансыңыз жеткіліксіз!",
        "withdraw_error_addr": "❌ Қате әмиян мекенжайы. Қайталап көріңіз.",
        "withdraw_success": f'✅ **Өтінім қабылданды!**\n\nСома: **${{}}** (комиссия шегерілді)\n\nАқша 5-10 минут ішінде келеді.',
        "withdraw_fail": "❌ **Қате!** Lave Pay API жауап бермеді. Кейінірек қайталап көріңіз.",
    },
    "ru": {
        "welcome": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> Привет! **Duel Games**!\n\n💰 Баланс: **${{:.2f}}**\n\nЧто делаем?',
        "btn_play": "🎮 Играть",
        "btn_profile": f'<tg-emoji emoji-id="{EMOJI_PROFILE}">👤</tg-emoji> Профиль',
        "btn_balance": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> Баланс',
        "btn_lang": f'<tg-emoji emoji-id="{EMOJI_LANG}">🌐</tg-emoji> Язык',
        "lang_select": "🌐 Выберите язык:",
        "profile_text": "👤 **Профиль**\n\n🆔 ID: `{}`\n🏆 Победы: {}\n❌ Поражения: {}\n🎮 Игры: {}",
        "balance_text": "💰 **Ваш баланс:**\n\n💵 USD: **${:.2f}**\n🇰🇿 KZT: **{:.2f} ₸**\n🇷🇺 RUB: **{:.2f} ₽**\n\n(Курс: 1 USD = 450 KZT = 90 RUB)",
        "choose_game": "🎮 Какую игру выберем?",
        "btn_dice_lobby": f'<tg-emoji emoji-id="{EMOJI_DICE}">🎲</tg-emoji> Дуэль на костях (Лобби)',
        "btn_darts_lobby": f'<tg-emoji emoji-id="{EMOJI_DARTS}">🎯</tg-emoji> Дуэль в дартс (Лобби)',
        "btn_bj_lobby": f'<tg-emoji emoji-id="{EMOJI_BJ}">🃏</tg-emoji> Блэкджек (Лобби)',
        "lobby_created": "✅ **Лобби создано!**\n\nID лобби: `{}`\nИгра: {}\n\nОтправьте этот ID друзьям или поделитесь ссылкой:\n\n`https://t.me/{bot_username}?start=lobby_{lobby_id}`\n\nЧтобы начать игру, напишите /start.",
        "lobby_joined": "✅ Вы присоединились к лобби! ID: `{}`\n\nОжидайте начала игры...",
        "lobby_not_found": "❌ Лобби не найдено.",
        "lobby_full": "❌ Лобби заполнено!",
        "lobby_started": "🎮 **Игра началась!**\n\nЛобби: {}",
        "lobby_waiting": "⏳ Ожидание игроков в лобби...\nУчастники: {}",
        "deposit_enter": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> **Пополнение**\n\nВведите сумму (USD):',
        "deposit_invoice": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> **К оплате: ${{}}**',
        "deposit_paid": f'✅ **Платеж получен!**\n\nБаланс: **${{:.2f}}**',
        "withdraw_enter_amount": f'<tg-emoji emoji-id="{EMOJI_WITHDRAW}">💸</tg-emoji> **Вывод средств**\n\nБаланс: **${{:.2f}}**\n\nМин. сумма: $1.00\nКомиссия: 5%\n\nВведите сумму для вывода:',
        "withdraw_enter_address": "📍 **Введите адрес кошелька** (USDT TRC20):",
        "withdraw_error_min": "❌ Минимальная сумма $1.00.",
        "withdraw_error_bal": "❌ Недостаточно средств!",
        "withdraw_error_addr": "❌ Неверный адрес кошелька. Попробуйте снова.",
        "withdraw_success": f'✅ **Заявка принята!**\n\nСумма: **${{}}** (с учетом комиссии)\n\nСредства придут в течение 5-10 минут.',
        "withdraw_fail": "❌ **Ошибка!** Lave Pay API не ответил. Попробуйте позже.",
    },
    "en": {
        "welcome": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> Hello! **Duel Games**!\n\n💰 Balance: **${{:.2f}}**\n\nWhat to do?',
        "btn_play": "🎮 Play",
        "btn_profile": f'<tg-emoji emoji-id="{EMOJI_PROFILE}">👤</tg-emoji> Profile',
        "btn_balance": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> Balance',
        "btn_lang": f'<tg-emoji emoji-id="{EMOJI_LANG}">🌐</tg-emoji> Language',
        "lang_select": "🌐 Choose language:",
        "profile_text": "👤 **Profile**\n\n🆔 ID: `{}`\n🏆 Wins: {}\n❌ Losses: {}\n🎮 Games: {}",
        "balance_text": "💰 **Your balance:**\n\n💵 USD: **${:.2f}**\n🇰🇿 KZT: **{:.2f} ₸**\n🇷🇺 RUB: **{:.2f} ₽**\n\n(Rate: 1 USD = 450 KZT = 90 RUB)",
        "choose_game": "🎮 Which game to play?",
        "btn_dice_lobby": f'<tg-emoji emoji-id="{EMOJI_DICE}">🎲</tg-emoji> Dice Duel (Lobby)',
        "btn_darts_lobby": f'<tg-emoji emoji-id="{EMOJI_DARTS}">🎯</tg-emoji> Darts Duel (Lobby)',
        "btn_bj_lobby": f'<tg-emoji emoji-id="{EMOJI_BJ}">🃏</tg-emoji> Blackjack (Lobby)',
        "lobby_created": "✅ **Lobby created!**\n\nLobby ID: `{}`\nGame: {}\n\nShare this ID with friends or use the link:\n\n`https://t.me/{bot_username}?start=lobby_{lobby_id}`\n\nTo start the game, type /start.",
        "lobby_joined": "✅ You joined the lobby! ID: `{}`\n\nWaiting for the game to start...",
        "lobby_not_found": "❌ Lobby not found.",
        "lobby_full": "❌ Lobby is full!",
        "lobby_started": "🎮 **Game started!**\n\nLobby: {}",
        "lobby_waiting": "⏳ Waiting for players...\nParticipants: {}",
        "deposit_enter": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> **Deposit**\n\nEnter amount (USD):',
        "deposit_invoice": f'<tg-emoji emoji-id="{EMOJI_WALLET}">💳</tg-emoji> **Amount: ${{}}**',
        "deposit_paid": f'✅ **Payment received!**\n\nBalance: **${{:.2f}}**',
        "withdraw_enter_amount": f'<tg-emoji emoji-id="{EMOJI_WITHDRAW}">💸</tg-emoji> **Withdraw**\n\nBalance: **${{:.2f}}**\n\nMin: $1.00\nFee: 5%\n\nEnter amount to withdraw:',
        "withdraw_enter_address": "📍 **Enter your wallet address** (USDT TRC20):",
        "withdraw_error_min": "❌ Minimum is $1.00.",
        "withdraw_error_bal": "❌ Insufficient balance!",
        "withdraw_error_addr": "❌ Invalid wallet address. Try again.",
        "withdraw_success": f'✅ **Request accepted!**\n\nAmount: **${{}}** (after fee)\n\nFunds will arrive in 5-10 mins.',
        "withdraw_fail": "❌ **Error!** Lave Pay API did not respond. Try again later.",
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

def main_menu_kb(lang):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXTS[lang]["btn_play"], callback_data="play_menu")
    kb.button(text=TEXTS[lang]["btn_profile"], callback_data="profile")
    kb.button(text=TEXTS[lang]["btn_balance"], callback_data="balance")
    kb.button(text=TEXTS[lang]["btn_lang"], callback_data="change_lang")
    kb.adjust(2, 2)
    return kb.as_markup()

def games_kb(lang):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXTS[lang]["btn_dice_lobby"], callback_data="create_lobby_dice")
    kb.button(text=TEXTS[lang]["btn_darts_lobby"], callback_data="create_lobby_darts")
    kb.button(text=TEXTS[lang]["btn_bj_lobby"], callback_data="create_lobby_bj")
    kb.button(text="🔙 Артқа / Назад / Back", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()

# ================= ХЭНДЛЕРЛЕР =================
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    args = message.text.split()
    
    if len(args) > 1 and args[1].startswith("lobby_"):
        lobby_id = args[1].replace("lobby_", "")
        lobby = get_lobby(lobby_id)
        if lobby:
            players = get_lobby_players(lobby_id)
            if message.from_user.id not in players:
                if len(players) >= 2:
                    lang = user["lang"]
                    await message.answer(TEXTS[lang]["lobby_full"])
                    return
                join_lobby_db(lobby_id, message.from_user.id)
                players.append(message.from_user.id)
            
            lang = user["lang"]
            await message.answer(TEXTS[lang]["lobby_joined"].format(lobby_id), parse_mode="HTML")
            
            host_id = lobby["host_id"]
            host = get_user(host_id)
            host_lang = host["lang"]
            
            player_mentions = ", ".join([f"[{p}](tg://user?id={p})" for p in players])
            await bot.send_message(
                host_id, 
                TEXTS[host_lang]["lobby_waiting"].format(player_mentions),
                parse_mode="HTML"
            )
            
            if len(players) == 2:
                await start_game(lobby_id, players, lobby["game_type"])
            return
        else:
            lang = user["lang"]
            await message.answer(TEXTS[lang]["lobby_not_found"])
            return

    if user["lang"]:
        lang = user["lang"]
        balance = user["balance"]
        await message.answer(TEXTS[lang]["welcome"].format(balance), reply_markup=main_menu_kb(lang), parse_mode="HTML")
    else:
        await message.answer(TEXTS["kz"]["lang_select"], reply_markup=lang_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("lang_"))
async def set_lang_cb(call: CallbackQuery):
    lang = call.data.split("_")[1]
    set_lang(call.from_user.id, lang)
    user = get_user(call.from_user.id)
    await call.message.edit_text(TEXTS[lang]["welcome"].format(user["balance"]), reply_markup=main_menu_kb(lang), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery):
    user = get_user(call.from_user.id)
    lang = user["lang"]
    await call.message.edit_text(TEXTS[lang]["welcome"].format(user["balance"]), reply_markup=main_menu_kb(lang), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "play_menu")
async def play_menu(call: CallbackQuery):
    user = get_user(call.from_user.id)
    lang = user["lang"]
    await call.message.edit_text(TEXTS[lang]["choose_game"], reply_markup=games_kb(lang), parse_mode="HTML")
    await call.answer()

# ================= ЛОББИ ҚҰРУ =================
@dp.callback_query(F.data.startswith("create_lobby_"))
async def create_lobby(call: CallbackQuery):
    user = get_user(call.from_user.id)
    lang = user["lang"]
    game_type = call.data.split("_")[2]
    
    lobby_id = str(uuid.uuid4())[:6].upper()
    create_lobby_db(lobby_id, call.from_user.id, game_type)
    
    game_name = {
        "dice": TEXTS[lang]["btn_dice_lobby"],
        "darts": TEXTS[lang]["btn_darts_lobby"],
        "bj": TEXTS[lang]["btn_bj_lobby"]
    }[game_type]
    
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=lobby_{lobby_id}"
    
    text = TEXTS[lang]["lobby_created"].format(lobby_id, game_name, bot_username=bot_info.username, lobby_id=lobby_id)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Достарға жіберу", url=f"https://t.me/share/url?url={link}&text=Лоббиге қосыл!")
    kb.button(text="🔙 Артқа", callback_data="play_menu")
    kb.adjust(1)
    
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()

# ================= ОЙЫНДЫ БАСТАУ =================
async def start_game(lobby_id, players, game_type):
    p1, p2 = players
    user1 = get_user(p1)
    lang = user1["lang"]
    
    await bot.send_message(p1, TEXTS[lang]["lobby_started"].format(lobby_id))
    await bot.send_message(p2, TEXTS[lang]["lobby_started"].format(lobby_id))
    
    if game_type == "dice":
        await play_dice_duel(p1, p2)
    elif game_type == "darts":
        await play_darts_duel(p1, p2)
    elif game_type == "bj":
        await bot.send_message(p1, "🃏 Блекджек әзірге дайындалып жатыр.")
        await bot.send_message(p2, "🃏 Блекджек әзірге дайындалып жатыр.")
    
    delete_lobby(lobby_id)

# ================= ОЙЫН ЛОГИКАСЫ =================
async def play_dice_duel(p1, p2):
    chat_id = p1
    await bot.send_message(chat_id, "🎲 1-ойыншы лақтырып жатыр...")
    d1 = await bot.send_dice(chat_id, emoji="🎲")
    d2 = await bot.send_dice(chat_id, emoji="🎲")
    d3 = await bot.send_dice(chat_id, emoji="🎲")
    await asyncio.sleep(4)
    score1 = d1.dice.value + d2.dice.value + d3.dice.value
    
    await bot.send_message(chat_id, "🎲 2-ойыншы лақтырып жатыр...")
    d4 = await bot.send_dice(chat_id, emoji="🎲")
    d5 = await bot.send_dice(chat_id, emoji="🎲")
    d6 = await bot.send_dice(chat_id, emoji="🎲")
    await asyncio.sleep(4)
    score2 = d4.dice.value + d5.dice.value + d6.dice.value
    
    user1 = get_user(p1)
    lang = user1["lang"]
    
    if score1 > score2:
        update_profile(p1, True)
        update_profile(p2, False)
        await bot.send_message(chat_id, TEXTS[lang]["win"].format(f"1-ойыншы ({score1} vs {score2})"))
    elif score2 > score1:
        update_profile(p1, False)
        update_profile(p2, True)
        await bot.send_message(chat_id, TEXTS[lang]["win"].format(f"2-ойыншы ({score1} vs {score2})"))
    else:
        await bot.send_message(chat_id, TEXTS[lang]["tie"])

async def play_darts_duel(p1, p2):
    chat_id = p1
    await bot.send_message(chat_id, "🎯 1-ойыншы лақтырып жатыр...")
    dart1 = await bot.send_dice(chat_id, emoji="🎯")
    await asyncio.sleep(3)
    
    await bot.send_message(chat_id, "🎯 2-ойыншы лақтырып жатыр...")
    dart2 = await bot.send_dice(chat_id, emoji="🎯")
    await asyncio.sleep(3)
    
    user1 = get_user(p1)
    lang = user1["lang"]
    
    if dart1.dice.value > dart2.dice.value:
        update_profile(p1, True)
        update_profile(p2, False)
        await bot.send_message(chat_id, TEXTS[lang]["win"].format(f"1-ойыншы ({dart1.dice.value} vs {dart2.dice.value})"))
    elif dart2.dice.value > dart1.dice.value:
        update_profile(p1, False)
        update_profile(p2, True)
        await bot.send_message(chat_id, TEXTS[lang]["win"].format(f"2-ойыншы ({dart1.dice.value} vs {dart2.dice.value})"))
    else:
        await bot.send_message(chat_id, TEXTS[lang]["tie"])

# ================= ПРОФИЛЬ ЖӘНЕ БАЛАНС =================
@dp.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    user = get_user(call.from_user.id)
    lang = user["lang"]
    text = TEXTS[lang]["profile_text"].format(user["user_id"], user["wins"], user["losses"], user["games"])
    await call.message.edit_text(text, reply_markup=main_menu_kb(lang), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "balance")
async def balance(call: CallbackQuery):
    user = get_user(call.from_user.id)
    lang = user["lang"]
    usd = user["balance"]
    
    kzt = usd * 450.0
    rub = usd * 90.0
    
    text = TEXTS[lang]["balance_text"].format(usd, kzt, rub)
    await call.message.edit_text(text, reply_markup=main_menu_kb(lang), parse_mode="HTML")
    await call.answer()

# ================= ТӨЛЕМ (ПОПОЛНЕНИЕ) =================
@dp.callback_query(F.data == "deposit")
async def deposit_handler(call: CallbackQuery):
    user = get_user(call.from_user.id)
    lang = user["lang"]
    await call.message.edit_text(TEXTS[lang]["deposit_enter"], parse_mode="HTML")
    await call.answer()

@dp.message(F.text.regexp(r"^\d+(\.\d{1,2})?$"))
async def process_amount(message: Message):
    user = get_user(message.from_user.id)
    lang = user["lang"]
    
    try:
        amount = Decimal(message.text)
        if amount <= 0:
            return
    except:
        return
    
    order_id = f"dep_{message.from_user.id}_{int(asyncio.get_event_loop().time())}"
    
    try:
        invoice = await lp.invoices.create(amount, order_id=order_id, description=f"Duel Games top-up for {message.from_user.id}")
        
        kb = InlineKeyboardBuilder()
        kb.button(text="💳 Төлеу / Оплатить / Pay", url=invoice.pay_url)
        kb.button(text="🔙 Артқа / Назад / Back", callback_data="back_main")
        kb.adjust(1)
        
        await message.answer(
            TEXTS[lang]["deposit_invoice"].format(amount),
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
        await invoice.poll(message=message, timeout=600)
        
    except Exception as e:
        await message.answer(f"❌ Қате: {e}")

@lp.invoice_paid()
async def on_payment(invoice):
    try:
        parts = invoice.order_id.split("_")
        user_id = int(parts[1])
        amount = float(invoice.amount)
        
        update_balance(user_id, amount)
        
        user = get_user(user_id)
        lang = user["lang"]
        await bot.send_message(user_id, TEXTS[lang]["deposit_paid"].format(user["balance"]), parse_mode="HTML")
    except Exception as e:
        print(f"Төлемді өңдеу қатесі: {e}")

# ================= ВЫВОД =================
@dp.callback_query(F.data == "withdraw")
async def withdraw_handler(call: CallbackQuery, state: FSMContext):
    user = get_user(call.from_user.id)
    lang = user["lang"]
    await state.set_state(WithdrawState.amount)
    await call.message.edit_text(TEXTS[lang]["withdraw_enter_amount"].format(user["balance"]), parse_mode="HTML")
    await call.answer()

@dp.message(WithdrawState.amount, F.text.regexp(r"^\d+(\.\d{1,2})?$"))
async def process_withdraw_amount(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    lang = user["lang"]
    
    try:
        amount = Decimal(message.text)
        if amount < Decimal("1.00"):
            await message.answer(TEXTS[lang]["withdraw_error_min"])
            return
        if amount > Decimal(str(user["balance"])):
            await message.answer(TEXTS[lang]["withdraw_error_bal"])
            return
    except:
        return
    
    await state.update_data(withdraw_amount=str(amount))
    await state.set_state(WithdrawState.address)
    
    await message.answer(TEXTS[lang]["withdraw_enter_address"], parse_mode="HTML")

@dp.message(WithdrawState.address)
async def process_withdraw_address(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    lang = user["lang"]
    data = await state.get_data()
    amount = Decimal(data.get("withdraw_amount"))
    address = message.text.strip()
    
    if not address.startswith("T") or len(address) < 30:
        await message.answer(TEXTS[lang]["withdraw_error_addr"])
        return
    
    fee = amount * Decimal("0.05")
    withdraw_amount = amount - fee
    
    try:
        await lp.withdrawals.create(
            amount=withdraw_amount,
            wallet="USDT_TRC20",
            address=address
        )
        
        update_balance(message.from_user.id, -float(amount))
        user = get_user(message.from_user.id)
        
        await message.answer(TEXTS[lang]["withdraw_success"].format(f"{withdraw_amount:.2f}"), parse_mode="HTML")
        await message.answer(TEXTS[lang]["welcome"].format(user["balance"]), reply_markup=main_menu_kb(lang), parse_mode="HTML")
        
    except Exception as e:
        await message.answer(TEXTS[lang]["withdraw_fail"])
        print(f"Вывод қатесі: {e}")
    
    await state.clear()

# ================= ІСКЕ ҚОСУ =================
async def main():
    try:
        init_db()
        print("Duel Games + PostgreSQL (psycopg2) іске қосылды!")
    except Exception as e:
        print(f"Дерекқорға қосылу қатесі: {e}")
        return

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
