import os
import re
import sqlite3
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


# === Конфигурация ===
BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "8337922006:AAEFTWqohFwFez5K4GkU8cvMkOFxIHCS__4",
)
ADMIN_ID_ENV = os.getenv("ADMIN_ID")
try:
    ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV else None
except ValueError:
    ADMIN_ID = None

DB_PATH = os.getenv("DB_PATH", "sora_codes.sqlite3")


# === Простая БД на SQLite ===
def with_connection(func):
    def wrapper(*args, **kwargs):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            result = func(conn, *args, **kwargs)
            conn.commit()
            return result
        finally:
            conn.close()
    return wrapper


@with_connection
def init_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            sharer_user_id INTEGER NOT NULL,
            sharer_name TEXT,
            sharer_username TEXT,
            created_at TEXT NOT NULL,
            valid INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stats (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            share_count INTEGER NOT NULL DEFAULT 0
        );
        """
    )


@with_connection
def add_code(
    conn: sqlite3.Connection,
    code: str,
    user_id: int,
    name: str,
    username: str,
) -> tuple[bool, str]:
    code = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{4,32}", code):
        return False, "Код должен содержать латинские буквы/цифры (4–32 символа)."

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO codes(code, sharer_user_id, sharer_name, sharer_username, created_at, valid)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (code, user_id, name, username or "", datetime.utcnow().isoformat()),
        )
    except sqlite3.IntegrityError:
        return False, "Этот код уже есть в базе. Спасибо!"

    # upsert статистики
    cur.execute(
        """
        INSERT INTO stats(user_id, name, username, share_count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            name=excluded.name,
            username=excluded.username,
            share_count=stats.share_count + 1
        """,
        (user_id, name, username or ""),
    )
    return True, "✅ Код успешно добавлен! Спасибо за помощь ❤️"


@with_connection
def get_oldest_code(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM codes WHERE valid = 1 ORDER BY datetime(created_at) ASC LIMIT 1"
    )
    row = cur.fetchone()
    return row


@with_connection
def remove_code_and_decrement(conn: sqlite3.Connection, code_id: int) -> None:
    cur = conn.cursor()
    cur.execute("SELECT sharer_user_id FROM codes WHERE id = ?", (code_id,))
    row = cur.fetchone()
    if not row:
        return
    sharer_id = int(row["sharer_user_id"]) if row["sharer_user_id"] is not None else None

    cur.execute("DELETE FROM codes WHERE id = ?", (code_id,))
    if sharer_id is not None:
        cur.execute(
            "UPDATE stats SET share_count = MAX(share_count - 1, 0) WHERE user_id = ?",
            (sharer_id,),
        )
        # по желанию можно удалять из топа, когда 0
        cur.execute("DELETE FROM stats WHERE user_id = ? AND share_count <= 0", (sharer_id,))


@with_connection
def get_leaderboard(conn: sqlite3.Connection, limit: int = 10):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT user_id, name, username, share_count
        FROM stats
        WHERE share_count > 0
        ORDER BY share_count DESC
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


@with_connection
def get_totals(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM codes WHERE valid = 1")
    codes_cnt = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) AS c FROM stats WHERE share_count > 0")
    users_cnt = int(cur.fetchone()[0])
    return codes_cnt, users_cnt


# === Состояния FSM ===
class ShareCode(StatesGroup):
    waiting = State()


# === Клавиатуры ===
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Получить код", callback_data="get_code")],
            [InlineKeyboardButton(text="📤 Поделиться кодом", callback_data="share_code")],
            [InlineKeyboardButton(text="🏆 Список почёта", callback_data="top")],
        ]
    )


def pledge_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data="will_share_yes"),
                InlineKeyboardButton(text="Нет", callback_data="will_share_no"),
            ]
        ]
    )


def code_feedback_kb(code_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Спасибо, работает!", callback_data=f"valid:{code_id}"),
                InlineKeyboardButton(text="❌ Код недействителен", callback_data=f"invalid:{code_id}"),
            ],
            [InlineKeyboardButton(text="📤 Поделиться кодом", callback_data="share_code")],
        ]
    )


# === Инициализация бота ===
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 👋 Это обменник кодами Sora 2.\nВыбери действие:",
        reply_markup=main_menu_kb(),
    )


@dp.callback_query(F.data == "get_code")
async def on_get_code_gate(call: CallbackQuery):
    await call.message.answer(
        (
            "Сейчас ты можешь получить код, потому что кто-то другой им поделился с тобой.\n\n"
            "После регистрации, пожалуйста, потом поделись полученным кодом — так ты поможешь другому человеку. "
            "Поделишься?🥹"
        ),
        reply_markup=pledge_kb(),
    )
    await call.answer()


@dp.callback_query(F.data == "will_share_yes")
async def on_will_share_yes(call: CallbackQuery):
    row = get_oldest_code()
    if not row:
        await call.message.answer(
            "❌ Сейчас нет доступных кодов. Поделись своим, чтобы помочь другим!",
            reply_markup=main_menu_kb(),
        )
        await call.answer()
        return

    code_id = int(row["id"])
    code = row["code"]
    sharer_name = row["sharer_name"] or "Пользователь"
    sharer_username = ("@" + row["sharer_username"]) if row["sharer_username"] else ""

    text = (
        f"Твой код: <b>{code}</b>\n\n"
        f"👤 Кодом поделился: {sharer_name} {sharer_username}\n\n"
        f"📱 Инструкция по установке:\n"
        f"Смени регион App Store на США (инструкция: https://t-j.ru/apple-region/)\n"
        f"Скачай приложение: https://apps.apple.com/us/app/sora-by-openai/id6744034028\n"
        f"Для использования может понадобиться VPN\n"
        f"💬 Если есть вопросы — задай их @Tesfjcvv\n\n"
        f"Если всё ок — нажми \"Спасибо, работает!\", если нет — \"Код недействителен\" и выдадим новый."
    )
    await call.message.answer(text, reply_markup=code_feedback_kb(code_id))
    await call.answer()


@dp.callback_query(F.data == "will_share_no")
async def on_will_share_no(call: CallbackQuery):
    await call.message.answer(
        "Хорошо! Если передумаешь — нажми \"Поделиться кодом\" в меню.",
        reply_markup=main_menu_kb(),
    )
    await call.answer()


@dp.callback_query(F.data == "share_code")
async def on_share_code(call: CallbackQuery, state: FSMContext):
    await state.set_state(ShareCode.waiting)
    await call.message.answer(
        "Отправь свой код одним сообщением. Спасибо за помощь другим пользователям!"
    )
    await call.answer()


@dp.message(ShareCode.waiting)
async def on_code_message(message: Message, state: FSMContext):
    user = message.from_user
    code_text = (message.text or "").strip()
    ok, msg = add_code(
        code_text,
        user_id=user.id,
        name=(user.first_name or "Пользователь"),
        username=(user.username or ""),
    )
    await message.answer(msg, reply_markup=main_menu_kb())
    await state.clear()


@dp.callback_query(F.data.startswith("valid:"))
async def on_valid(call: CallbackQuery):
    await call.message.answer("🎉 Отлично! Код подтверждён.")
    await call.answer()


@dp.callback_query(F.data.startswith("invalid:"))
async def on_invalid(call: CallbackQuery):
    try:
        code_id = int(call.data.split(":", 1)[1])
    except Exception:
        await call.answer("Ошибка данных", show_alert=True)
        return
    remove_code_and_decrement(code_id)
    await call.message.answer("🚫 Код удалён как недействительный.")
    await call.answer()


@dp.callback_query(F.data == "top")
async def on_top(call: CallbackQuery):
    rows = get_leaderboard(limit=10)
    if not rows:
        await call.message.answer("Пока никто не делился кодами 😢")
        await call.answer()
        return
    text_lines = ["🏆 <b>Список почёта:</b>\n"]
    for idx, r in enumerate(rows, start=1):
        name = r["name"] or "Пользователь"
        uname = ("@" + r["username"]) if r["username"] else ""
        cnt = int(r["share_count"]) if r["share_count"] is not None else 0
        text_lines.append(f"{idx}. {name} {uname} — <b>{cnt}</b> код(ов)")
    await call.message.answer("\n".join(text_lines))
    await call.answer()


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if ADMIN_ID is not None and message.from_user.id != ADMIN_ID:
        await message.answer("Доступ запрещён.")
        return
    codes_cnt, users_cnt = get_totals()
    await message.answer(
        (
            "🛠 <b>Админ-панель</b>\n"
            f"Активных кодов: <b>{codes_cnt}</b>\n"
            f"Пользователей в топе: <b>{users_cnt}</b>\n\n"
            "Команды: /start — меню, /admin — этот экран."
        ),
        reply_markup=main_menu_kb(),
    )


async def main() -> None:
    init_db()
    print("Бот запущен (aiogram, long polling)…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass