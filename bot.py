import asyncio
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv


# -------------------------------
# Configuration and constants
# -------------------------------

ADMIN_USER_ID = 7694543415
DB_PATH = "diamond_clicker.db"


@dataclass
class ConfigValues:
    base_click_reward: int = 1
    max_clicks_per_second: int = 10
    miner_base_rate_per_min: float = 0.5  # per miner
    farm_multiplier_per_level: float = 1.25
    click_upgrade_base_cost: int = 50
    click_upgrade_cost_growth: float = 1.7
    miner_base_cost: int = 100
    miner_cost_growth: float = 1.55
    farm_base_cost: int = 500
    farm_cost_growth: float = 2.0
    daily_bonus_base: int = 100
    daily_bonus_growth_per_streak: float = 1.2
    leaderboard_size: int = 10
    diamond_to_gold_rate: int = 100  # 1 💎 -> N gold


def now_ts() -> int:
    return int(time.time())


def today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self._init_schema()
        await self._ensure_default_config()

    async def _init_schema(self) -> None:
        assert self.conn is not None
        await self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at INTEGER NOT NULL,
                diamonds INTEGER NOT NULL,
                gold INTEGER NOT NULL,
                click_power INTEGER NOT NULL,
                clicks_total INTEGER NOT NULL,
                total_earned INTEGER NOT NULL,
                last_click_window_start INTEGER,
                clicks_in_window INTEGER,
                last_mine_ts INTEGER,
                uncollected_mine REAL NOT NULL,
                miner_count INTEGER NOT NULL,
                farm_level INTEGER NOT NULL,
                daily_streak INTEGER NOT NULL,
                last_daily_claim TEXT,
                vip INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS achievements (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                condition_type TEXT NOT NULL,
                condition_value INTEGER NOT NULL,
                reward_diamonds INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_achievements (
                user_id INTEGER NOT NULL,
                achievement_id TEXT NOT NULL,
                awarded_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, achievement_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (achievement_id) REFERENCES achievements(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                action TEXT NOT NULL,
                actor_id INTEGER,
                target_id INTEGER,
                details TEXT
            );
            """
        )
        await self.conn.commit()

    async def _ensure_default_config(self) -> None:
        assert self.conn is not None
        defaults = ConfigValues()
        existing = {}
        async with self.conn.execute("SELECT key, value FROM config") as cur:
            async for row in cur:
                existing[row["key"]] = row["value"]

        to_insert: List[Tuple[str, str]] = []
        for field, value in defaults.__dict__.items():
            if field not in existing:
                to_insert.append((field, str(value)))
        if to_insert:
            await self.conn.executemany("INSERT INTO config(key, value) VALUES (?, ?)", to_insert)
            await self.conn.commit()

    async def get_config(self) -> ConfigValues:
        assert self.conn is not None
        values: Dict[str, str] = {}
        async with self.conn.execute("SELECT key, value FROM config") as cur:
            async for row in cur:
                values[row["key"]] = row["value"]
        result = ConfigValues()
        for k, v in values.items():
            if hasattr(result, k):
                field_type = type(getattr(result, k))
                try:
                    setattr(result, k, field_type(v))
                except Exception:
                    pass
        return result

    async def set_config_value(self, key: str, value: str) -> bool:
        assert self.conn is not None
        await self.conn.execute("REPLACE INTO config(key, value) VALUES (?, ?)", (key, value))
        await self.conn.commit()
        return True

    async def get_or_create_user(self, user_id: int, username: str, first_name: str) -> aiosqlite.Row:
        assert self.conn is not None
        async with self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row is not None:
            return row
        created = now_ts()
        await self.conn.execute(
            """
            INSERT INTO users(
                user_id, username, first_name, created_at,
                diamonds, gold, click_power, clicks_total, total_earned,
                last_click_window_start, clicks_in_window, last_mine_ts,
                uncollected_mine, miner_count, farm_level, daily_streak,
                last_daily_claim, vip
            ) VALUES (?, ?, ?, ?, 0, 0, 1, 0, 0, NULL, 0, ?, 0.0, 0, 0, 0, NULL, 0)
            """,
            (user_id, username, first_name, created, now_ts()),
        )
        await self.conn.commit()
        async with self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur2:
            row2 = await cur2.fetchone()
        return row2

    async def update_user_fields(self, user_id: int, fields: Dict[str, Any]) -> None:
        assert self.conn is not None
        if not fields:
            return
        keys = ", ".join([f"{k} = ?" for k in fields.keys()])
        values = list(fields.values()) + [user_id]
        await self.conn.execute(f"UPDATE users SET {keys} WHERE user_id = ?", values)
        await self.conn.commit()

    # --------------- Mining accrual ---------------
    async def accrue_mining(self, user_id: int) -> aiosqlite.Row:
        assert self.conn is not None
        async with self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            user = await cur.fetchone()
        if user is None:
            raise RuntimeError("User not found")
        cfg = await self.get_config()
        last_ts = user["last_mine_ts"] or now_ts()
        elapsed = max(0, now_ts() - int(last_ts))
        miner_count = int(user["miner_count"]) or 0
        farm_level = int(user["farm_level"]) or 0
        multiplier = (cfg.farm_multiplier_per_level ** farm_level) if farm_level > 0 else 1.0
        rate_per_sec = (miner_count * (cfg.miner_base_rate_per_min / 60.0)) * multiplier
        added = rate_per_sec * float(elapsed)
        new_uncollected = float(user["uncollected_mine"]) + added
        await self.update_user_fields(user_id, {"uncollected_mine": new_uncollected, "last_mine_ts": now_ts()})
        async with self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur2:
            return await cur2.fetchone()

    async def collect_mining(self, user_id: int) -> Tuple[int, float]:
        assert self.conn is not None
        user = await self.accrue_mining(user_id)
        to_collect_int = int(math.floor(float(user["uncollected_mine"])) )
        if to_collect_int <= 0:
            return 0, float(user["uncollected_mine"])  # nothing
        new_balance = int(user["diamonds"]) + to_collect_int
        new_uncollected = float(user["uncollected_mine"]) - to_collect_int
        new_total_earned = int(user["total_earned"]) + to_collect_int
        await self.update_user_fields(
            user_id,
            {
                "diamonds": new_balance,
                "uncollected_mine": new_uncollected,
                "total_earned": new_total_earned,
            },
        )
        return to_collect_int, new_uncollected

    # --------------- Click handling ---------------
    async def process_click(self, user_id: int) -> Tuple[bool, int, str]:
        assert self.conn is not None
        cfg = await self.get_config()
        async with self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            user = await cur.fetchone()
        if user is None:
            raise RuntimeError("User not found")
        window_start = user["last_click_window_start"] or 0
        clicks_in_window = int(user["clicks_in_window"]) if user["clicks_in_window"] is not None else 0
        now = now_ts()
        if now - int(window_start) >= 1:
            window_start = now
            clicks_in_window = 0
        if clicks_in_window >= int(cfg.max_clicks_per_second):
            return False, int(user["diamonds"]), "⏱ Слишком быстро! Подожди секунду."
        click_reward = int(cfg.base_click_reward) * int(user["click_power"])
        new_balance = int(user["diamonds"]) + click_reward
        new_clicks_in_window = clicks_in_window + 1
        new_clicks_total = int(user["clicks_total"]) + 1
        new_total_earned = int(user["total_earned"]) + click_reward
        await self.update_user_fields(
            user_id,
            {
                "diamonds": new_balance,
                "last_click_window_start": window_start,
                "clicks_in_window": new_clicks_in_window,
                "clicks_total": new_clicks_total,
                "total_earned": new_total_earned,
            },
        )
        return True, new_balance, f"+{click_reward} 💎"

    # --------------- Shop and upgrades ---------------
    async def calc_cost_click_power(self, current_power: int) -> int:
        cfg = await self.get_config()
        # power starts at 1; buying next level costs base * growth^(current_power-1)
        return int(round(cfg.click_upgrade_base_cost * (cfg.click_upgrade_cost_growth ** (max(0, current_power - 1)))))

    async def calc_cost_miner(self, current_miners: int) -> int:
        cfg = await self.get_config()
        return int(round(cfg.miner_base_cost * (cfg.miner_cost_growth ** max(0, current_miners))))

    async def calc_cost_farm_level(self, current_level: int) -> int:
        cfg = await self.get_config()
        return int(round(cfg.farm_base_cost * (cfg.farm_cost_growth ** max(0, current_level))))

    async def purchase_click_power(self, user_id: int) -> Tuple[bool, str]:
        async with self.conn.execute("SELECT diamonds, click_power FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return False, "Ошибка профиля"
        cost = await self.calc_cost_click_power(int(row["click_power"]))
        if int(row["diamonds"]) < cost:
            return False, f"Не хватает 💎. Цена: {cost}"
        await self.update_user_fields(
            user_id,
            {
                "diamonds": int(row["diamonds"]) - cost,
                "click_power": int(row["click_power"]) + 1,
            },
        )
        return True, f"Клик усилен! Цена: {cost} 💎"

    async def purchase_miner(self, user_id: int) -> Tuple[bool, str]:
        async with self.conn.execute("SELECT diamonds, miner_count FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return False, "Ошибка профиля"
        cost = await self.calc_cost_miner(int(row["miner_count"]))
        if int(row["diamonds"]) < cost:
            return False, f"Не хватает 💎. Цена: {cost}"
        await self.update_user_fields(
            user_id,
            {
                "diamonds": int(row["diamonds"]) - cost,
                "miner_count": int(row["miner_count"]) + 1,
            },
        )
        return True, f"Нанят майнер! Цена: {cost} 💎"

    async def purchase_farm_level(self, user_id: int) -> Tuple[bool, str]:
        async with self.conn.execute("SELECT diamonds, farm_level FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return False, "Ошибка профиля"
        cost = await self.calc_cost_farm_level(int(row["farm_level"]))
        if int(row["diamonds"]) < cost:
            return False, f"Не хватает 💎. Цена: {cost}"
        await self.update_user_fields(
            user_id,
            {
                "diamonds": int(row["diamonds"]) - cost,
                "farm_level": int(row["farm_level"]) + 1,
            },
        )
        return True, f"Ферма улучшена! Цена: {cost} 💎"

    # --------------- Daily bonus ---------------
    async def claim_daily(self, user_id: int) -> Tuple[bool, str]:
        cfg = await self.get_config()
        async with self.conn.execute(
            "SELECT diamonds, daily_streak, last_daily_claim, total_earned FROM users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False, "Ошибка профиля"
        last = row["last_daily_claim"]
        today = today_str()
        if last == today:
            return False, "Бонус уже получен сегодня"
        streak = int(row["daily_streak"]) or 0
        if last is None:
            streak = 1
        else:
            try:
                last_date = datetime.fromisoformat(last).date()
            except Exception:
                last_date = None
            if last_date is not None and (datetime.now(timezone.utc).date() - last_date).days == 1:
                streak += 1
            else:
                streak = 1
        reward = int(round(cfg.daily_bonus_base * (cfg.daily_bonus_growth_per_streak ** (streak - 1))))
        new_balance = int(row["diamonds"]) + reward
        new_total_earned = int(row["total_earned"]) + reward
        await self.update_user_fields(
            user_id,
            {
                "diamonds": new_balance,
                "daily_streak": streak,
                "last_daily_claim": today,
                "total_earned": new_total_earned,
            },
        )
        return True, f"Ежедневный бонус: +{reward} 💎 (серия: {streak})"

    # --------------- Achievements ---------------
    async def seed_achievements(self) -> None:
        assert self.conn is not None
        data = [
            ("clicks_100", "100 кликов", "Сделай 100 кликов", "clicks_total", 100, 50),
            ("clicks_1000", "1000 кликов", "Сделай 1000 кликов", "clicks_total", 1000, 250),
            ("earned_1k", "1k алмазов", "Заработай всего 1000 алмазов", "total_earned", 1000, 100),
            ("miners_10", "10 майнеров", "Найми 10 майнеров", "miner_count", 10, 300),
            ("farm_lvl_5", "Ферма 5 ур.", "Достигни 5 уровня фермы", "farm_level", 5, 500),
        ]
        await self.conn.executemany(
            "INSERT OR IGNORE INTO achievements(id, name, description, condition_type, condition_value, reward_diamonds) VALUES(?,?,?,?,?,?)",
            data,
        )
        await self.conn.commit()

    async def check_and_award_achievements(self, user_id: int) -> List[str]:
        assert self.conn is not None
        async with self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            user = await cur.fetchone()
        if user is None:
            return []
        fields = {
            "clicks_total": int(user["clicks_total"]) or 0,
            "total_earned": int(user["total_earned"]) or 0,
            "miner_count": int(user["miner_count"]) or 0,
            "farm_level": int(user["farm_level"]) or 0,
        }
        awarded: List[str] = []
        async with self.conn.execute(
            "SELECT a.* FROM achievements a LEFT JOIN user_achievements ua ON ua.achievement_id = a.id AND ua.user_id = ? WHERE ua.achievement_id IS NULL",
            (user_id,),
        ) as cur2:
            async for a in cur2:
                cond_value = fields.get(a["condition_type"], -1)
                if cond_value >= int(a["condition_value"]):
                    # award
                    await self.conn.execute(
                        "INSERT OR IGNORE INTO user_achievements(user_id, achievement_id, awarded_at) VALUES(?,?,?)",
                        (user_id, a["id"], now_ts()),
                    )
                    await self.conn.execute(
                        "UPDATE users SET diamonds = diamonds + ?, total_earned = total_earned + ? WHERE user_id = ?",
                        (int(a["reward_diamonds"]), int(a["reward_diamonds"]), user_id),
                    )
                    awarded.append(f"🏅 {a['name']} (+{a['reward_diamonds']} 💎)")
        if awarded:
            await self.conn.commit()
        return awarded

    # --------------- Leaderboards ---------------
    async def compute_rate_per_sec(self, user_row: aiosqlite.Row, cfg: Optional[ConfigValues] = None) -> float:
        if cfg is None:
            cfg = await self.get_config()
        miner_count = int(user_row["miner_count"]) or 0
        farm_level = int(user_row["farm_level"]) or 0
        multiplier = (cfg.farm_multiplier_per_level ** farm_level) if farm_level > 0 else 1.0
        return (miner_count * (cfg.miner_base_rate_per_min / 60.0)) * multiplier

    async def top_by_diamonds(self, limit: int) -> List[Tuple[int, int]]:
        assert self.conn is not None
        out: List[Tuple[int, int]] = []
        async with self.conn.execute(
            "SELECT user_id, diamonds FROM users ORDER BY diamonds DESC LIMIT ?",
            (limit,),
        ) as cur:
            async for row in cur:
                out.append((int(row["user_id"]), int(row["diamonds"])) )
        return out

    async def top_by_rate(self, limit: int) -> List[Tuple[int, float]]:
        assert self.conn is not None
        out: List[Tuple[int, float]] = []
        cfg = await self.get_config()
        async with self.conn.execute("SELECT * FROM users") as cur:
            async for row in cur:
                rate = await self.compute_rate_per_sec(row, cfg)
                out.append((int(row["user_id"]), rate))
        out.sort(key=lambda x: x[1], reverse=True)
        return out[:limit]

    # --------------- Admin ops ---------------
    async def add_diamonds(self, actor_id: int, target_id: int, amount: int) -> bool:
        assert self.conn is not None
        await self.conn.execute("UPDATE users SET diamonds = diamonds + ? WHERE user_id = ?", (amount, target_id))
        await self.conn.execute(
            "INSERT INTO audit_logs(ts, action, actor_id, target_id, details) VALUES(?,?,?,?,?)",
            (now_ts(), "add_diamonds", actor_id, target_id, str(amount)),
        )
        await self.conn.commit()
        return True

    async def sub_diamonds(self, actor_id: int, target_id: int, amount: int) -> bool:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE users SET diamonds = MAX(0, diamonds - ?) WHERE user_id = ?",
            (amount, target_id),
        )
        await self.conn.execute(
            "INSERT INTO audit_logs(ts, action, actor_id, target_id, details) VALUES(?,?,?,?,?)",
            (now_ts(), "sub_diamonds", actor_id, target_id, str(amount)),
        )
        await self.conn.commit()
        return True

    async def reset_user(self, actor_id: int, target_id: int) -> bool:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE users SET diamonds=0, gold=0, click_power=1, clicks_total=0, total_earned=0, last_click_window_start=NULL, clicks_in_window=0, last_mine_ts=?, uncollected_mine=0.0, miner_count=0, farm_level=0, daily_streak=0, last_daily_claim=NULL, vip=0 WHERE user_id = ?",
            (now_ts(), target_id),
        )
        await self.conn.execute("DELETE FROM user_achievements WHERE user_id = ?", (target_id,))
        await self.conn.execute(
            "INSERT INTO audit_logs(ts, action, actor_id, target_id, details) VALUES(?,?,?,?,?)",
            (now_ts(), "reset_user", actor_id, target_id, ""),
        )
        await self.conn.commit()
        return True

    async def set_vip(self, actor_id: int, target_id: int, vip: int) -> bool:
        assert self.conn is not None
        await self.conn.execute("UPDATE users SET vip = ? WHERE user_id = ?", (1 if vip else 0, target_id))
        await self.conn.execute(
            "INSERT INTO audit_logs(ts, action, actor_id, target_id, details) VALUES(?,?,?,?,?)",
            (now_ts(), "set_vip", actor_id, target_id, str(vip)),
        )
        await self.conn.commit()
        return True

    async def award_achievement_admin(self, actor_id: int, target_id: int, ach_id: str) -> Tuple[bool, str]:
        assert self.conn is not None
        async with self.conn.execute("SELECT * FROM achievements WHERE id = ?", (ach_id,)) as cur:
            a = await cur.fetchone()
        if a is None:
            return False, "Нет такого достижения"
        await self.conn.execute(
            "INSERT OR IGNORE INTO user_achievements(user_id, achievement_id, awarded_at) VALUES(?,?,?)",
            (target_id, ach_id, now_ts()),
        )
        await self.conn.execute(
            "UPDATE users SET diamonds = diamonds + ?, total_earned = total_earned + ? WHERE user_id = ?",
            (int(a["reward_diamonds"]), int(a["reward_diamonds"]), target_id),
        )
        await self.conn.execute(
            "INSERT INTO audit_logs(ts, action, actor_id, target_id, details) VALUES(?,?,?,?,?)",
            (now_ts(), "award_achievement", actor_id, target_id, ach_id),
        )
        await self.conn.commit()
        return True, f"Выдано: {a['name']} (+{a['reward_diamonds']} 💎)"

    async def list_users(self, page: int, page_size: int = 20) -> List[aiosqlite.Row]:
        assert self.conn is not None
        offset = max(0, (page - 1) * page_size)
        out: List[aiosqlite.Row] = []
        async with self.conn.execute(
            "SELECT user_id, username, first_name, diamonds, gold, miner_count, farm_level FROM users ORDER BY diamonds DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ) as cur:
            async for row in cur:
                out.append(row)
        return out

    async def get_logs(self, limit: int = 20) -> List[aiosqlite.Row]:
        assert self.conn is not None
        out: List[aiosqlite.Row] = []
        async with self.conn.execute(
            "SELECT ts, action, actor_id, target_id, details FROM audit_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            async for row in cur:
                out.append(row)
        return out

    # --------------- Currency conversion ---------------
    async def convert_d2g(self, user_id: int, diamonds: int) -> Tuple[bool, str]:
        if diamonds <= 0:
            return False, "Некорректная сумма"
        async with self.conn.execute("SELECT diamonds, gold FROM users WHERE user_id = ?", (user_id,)) as cur:
            r = await cur.fetchone()
        if r is None:
            return False, "Ошибка профиля"
        if int(r["diamonds"]) < diamonds:
            return False, "Недостаточно 💎"
        cfg = await self.get_config()
        gold_gain = diamonds * int(cfg.diamond_to_gold_rate)
        await self.update_user_fields(user_id, {"diamonds": int(r["diamonds"]) - diamonds, "gold": int(r["gold"]) + gold_gain})
        return True, f"Конвертировано: -{diamonds} 💎 -> +{gold_gain} золота"

    async def convert_g2d(self, user_id: int, gold: int) -> Tuple[bool, str]:
        if gold <= 0:
            return False, "Некорректная сумма"
        async with self.conn.execute("SELECT diamonds, gold FROM users WHERE user_id = ?", (user_id,)) as cur:
            r = await cur.fetchone()
        if r is None:
            return False, "Ошибка профиля"
        if int(r["gold"]) < gold:
            return False, "Недостаточно золота"
        cfg = await self.get_config()
        # inverse rate; ensure at least 1 diamond for enough gold
        diamonds_gain = gold // int(cfg.diamond_to_gold_rate)
        if diamonds_gain <= 0:
            return False, f"Нужно минимум {cfg.diamond_to_gold_rate} золота за 1 💎"
        gold_spent = diamonds_gain * int(cfg.diamond_to_gold_rate)
        await self.update_user_fields(
            user_id,
            {"gold": int(r["gold"]) - gold_spent, "diamonds": int(r["diamonds"]) + diamonds_gain, "total_earned": int(r["diamonds"]) + diamonds_gain},
        )
        # fix total_earned addition: re-read and add properly
        async with self.conn.execute("SELECT total_earned FROM users WHERE user_id = ?", (user_id,)) as cur2:
            row2 = await cur2.fetchone()
        await self.update_user_fields(user_id, {"total_earned": int(row2["total_earned"]) + diamonds_gain})
        return True, f"Конвертировано: -{gold_spent} золота -> +{diamonds_gain} 💎"


db = Database(DB_PATH)
router = Router()


# -------------------------------
# UI helpers
# -------------------------------


def main_menu_kb(is_admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💎 Клик", callback_data="click")
    b.button(text="🏭 Ферма", callback_data="farm")
    b.button(text="🧍 Профиль", callback_data="profile")
    b.button(text="🏆 Топ", callback_data="top")
    b.button(text="🏪 Магазин", callback_data="shop")
    b.button(text="🎁 Бонус", callback_data="daily")
    b.button(text="⚙️ Настройки", callback_data="settings")
    if is_admin:
        b.button(text="🛠 Admin", callback_data="admin")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def farm_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Улучшить оборудование", callback_data="farm_upgrade")
    b.button(text="Автоматический майнинг", callback_data="farm_info")
    b.button(text="Собрать добычу", callback_data="farm_collect")
    b.button(text="⬅️ Назад", callback_data="menu")
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def shop_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Усилить клик", callback_data="buy_click")
    b.button(text="Нанять майнера", callback_data="buy_miner")
    b.button(text="Улучшить ферму", callback_data="buy_farm")
    b.button(text="⬅️ Назад", callback_data="menu")
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def profile_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Показать достижения", callback_data="achievements")
    b.button(text="Изменить ник", callback_data="change_nick")
    b.button(text="⬅️ Назад", callback_data="menu")
    b.adjust(1, 1, 1)
    return b.as_markup()


# -------------------------------
# Handlers
# -------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
    )
    await db.seed_achievements()
    await db.accrue_mining(message.from_user.id)
    await message.answer(
        "Привет! 👋 Добро пожаловать в кликер алмазов.",
        reply_markup=main_menu_kb(message.from_user.id == ADMIN_USER_ID),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery) -> None:
    await db.get_or_create_user(c.from_user.id, c.from_user.username or "", c.from_user.first_name or "")
    await db.accrue_mining(c.from_user.id)
    await c.message.edit_text(
        "Главное меню:",
        reply_markup=main_menu_kb(c.from_user.id == ADMIN_USER_ID),
    )
    await c.answer()


@router.callback_query(F.data == "click")
async def cb_click(c: CallbackQuery) -> None:
    await db.get_or_create_user(c.from_user.id, c.from_user.username or "", c.from_user.first_name or "")
    ok, balance, msg = await db.process_click(c.from_user.id)
    awarded = await db.check_and_award_achievements(c.from_user.id)
    suffix = ("\n" + "\n".join(awarded)) if awarded else ""
    await c.answer(msg)
    await c.message.edit_text(
        f"💎 Баланс: <b>{balance}</b>\nНажимай кнопку, чтобы добывать алмазы!" + suffix,
        reply_markup=main_menu_kb(c.from_user.id == ADMIN_USER_ID),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "farm")
async def cb_farm(c: CallbackQuery) -> None:
    user = await db.accrue_mining(c.from_user.id)
    cfg = await db.get_config()
    rate = await db.compute_rate_per_sec(user, cfg)
    text = (
        "🏭 Ферма\n"
        f"Майнеров: <b>{user['miner_count']}</b>\n"
        f"Уровень фермы: <b>{user['farm_level']}</b>\n"
        f"Накоплено: <b>{int(user['uncollected_mine'])}</b> 💎\n"
        f"Скорость: <b>{rate:.2f}</b> / сек"
    )
    await c.message.edit_text(text, reply_markup=farm_menu_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@router.callback_query(F.data == "farm_collect")
async def cb_farm_collect(c: CallbackQuery) -> None:
    collected, left = await db.collect_mining(c.from_user.id)
    if collected > 0:
        awarded = await db.check_and_award_achievements(c.from_user.id)
        suffix = ("\n" + "\n".join(awarded)) if awarded else ""
        await c.answer(f"Собрано: +{collected} 💎")
    else:
        suffix = ""
        await c.answer("Нечего собирать")
    user = await db.accrue_mining(c.from_user.id)
    cfg = await db.get_config()
    rate = await db.compute_rate_per_sec(user, cfg)
    text = (
        "🏭 Ферма\n"
        f"Накоплено: <b>{int(user['uncollected_mine'])}</b> 💎 (осталось {left:.2f})\n"
        f"Скорость: <b>{rate:.2f}</b> / сек"
    )
    await c.message.edit_text(text, reply_markup=farm_menu_kb(), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "farm_info")
async def cb_farm_info(c: CallbackQuery) -> None:
    user = await db.accrue_mining(c.from_user.id)
    cfg = await db.get_config()
    rate = await db.compute_rate_per_sec(user, cfg)
    text = (
        "⚙️ Автоматический майнинг\n"
        f"Майнеров: <b>{user['miner_count']}</b> | Уровень фермы: <b>{user['farm_level']}</b>\n"
        f"Текущая скорость: <b>{rate:.2f}</b> / сек"
    )
    await c.message.edit_text(text, reply_markup=farm_menu_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@router.callback_query(F.data == "farm_upgrade")
async def cb_farm_upgrade(c: CallbackQuery) -> None:
    user = await db.accrue_mining(c.from_user.id)
    cost_miner = await db.calc_cost_miner(int(user["miner_count"]))
    cost_farm = await db.calc_cost_farm_level(int(user["farm_level"]))
    text = (
        "🔧 Улучшения фермы\n"
        f"Алмазов: <b>{user['diamonds']}</b>\n"
        f"Нанять майнера — <b>{cost_miner}</b> 💎\n"
        f"Улучшить ферму — <b>{cost_farm}</b> 💎"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="Нанять майнера", callback_data="buy_miner")
    kb.button(text="Улучшить ферму", callback_data="buy_farm")
    kb.button(text="⬅️ Назад", callback_data="farm")
    kb.adjust(1, 1, 1)
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@router.callback_query(F.data == "shop")
async def cb_shop(c: CallbackQuery) -> None:
    user = await db.accrue_mining(c.from_user.id)
    cost_click = await db.calc_cost_click_power(int(user["click_power"]))
    cost_miner = await db.calc_cost_miner(int(user["miner_count"]))
    cost_farm = await db.calc_cost_farm_level(int(user["farm_level"]))
    text = (
        "🏪 Магазин\n"
        f"Алмазов: <b>{user['diamonds']}</b>\n"
        f"Усилить клик (+1) — <b>{cost_click}</b> 💎 (текущая сила: {user['click_power']})\n"
        f"Нанять майнера — <b>{cost_miner}</b> 💎 (есть: {user['miner_count']})\n"
        f"Улучшить ферму — <b>{cost_farm}</b> 💎 (уровень: {user['farm_level']})\n"
    )
    await c.message.edit_text(text, reply_markup=shop_menu_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@router.callback_query(F.data == "buy_click")
async def cb_buy_click(c: CallbackQuery) -> None:
    ok, msg = await db.purchase_click_power(c.from_user.id)
    await c.answer("Куплено" if ok else msg, show_alert=not ok)
    await cb_shop(c)


@router.callback_query(F.data == "buy_miner")
async def cb_buy_miner(c: CallbackQuery) -> None:
    ok, msg = await db.purchase_miner(c.from_user.id)
    await c.answer("Куплено" if ok else msg, show_alert=not ok)
    await cb_shop(c)


@router.callback_query(F.data == "buy_farm")
async def cb_buy_farm(c: CallbackQuery) -> None:
    ok, msg = await db.purchase_farm_level(c.from_user.id)
    await c.answer("Куплено" if ok else msg, show_alert=not ok)
    await cb_shop(c)


@router.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery) -> None:
    user = await db.get_or_create_user(c.from_user.id, c.from_user.username or "", c.from_user.first_name or "")
    text = (
        "🧍 Профиль\n"
        f"ID: <code>{c.from_user.id}</code>\n"
        f"Ник: <b>{c.from_user.first_name}</b> (@{c.from_user.username or '—'})\n"
        f"Дата регистрации: <b>{datetime.fromtimestamp(user['created_at'], tz=timezone.utc).strftime('%Y-%m-%d')}</b>\n"
        f"Баланс: <b>{user['diamonds']}</b> 💎\n"
        f"Сила клика: <b>{user['click_power']}</b>\n"
        f"Ферма: ур. <b>{user['farm_level']}</b>, майнеров: <b>{user['miner_count']}</b>\n"
        f"Всего заработано: <b>{user['total_earned']}</b> 💎\n"
    )
    await c.message.edit_text(text, reply_markup=profile_menu_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@router.callback_query(F.data == "achievements")
async def cb_achievements(c: CallbackQuery) -> None:
    # list awarded and available
    assert db.conn is not None
    rows: List[str] = []
    async with db.conn.execute(
        "SELECT a.name, a.description, ua.awarded_at FROM achievements a LEFT JOIN user_achievements ua ON a.id = ua.achievement_id AND ua.user_id = ?",
        (c.from_user.id,),
    ) as cur:
        async for row in cur:
            mark = "✅" if row["awarded_at"] is not None else "⬜"
            rows.append(f"{mark} <b>{row['name']}</b> — {row['description']}")
    text = "🏅 Достижения\n" + ("\n".join(rows) if rows else "Список пуст")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="profile")
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@router.callback_query(F.data == "change_nick")
async def cb_change_nick(c: CallbackQuery) -> None:
    await c.answer("Отправьте команду /nick НовоеИмя")


@router.message(Command("nick"))
async def cmd_nick(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Использование: /nick НовоеИмя")
        return
    new_name = parts[1].strip()[:32]
    await db.update_user_fields(message.from_user.id, {"first_name": new_name})
    await message.reply(f"Имя изменено на: <b>{new_name}</b>", parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "top")
async def cb_top(c: CallbackQuery) -> None:
    cfg = await db.get_config()
    top_d = await db.top_by_diamonds(cfg.leaderboard_size)
    top_r = await db.top_by_rate(cfg.leaderboard_size)
    def fmt_user(uid: int) -> str:
        return f"<code>{uid}</code>"
    lines = ["🏆 Топ игроков по 💎:"]
    for i, (uid, val) in enumerate(top_d, 1):
        lines.append(f"{i}. {fmt_user(uid)} — <b>{val}</b> 💎")
    lines.append("")
    lines.append("⚡️ Топ по скорости майнинга:")
    for i, (uid, rate) in enumerate(top_r, 1):
        lines.append(f"{i}. {fmt_user(uid)} — <b>{rate:.2f}</b>/сек")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="menu")
    await c.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@router.callback_query(F.data == "daily")
async def cb_daily(c: CallbackQuery) -> None:
    ok, msg = await db.claim_daily(c.from_user.id)
    await c.answer(msg, show_alert=not ok)
    await cb_menu(c)


@router.callback_query(F.data == "settings")
async def cb_settings(c: CallbackQuery) -> None:
    await c.message.edit_text("Настройки: используйте команды /d2g <алмазы> или /g2d <золото> для конвертации валют.", reply_markup=main_menu_kb(c.from_user.id == ADMIN_USER_ID))
    await c.answer()


# -------------------------------
# Admin commands
# -------------------------------


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.reply("Недостаточно прав")
        return
    text = (
        "🛠 Админ-панель (команды):\n"
        "/users — статистика пользователей\n"
        "/add <id> <amount> — добавить алмазы\n"
        "/sub <id> <amount> — отнять алмазы\n"
        "/reset <id> — сбросить прогресс\n"
        "/cfglist — показать конфиг\n"
        "/setcfg <key> <value> — изменить конфиг\n"
        "/vip <id> <0/1> — установить VIP\n"
        "/award <id> <ach_id> — выдать достижение\n"
        "/logs — последние операции\n"
    )
    await message.reply(text)


@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    assert db.conn is not None
    async with db.conn.execute("SELECT COUNT(*) AS c FROM users") as cur:
        row = await cur.fetchone()
    await message.reply(f"Пользователей: <b>{row['c']}</b>", parse_mode=ParseMode.HTML)
    # show first page
    users = await db.list_users(page=1)
    lines = []
    for u in users:
        lines.append(f"<code>{u['user_id']}</code> {u['first_name'] or ''} (@{u['username'] or '—'}) — {u['diamonds']}💎, майнеров {u['miner_count']}, ферма {u['farm_level']}")
    if lines:
        await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("add"))
async def cmd_add(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply("Использование: /add <id> <amount>")
        return
    uid = int(parts[1])
    amount = int(parts[2])
    await db.add_diamonds(message.from_user.id, uid, amount)
    await message.reply("Готово")


@router.message(Command("sub"))
async def cmd_sub(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply("Использование: /sub <id> <amount>")
        return
    uid = int(parts[1])
    amount = int(parts[2])
    await db.sub_diamonds(message.from_user.id, uid, amount)
    await message.reply("Готово")


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply("Использование: /reset <id>")
        return
    uid = int(parts[1])
    await db.reset_user(message.from_user.id, uid)
    await message.reply("Сброшено")


@router.message(Command("cfglist"))
async def cmd_cfglist(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    cfg = await db.get_config()
    lines = ["Текущие параметры:"]
    for k, v in cfg.__dict__.items():
        lines.append(f"- {k}: <b>{v}</b>")
    await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("setcfg"))
async def cmd_setcfg(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3:
        await message.reply("Использование: /setcfg <key> <value>")
        return
    key, value = parts[1], parts[2]
    await db.set_config_value(key, value)
    await message.reply("OK")


@router.message(Command("vip"))
async def cmd_vip(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply("Использование: /vip <id> <0|1>")
        return
    uid = int(parts[1])
    val = 1 if parts[2] == "1" else 0
    await db.set_vip(message.from_user.id, uid, val)
    await message.reply("OK")


@router.message(Command("award"))
async def cmd_award(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply("Использование: /award <id> <ach_id>")
        return
    uid = int(parts[1])
    ach_id = parts[2]
    ok, msg = await db.award_achievement_admin(message.from_user.id, uid, ach_id)
    await message.reply(msg if ok else f"Ошибка: {msg}")


@router.message(Command("logs"))
async def cmd_logs(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    logs = await db.get_logs()
    lines = ["Последние операции:"]
    for l in logs:
        ts = datetime.fromtimestamp(l["ts"], tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        lines.append(f"{ts} — {l['action']} actor={l['actor_id']} target={l['target_id']} details={l['details']}")
    await message.reply("\n".join(lines))


@router.message(Command("d2g"))
async def cmd_d2g(message: Message) -> None:
    await db.get_or_create_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "")
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply("Использование: /d2g <кол-во_алмазов>")
        return
    amount = int(parts[1])
    ok, msg = await db.convert_d2g(message.from_user.id, amount)
    await message.reply(msg if ok else f"Ошибка: {msg}")


@router.message(Command("g2d"))
async def cmd_g2d(message: Message) -> None:
    await db.get_or_create_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "")
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply("Использование: /g2d <кол-во_золота>")
        return
    amount = int(parts[1])
    ok, msg = await db.convert_g2d(message.from_user.id, amount)
    await message.reply(msg if ok else f"Ошибка: {msg}")


# -------------------------------
# App entrypoint
# -------------------------------


async def on_startup() -> None:
    await db.connect()


async def main() -> None:
    load_dotenv()
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        print("[WARN] BOT_TOKEN env var is not set. Set BOT_TOKEN to run the bot.")
        return
    await on_startup()
    dp = Dispatcher()
    dp.include_router(router)
    bot = Bot(token=token, parse_mode=ParseMode.HTML)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass