import asyncio
import os
import random
import uuid
from decimal import Decimal
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from lavepay import LavePay
================= БАПТАУЛАР =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
LAVEPAY_API_KEY = os.getenv("LAVEPAY_API_KEY")
LAVEPAY_WEBHOOK_SECRET = os.getenv("LAVEPAY_WEBHOOK_SECRET")
DB_URL = os.getenv("DATABASE_URL")
================= ПРЕМИУМ ЭМОДЗИ ID-ЛЕРІ (Тек мәтін мен батырма үшін) =================
Осы жерге өзіңіздің премиум эмодзилеріңіздің ID-лерін қойыңыз
EMOJI_DICE = "5368324170671202286"   # Мәтіндегі кости үшін
EMOJI_BJ = "5368324170671202286"     # Мәтіндегі блекджек үшін
EMOJI_DARTS = "5368324170671202286"  # Мәтіндегі дартс үшін
EMOJI_WALLET = "5368324170671202286" # Әмиян
EMOJI_TROPHY = "5368324170671202286" # Кубок
EMOJI_LANG = "5368324170671202286"   # Тіл
EMOJI_PROFILE = "5368324170671202286"# Профиль
EMOJI_WITHDRAW = "5368324170671202286"# Шығару
================= БОТ ЖӘНЕ ДЕРЕКҚОР =================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None
lp = LavePay(LAVEPAY_API_KEY, webhook_secret=LAVEPAY_WEBHOOK_SECRET)
================= ДЕРЕКҚОРДЫ ИНИЦИАЛИЗАЦИЯЛАУ =================
async def init_db():
global db_pool
db_pool = await asyncpg.create_pool(DB_URL)
async with db_pool.acquire() as conn:
await conn.execute("""
CREATE TABLE IF NOT EXISTS users (
user_id BIGINT PRIMARY KEY,
balance REAL DEFAULT 0.0,
lang TEXT DEFAULT 'kz',
wins INTEGER DEFAULT 0,
losses INTEGER DEFAULT 0,
games INTEGER DEFAULT 0
)
""")
await conn.execute("""
CREATE TABLE IF NOT EXISTS lobbies (
lobby_id TEXT PRIMARY KEY,
host_id BIGINT,
game_type TEXT,
status TEXT DEFAULT 'waiting'
)
""")
await conn.execute("""
CREATE TABLE IF NOT EXISTS lobby_players (
lobby_id TEXT REFERENCES lobbies(lobby_id) ON DELETE CASCADE,
user_id BIGINT,
PRIMARY KEY (lobby_id, user_id)
)
""")
================= ДЕРЕКҚОР ФУНКЦИЯЛАРЫ =================
async def get_user(user_id):
async with db_pool.acquire() as conn:
row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
if not row:
await conn.execute("INSERT INTO users (user_id) VALUES ($1)", user_id)
row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
return row
async def update_balance(user_id, amount):
async with db_pool.acquire() as conn:
await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)
async def set_lang(user_id, lang):
async with db_pool.acquire() as conn:
await conn.execute("UPDATE users SET lang = $1 WHERE user_id = $2", lang, user_id)
async def update_profile(user_id, win: bool):
async with db_pool.acquire() as conn:
if win:
await conn.execute("UPDATE users SET wins = wins + 1, games = games + 1 WHERE user_id = $1", user_id)
else:
await conn.execute("UPDATE users SET losses = losses + 1, games = games + 1 WHERE user_id = $1", user_id)
async def create_lobby_db(lobby_id, host_id, game_type):
async with db_pool.acquire() as conn:
await conn.execute("INSERT INTO lobbies (lobby_id, host_id, game_type) VALUES ($1, $2, $3)", lobby_id, host_id, game_type)
await conn.execute("INSERT INTO lobby_players (lobby_id, user_id) VALUES ($1, $2)", lobby_id, host_id)
async def join_lobby_db(lobby_id, user_id):
async with db_pool.acquire() as conn:
exists = await conn.fetchval("SELECT 1 FROM lobby_players WHERE lobby_id = $1 AND user_id = $2", lobby_id, user_id)
if not exists:
await conn.execute("INSERT INTO lobby_players (lobby_id, user_id) VALUES ($1, $2)", lobby_id, user_id)
async def get_lobby_players(lobby_id):
async with db_pool.acquire() as conn:
rows = await conn.fetch("SELECT user_id FROM lobby_players WHERE lobby_id = $1", lobby_id)
return [row["user_id"] for row in rows]
async def get_lobby(lobby_id):
async with db_pool.acquire() as conn:
return await conn.fetchrow("SELECT * FROM lobbies WHERE lobby_id = $1", lobby_id)
async def delete_lobby(lobby_id):
async with db_pool.acquire() as conn:
await conn.execute("DELETE FROM lobbies WHERE lobby_id = $1", lobby_id)
================= FSM КҮЙЛЕРІ =================
class WithdrawState(StatesGroup):
amount = State()
address = State()
================= ТІЛДЕР (Премиум эмодзилер тек мәтінде) =================
