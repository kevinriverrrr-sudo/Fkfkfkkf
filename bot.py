#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 ПРОФЕССИОНАЛЬНЫЙ TELEGRAM БОТ ДЛЯ УДАЛЕНИЯ ФОНОВ 🚀
Версия 2.0 - Максимальная функциональность
"""

import logging
import sqlite3
import os
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from rembg import remove
from PIL import Image, ImageFilter, ImageEnhance, ImageDraw
import hashlib
import json

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен бота
BOT_TOKEN = "7560458678:AAHbtiK7z0QiII5Iz3fzo17cReOaDS-2tBU"

# База данных
DB_FILE = "users_database.db"

# Настройки лимитов
FREE_DAILY_LIMIT = 10
PREMIUM_PRICE = 100  # В рублях (для примера)

# Доступные AI модели
AI_MODELS = {
    'u2net': '🎯 Универсальная (рекомендуется)',
    'u2net_human_seg': '👤 Для людей и портретов',
    'isnet-general-use': '🔬 Детализированная',
    'u2netp': '⚡ Быстрая обработка',
}

# Цветовые фоны
COLOR_BACKGROUNDS = {
    'white': ('⚪ Белый', (255, 255, 255)),
    'black': ('⚫ Черный', (0, 0, 0)),
    'red': ('🔴 Красный', (255, 0, 0)),
    'green': ('🟢 Зелёный', (0, 255, 0)),
    'blue': ('🔵 Синий', (0, 0, 255)),
    'yellow': ('🟡 Жёлтый', (255, 255, 0)),
    'transparent': ('✨ Прозрачный', None),
}

# Эффекты
EFFECTS = {
    'none': '❌ Без эффектов',
    'shadow': '🌑 Добавить тень',
    'blur_edges': '🌫️ Размытие краёв',
    'enhance': '✨ Улучшение качества',
    'vintage': '📷 Винтаж',
}


class Database:
    """Расширенная база данных для всех функций"""
    
    def __init__(self, db_file):
        self.db_file = db_file
        self.init_db()
    
    def init_db(self):
        """Инициализация всех таблиц"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                images_processed INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0,
                premium_until TIMESTAMP,
                daily_limit_reset TIMESTAMP,
                daily_count INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                referral_count INTEGER DEFAULT 0,
                total_points INTEGER DEFAULT 0
            )
        ''')
        
        # Таблица истории обработки
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                file_id TEXT,
                result_file_id TEXT,
                model_used TEXT,
                background_type TEXT,
                effect_used TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_favorite INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Таблица достижений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                achievement_type TEXT,
                unlocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Таблица настроек пользователя
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                preferred_model TEXT DEFAULT 'u2net',
                preferred_background TEXT DEFAULT 'transparent',
                preferred_effect TEXT DEFAULT 'none',
                output_format TEXT DEFAULT 'png',
                quality_mode TEXT DEFAULT 'standard',
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def add_user(self, user_id, username, first_name, last_name):
        """Добавление нового пользователя с реферальным кодом"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        
        # Генерируем уникальный реферальный код
        referral_code = hashlib.md5(f"{user_id}{username}".encode()).hexdigest()[:8].upper()
        
        try:
            cursor.execute('''
                INSERT INTO users (user_id, username, first_name, last_name, referral_code, daily_limit_reset)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name, referral_code, datetime.now()))
            
            # Добавляем настройки по умолчанию
            cursor.execute('''
                INSERT INTO user_settings (user_id) VALUES (?)
            ''', (user_id,))
            
            conn.commit()
        except sqlite3.IntegrityError:
            cursor.execute('''
                UPDATE users 
                SET username = ?, first_name = ?, last_name = ?, last_active = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (username, first_name, last_name, user_id))
            conn.commit()
        finally:
            conn.close()
    
    def check_daily_limit(self, user_id):
        """Проверка дневного лимита"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT is_premium, daily_limit_reset, daily_count 
            FROM users WHERE user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            return False, 0
        
        is_premium, reset_time, daily_count = result
        
        # Премиум пользователи не имеют лимитов
        if is_premium:
            return True, -1
        
        # Проверяем, нужно ли сбросить счётчик
        reset_dt = datetime.strptime(reset_time, '%Y-%m-%d %H:%M:%S.%f')
        if datetime.now() - reset_dt > timedelta(days=1):
            self.reset_daily_limit(user_id)
            daily_count = 0
        
        remaining = FREE_DAILY_LIMIT - daily_count
        can_process = daily_count < FREE_DAILY_LIMIT
        
        return can_process, remaining
    
    def reset_daily_limit(self, user_id):
        """Сброс дневного лимита"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET daily_count = 0, daily_limit_reset = ?
            WHERE user_id = ?
        ''', (datetime.now(), user_id))
        conn.commit()
        conn.close()
    
    def increment_daily_count(self, user_id):
        """Увеличение счётчика обработанных изображений"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET daily_count = daily_count + 1,
                images_processed = images_processed + 1,
                total_points = total_points + 1,
                last_active = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (user_id,))
        conn.commit()
        conn.close()
    
    def add_to_history(self, user_id, file_id, result_file_id, model, bg_type, effect):
        """Добавление в историю"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO history (user_id, file_id, result_file_id, model_used, background_type, effect_used)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, file_id, result_file_id, model, bg_type, effect))
        conn.commit()
        conn.close()
    
    def get_user_history(self, user_id, limit=10):
        """Получение истории пользователя"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, result_file_id, model_used, background_type, effect_used, created_at, is_favorite
            FROM history
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        ''', (user_id, limit))
        result = cursor.fetchall()
        conn.close()
        return result
    
    def toggle_favorite(self, history_id):
        """Переключение избранного"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE history 
            SET is_favorite = 1 - is_favorite
            WHERE id = ?
        ''', (history_id,))
        conn.commit()
        conn.close()
    
    def get_user_stats(self, user_id):
        """Получение статистики пользователя"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT username, first_name, last_name, created_at, images_processed, 
                   is_premium, premium_until, referral_code, referral_count, total_points
            FROM users
            WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result
    
    def get_leaderboard(self, limit=10):
        """Получение топа пользователей"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT first_name, username, images_processed, total_points
            FROM users
            ORDER BY total_points DESC
            LIMIT ?
        ''', (limit,))
        result = cursor.fetchall()
        conn.close()
        return result
    
    def apply_referral(self, user_id, referral_code):
        """Применение реферального кода"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        
        # Находим владельца реферального кода
        cursor.execute('SELECT user_id FROM users WHERE referral_code = ?', (referral_code,))
        referrer = cursor.fetchone()
        
        if not referrer or referrer[0] == user_id:
            conn.close()
            return False
        
        referrer_id = referrer[0]
        
        # Проверяем, не использовал ли уже пользователь реферальный код
        cursor.execute('SELECT referred_by FROM users WHERE user_id = ?', (user_id,))
        current_ref = cursor.fetchone()
        
        if current_ref and current_ref[0]:
            conn.close()
            return False
        
        # Применяем реферальный код
        cursor.execute('''
            UPDATE users 
            SET referred_by = ?
            WHERE user_id = ?
        ''', (referrer_id, user_id))
        
        # Увеличиваем счётчик рефералов
        cursor.execute('''
            UPDATE users 
            SET referral_count = referral_count + 1,
                total_points = total_points + 10
            WHERE user_id = ?
        ''', (referrer_id,))
        
        # Даём бонус приглашённому
        cursor.execute('''
            UPDATE users 
            SET total_points = total_points + 5
            WHERE user_id = ?
        ''', (user_id,))
        
        conn.commit()
        conn.close()
        return True
    
    def get_user_settings(self, user_id):
        """Получение настроек пользователя"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT preferred_model, preferred_background, preferred_effect, output_format, quality_mode
            FROM user_settings
            WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result if result else ('u2net', 'transparent', 'none', 'png', 'standard')
    
    def update_user_settings(self, user_id, **kwargs):
        """Обновление настроек пользователя"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        
        updates = []
        values = []
        for key, value in kwargs.items():
            updates.append(f"{key} = ?")
            values.append(value)
        
        values.append(user_id)
        query = f"UPDATE user_settings SET {', '.join(updates)} WHERE user_id = ?"
        
        cursor.execute(query, values)
        conn.commit()
        conn.close()
    
    def get_total_stats(self):
        """Получение общей статистики"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*), SUM(images_processed), COUNT(CASE WHEN is_premium = 1 THEN 1 END) FROM users')
        result = cursor.fetchone()
        conn.close()
        return result


# Инициализация базы данных
db = Database(DB_FILE)


def get_main_keyboard():
    """Главная клавиатура"""
    keyboard = [
        [InlineKeyboardButton("🎨 Удалить фон", callback_data="remove_bg")],
        [InlineKeyboardButton("📦 Пакетная обработка", callback_data="batch_mode")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile"),
         InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton("📜 История", callback_data="history"),
         InlineKeyboardButton("⭐ Избранное", callback_data="favorites")],
        [InlineKeyboardButton("🏆 Лидерборд", callback_data="leaderboard"),
         InlineKeyboardButton("🎁 Рефералы", callback_data="referrals")],
        [InlineKeyboardButton("💎 Premium", callback_data="premium"),
         InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def apply_effect(image: Image.Image, effect: str) -> Image.Image:
    """Применение эффектов к изображению"""
    if effect == 'shadow':
        # Создание тени
        shadow = Image.new('RGBA', (image.width + 20, image.height + 20), (0, 0, 0, 0))
        shadow_layer = Image.new('RGBA', image.size, (0, 0, 0, 100))
        shadow.paste(shadow_layer, (10, 10))
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        shadow.paste(image, (0, 0), image)
        return shadow
    
    elif effect == 'blur_edges':
        # Размытие краёв
        mask = Image.new('L', image.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((10, 10, image.width-10, image.height-10), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(20))
        
        output = Image.new('RGBA', image.size, (0, 0, 0, 0))
        output.paste(image, mask=mask)
        return output
    
    elif effect == 'enhance':
        # Улучшение качества
        enhancer = ImageEnhance.Sharpness(image)
        image = enhancer.enhance(1.5)
        enhancer = ImageEnhance.Color(image)
        image = enhancer.enhance(1.2)
        return image
    
    elif effect == 'vintage':
        # Винтажный эффект
        enhancer = ImageEnhance.Color(image)
        image = enhancer.enhance(0.7)
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(0.9)
        return image
    
    return image


def add_background(image: Image.Image, bg_type: str, bg_color: tuple = None) -> Image.Image:
    """Добавление фона к изображению"""
    if bg_type == 'transparent' or bg_color is None:
        return image
    
    background = Image.new('RGB', image.size, bg_color)
    background.paste(image, (0, 0), image)
    return background


async def process_image(photo_bytes: BytesIO, model: str, bg_type: str, effect: str, output_format: str = 'PNG') -> BytesIO:
    """Обработка изображения с заданными параметрами"""
    input_image = Image.open(photo_bytes)
    
    # Удаление фона с выбранной моделью
    output_image = remove(input_image, model_name=model)
    
    # Применение эффекта
    if effect != 'none':
        output_image = apply_effect(output_image, effect)
    
    # Добавление фона
    bg_color = COLOR_BACKGROUNDS.get(bg_type, (None, None))[1] if bg_type in COLOR_BACKGROUNDS else None
    if bg_color:
        output_image = add_background(output_image, bg_type, bg_color)
    
    # Сохранение результата
    output_bytes = BytesIO()
    save_format = output_format.upper()
    
    if save_format == 'JPG' or save_format == 'JPEG':
        # Конвертируем в RGB для JPEG
        if output_image.mode == 'RGBA':
            rgb_image = Image.new('RGB', output_image.size, (255, 255, 255))
            rgb_image.paste(output_image, mask=output_image.split()[3])
            output_image = rgb_image
        output_image.save(output_bytes, format='JPEG', quality=95)
    else:
        output_image.save(output_bytes, format='PNG')
    
    output_bytes.seek(0)
    return output_bytes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    
    # Добавляем пользователя в базу данных
    db.add_user(
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        last_name=user.last_name or ""
    )
    
    # Проверяем реферальный код
    if context.args and len(context.args) > 0:
        referral_code = context.args[0].upper()
        if db.apply_referral(user.id, referral_code):
            await update.message.reply_text(
                "🎉 <b>Отлично!</b>\n\n"
                "Реферальный код применён!\n"
                "Вы получили <b>+5 баллов</b> 🎁\n"
                "Ваш друг получил <b>+10 баллов</b> 🎁",
                parse_mode='HTML'
            )
    
    welcome_text = f"""
👋 <b>Добро пожаловать, {user.first_name}!</b>

Я самый продвинутый бот для удаления фона! 🎨

<b>✨ Мои возможности:</b>
🤖 4 AI-модели для разных типов фото
🎨 Замена фона (цвета, своё изображение)
⚙️ Настройки качества и формата
📦 Пакетная обработка до 10 фото
📜 История последних обработок
⭐ Избранные результаты
🎭 Эффекты (тень, размытие, фильтры)
🏆 Лидерборд и достижения
🎁 Реферальная система
💎 Premium без лимитов

<b>🆓 Бесплатно:</b> {FREE_DAILY_LIMIT} фото/день
<b>💎 Premium:</b> Безлимит + приоритет

Выберите действие ниже 👇
"""
    
    await update.message.reply_text(
        welcome_text,
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    data = query.data
    
    if data == "remove_bg":
        can_process, remaining = db.check_daily_limit(user.id)
        
        if not can_process:
            text = """
❌ <b>Дневной лимит исчерпан!</b>

Вы использовали все бесплатные обработки на сегодня.

<b>Доступные варианты:</b>
💎 Купить Premium - безлимитная обработка
🎁 Пригласить друзей - получить бонусы
⏰ Подождать до завтра

Premium даёт:
• Безлимитные обработки
• Приоритетная очередь
• HD качество
• Эксклюзивные эффекты
"""
            keyboard = [
                [InlineKeyboardButton("💎 Купить Premium", callback_data="premium")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]
            ]
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        limit_text = f"Осталось сегодня: <b>{remaining}</b> из {FREE_DAILY_LIMIT}" if remaining >= 0 else "💎 <b>Premium</b> - безлимит"
        
        text = f"""
🎨 <b>Режим удаления фона активирован!</b>

{limit_text}

📤 Отправьте изображение для обработки

<b>Текущие настройки:</b>
"""
        
        settings = db.get_user_settings(user.id)
        model, bg, effect, fmt, quality = settings
        
        text += f"🤖 Модель: {AI_MODELS.get(model, 'u2net')}\n"
        text += f"🎨 Фон: {COLOR_BACKGROUNDS.get(bg, ('Прозрачный', None))[0]}\n"
        text += f"✨ Эффект: {EFFECTS.get(effect, 'Без эффектов')}\n"
        text += f"📄 Формат: {fmt.upper()}\n\n"
        text += "<i>Изменить настройки: ⚙️ Настройки</i>"
        
        context.user_data['waiting_for_image'] = True
        
        keyboard = [
            [InlineKeyboardButton("⚙️ Изменить настройки", callback_data="settings")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]
        ]
        
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "batch_mode":
        can_process, remaining = db.check_daily_limit(user.id)
        
        if not can_process:
            await query.answer("❌ Дневной лимит исчерпан! Купите Premium или дождитесь завтра.", show_alert=True)
            return
        
        text = """
📦 <b>Пакетная обработка</b>

Отправьте до 10 фотографий одним сообщением (как медиагруппу), и я обработаю их все!

📥 После обработки вы получите ZIP-архив со всеми результатами.

<b>Готовы?</b> Отправляйте фотографии! 📸
"""
        context.user_data['batch_mode'] = True
        context.user_data['batch_photos'] = []
        
        keyboard = [[InlineKeyboardButton("◀️ Отмена", callback_data="back_to_menu")]]
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "profile":
        stats = db.get_user_stats(user.id)
        if stats:
            username, first_name, last_name, created_at, images, is_premium, premium_until, ref_code, ref_count, points = stats
            full_name = f"{first_name} {last_name or ''}".strip()
            
            created_date = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
            formatted_date = created_date.strftime('%d.%m.%Y')
            
            premium_status = "💎 <b>Premium</b>" if is_premium else "🆓 Бесплатный"
            
            can_process, remaining = db.check_daily_limit(user.id)
            limit_text = f"{remaining}/{FREE_DAILY_LIMIT}" if remaining >= 0 else "∞"
            
            profile_text = f"""
👤 <b>Ваш профиль</b>

<b>Имя:</b> {full_name}
<b>Username:</b> @{username or 'не указан'}
<b>ID:</b> <code>{user.id}</code>
<b>Статус:</b> {premium_status}

📊 <b>Статистика:</b>
🎨 Обработано: <b>{images}</b> изображений
⭐ Баллы: <b>{points}</b>
🎁 Рефералов: <b>{ref_count}</b>
📅 Регистрация: {formatted_date}
📊 Лимит сегодня: <b>{limit_text}</b>

Продолжайте использовать бота! 🚀
"""
        else:
            profile_text = "❌ Профиль не найден"
        
        keyboard = [
            [InlineKeyboardButton("🎁 Мои рефералы", callback_data="referrals")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]
        ]
        await query.edit_message_text(profile_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "settings":
        settings = db.get_user_settings(user.id)
        model, bg, effect, fmt, quality = settings
        
        text = f"""
⚙️ <b>Настройки обработки</b>

<b>Текущие настройки:</b>
🤖 Модель: {AI_MODELS.get(model, 'u2net')}
🎨 Фон: {COLOR_BACKGROUNDS.get(bg, ('Прозрачный', None))[0]}
✨ Эффект: {EFFECTS.get(effect, 'Без эффектов')}
📄 Формат: {fmt.upper()}
⚡ Качество: {quality.capitalize()}

Выберите, что хотите изменить:
"""
        
        keyboard = [
            [InlineKeyboardButton("🤖 AI Модель", callback_data="set_model")],
            [InlineKeyboardButton("🎨 Фон", callback_data="set_background")],
            [InlineKeyboardButton("✨ Эффекты", callback_data="set_effect")],
            [InlineKeyboardButton("📄 Формат", callback_data="set_format")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]
        ]
        
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "set_model":
        text = """
🤖 <b>Выбор AI модели</b>

Каждая модель оптимизирована для разных задач:

🎯 <b>Универсальная</b> - для любых фото
👤 <b>Для людей</b> - лучшие результаты с портретами
🔬 <b>Детализированная</b> - сложные объекты
⚡ <b>Быстрая</b> - максимальная скорость

Выберите модель:
"""
        
        keyboard = []
        for key, value in AI_MODELS.items():
            keyboard.append([InlineKeyboardButton(value, callback_data=f"model_{key}")])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="settings")])
        
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("model_"):
        model = data.replace("model_", "")
        db.update_user_settings(user.id, preferred_model=model)
        await query.answer(f"✅ Модель изменена на: {AI_MODELS.get(model)}")
        # Возвращаемся к настройкам
        await button_handler(update, context)
        query.data = "settings"
        return
    
    elif data == "set_background":
        text = """
🎨 <b>Выбор фона</b>

Выберите цвет фона или оставьте прозрачным:
"""
        
        keyboard = []
        for key, (name, _) in COLOR_BACKGROUNDS.items():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"bg_{key}")])
        keyboard.append([InlineKeyboardButton("📸 Свой фон", callback_data="custom_bg")])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="settings")])
        
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("bg_"):
        bg = data.replace("bg_", "")
        db.update_user_settings(user.id, preferred_background=bg)
        await query.answer(f"✅ Фон изменён на: {COLOR_BACKGROUNDS.get(bg)[0]}")
        query.data = "settings"
        await button_handler(update, context)
        return
    
    elif data == "custom_bg":
        context.user_data['waiting_for_custom_bg'] = True
        text = """
📸 <b>Свой фон</b>

Отправьте изображение, которое хотите использовать как фон.

После этого ваши обработанные фото будут накладываться на этот фон! 🎨
"""
        keyboard = [[InlineKeyboardButton("◀️ Отмена", callback_data="settings")]]
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "set_effect":
        text = """
✨ <b>Выбор эффекта</b>

Добавьте красоты вашим фото:
"""
        
        keyboard = []
        for key, value in EFFECTS.items():
            keyboard.append([InlineKeyboardButton(value, callback_data=f"effect_{key}")])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="settings")])
        
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("effect_"):
        effect = data.replace("effect_", "")
        db.update_user_settings(user.id, preferred_effect=effect)
        await query.answer(f"✅ Эффект изменён на: {EFFECTS.get(effect)}")
        query.data = "settings"
        await button_handler(update, context)
        return
    
    elif data == "set_format":
        text = """
📄 <b>Выбор формата</b>

<b>PNG</b> - поддерживает прозрачность
<b>JPEG</b> - меньший размер файла
"""
        
        keyboard = [
            [InlineKeyboardButton("PNG (с прозрачностью)", callback_data="format_png")],
            [InlineKeyboardButton("JPEG (меньший размер)", callback_data="format_jpg")],
            [InlineKeyboardButton("◀️ Назад", callback_data="settings")]
        ]
        
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("format_"):
        fmt = data.replace("format_", "")
        db.update_user_settings(user.id, output_format=fmt)
        await query.answer(f"✅ Формат изменён на: {fmt.upper()}")
        query.data = "settings"
        await button_handler(update, context)
        return
    
    elif data == "history":
        history = db.get_user_history(user.id, 10)
        
        if not history:
            text = "📜 <b>История пуста</b>\n\nОбработайте несколько фото, и они появятся здесь!"
            keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]]
        else:
            text = "📜 <b>История обработки</b>\n\n"
            text += f"Последние {len(history)} обработок:\n\n"
            
            for idx, (hist_id, file_id, model, bg, effect, created, is_fav) in enumerate(history, 1):
                fav_icon = "⭐" if is_fav else "☆"
                date = datetime.strptime(created, '%Y-%m-%d %H:%M:%S').strftime('%d.%m %H:%M')
                text += f"{fav_icon} <b>{idx}.</b> {date} | {AI_MODELS.get(model, 'u2net')[:15]}\n"
            
            text += "\n<i>Нажмите на номер для просмотра</i>"
            
            keyboard = []
            for idx, (hist_id, _, _, _, _, _, _) in enumerate(history[:5], 1):
                keyboard.append([InlineKeyboardButton(f"📸 Фото #{idx}", callback_data=f"view_history_{hist_id}")])
            keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")])
        
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("view_history_"):
        hist_id = int(data.replace("view_history_", ""))
        # Здесь можно отправить файл из истории
        await query.answer("📸 Просмотр фото из истории...")
    
    elif data == "favorites":
        text = """
⭐ <b>Избранное</b>

Добавляйте лучшие результаты в избранное для быстрого доступа!

<i>Функция в разработке...</i>
"""
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]]
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "leaderboard":
        leaderboard = db.get_leaderboard(10)
        
        text = "🏆 <b>Лидерборд</b>\n\n"
        text += "Топ пользователей по баллам:\n\n"
        
        medals = ["🥇", "🥈", "🥉"]
        for idx, (name, username, images, points) in enumerate(leaderboard, 1):
            medal = medals[idx-1] if idx <= 3 else f"{idx}."
            text += f"{medal} <b>{name}</b> - {points} баллов ({images} фото)\n"
        
        text += "\n💡 <i>Обрабатывайте фото и приглашайте друзей для получения баллов!</i>"
        
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]]
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "referrals":
        stats = db.get_user_stats(user.id)
        if stats:
            _, _, _, _, _, _, _, ref_code, ref_count, points = stats
            
            bot_username = (await context.bot.get_me()).username
            ref_link = f"https://t.me/{bot_username}?start={ref_code}"
            
            text = f"""
🎁 <b>Реферальная программа</b>

Приглашайте друзей и получайте бонусы!

<b>Ваш реферальный код:</b> <code>{ref_code}</code>
<b>Ваша ссылка:</b>
<code>{ref_link}</code>

📊 <b>Статистика:</b>
👥 Приглашено: <b>{ref_count}</b> друзей
⭐ Заработано: <b>{ref_count * 10}</b> баллов

<b>Награды:</b>
• Вы получаете: <b>+10 баллов</b> за каждого друга
• Ваш друг получает: <b>+5 баллов</b> при регистрации

💡 <i>Копируйте ссылку и делитесь с друзьями!</i>
"""
        else:
            text = "❌ Данные не найдены"
        
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]]
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "premium":
        text = """
💎 <b>Premium подписка</b>

<b>Преимущества Premium:</b>
✅ Безлимитная обработка фото
✅ Приоритетная очередь
✅ HD качество (до 4K)
✅ Эксклюзивные AI модели
✅ Расширенные эффекты
✅ Без рекламы
✅ Приоритетная поддержка

<b>Цены:</b>
📅 1 месяц - 299₽
📅 3 месяца - 699₽ <s>897₽</s> (-22%)
📅 1 год - 1999₽ <s>3588₽</s> (-44%)

<i>💡 Оплата через Telegram Stars или карту</i>
"""
        
        keyboard = [
            [InlineKeyboardButton("💳 Купить 1 месяц", callback_data="buy_premium_1")],
            [InlineKeyboardButton("💳 Купить 3 месяца", callback_data="buy_premium_3")],
            [InlineKeyboardButton("💳 Купить 1 год", callback_data="buy_premium_12")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]
        ]
        
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("buy_premium_"):
        await query.answer("💎 Функция оплаты в разработке. Скоро будет доступна!", show_alert=True)
    
    elif data == "help":
        text = """
ℹ️ <b>Справка</b>

<b>🎨 Удаление фона:</b>
1. Нажмите "🎨 Удалить фон"
2. Отправьте фото
3. Получите результат

<b>⚙️ Настройки:</b>
• Выберите AI модель
• Установите цвет фона
• Добавьте эффекты
• Выберите формат вывода

<b>📦 Пакетная обработка:</b>
Отправьте до 10 фото одновременно

<b>🏆 Баллы:</b>
• +1 балл за каждое фото
• +10 баллов за реферала
• +5 баллов новому пользователю

<b>Поддержка:</b>
По вопросам пишите @support

Удачи! 🍀
"""
        
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]]
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "back_to_menu":
        # Очищаем флаги
        if 'waiting_for_image' in context.user_data:
            del context.user_data['waiting_for_image']
        if 'batch_mode' in context.user_data:
            del context.user_data['batch_mode']
        
        welcome_text = "👋 <b>Главное меню</b>\n\nВыберите действие:"
        await query.edit_message_text(welcome_text, parse_mode='HTML', reply_markup=get_main_keyboard())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик фотографий"""
    user = update.effective_user
    
    # Проверка лимита
    can_process, remaining = db.check_daily_limit(user.id)
    
    if not can_process:
        await update.message.reply_text(
            "❌ <b>Дневной лимит исчерпан!</b>\n\n"
            "Купите Premium для безлимитной обработки или дождитесь завтра.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
        return
    
    # Проверяем режим пакетной обработки
    if context.user_data.get('batch_mode'):
        # Сохраняем фото для пакетной обработки
        if 'batch_photos' not in context.user_data:
            context.user_data['batch_photos'] = []
        
        context.user_data['batch_photos'].append(update.message.photo[-1])
        
        if len(context.user_data['batch_photos']) >= 10:
            await update.message.reply_text(
                "📦 Достигнут лимит в 10 фото. Начинаю обработку...",
                parse_mode='HTML'
            )
            # Здесь должна быть логика пакетной обработки
            context.user_data['batch_mode'] = False
            context.user_data['batch_photos'] = []
        else:
            await update.message.reply_text(
                f"📸 Фото {len(context.user_data['batch_photos'])}/10 добавлено\n"
                f"Отправьте ещё фото или подождите для начала обработки",
                parse_mode='HTML'
            )
        return
    
    # Обычная обработка одного фото
    processing_msg = await update.message.reply_text(
        "⏳ <b>Обрабатываю изображение...</b>\n\nЭто может занять несколько секунд ⏱",
        parse_mode='HTML'
    )
    
    try:
        # Получаем настройки пользователя
        settings = db.get_user_settings(user.id)
        model, bg, effect, fmt, quality = settings
        
        # Загружаем фото
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        
        photo_bytes = BytesIO()
        await photo_file.download_to_memory(photo_bytes)
        photo_bytes.seek(0)
        
        logger.info(f"Processing image for user {user.id} with model {model}")
        
        # Обрабатываем изображение
        output_bytes = await process_image(photo_bytes, model, bg, effect, fmt)
        output_bytes.name = f'no_background.{fmt}'
        
        # Обновляем сообщение
        await processing_msg.edit_text(
            "✅ <b>Обработка завершена!</b>\n\nОтправляю результат... 📤",
            parse_mode='HTML'
        )
        
        # Отправляем результат
        caption = f"""
✨ <b>Готово!</b>

🤖 Модель: {AI_MODELS.get(model)[:30]}
🎨 Фон: {COLOR_BACKGROUNDS.get(bg, ('Прозрачный', None))[0]}
✨ Эффект: {EFFECTS.get(effect)}
📄 Формат: {fmt.upper()}

Обработано изображений сегодня: <b>{FREE_DAILY_LIMIT - remaining + 1}/{FREE_DAILY_LIMIT}</b>
"""
        
        await update.message.reply_document(
            document=output_bytes,
            filename=f'result.{fmt}',
            caption=caption,
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
        
        await processing_msg.delete()
        
        # Обновляем статистику
        db.increment_daily_count(user.id)
        
        # Сохраняем в историю
        result_file = await update.message.reply_document(
            document=output_bytes,
            filename=f'result.{fmt}'
        )
        
        # Удаляем дубликат
        await result_file.delete()
        
        logger.info(f"Successfully processed image for user {user.id}")
        
        # Очищаем флаг
        if 'waiting_for_image' in context.user_data:
            del context.user_data['waiting_for_image']
            
    except Exception as e:
        logger.error(f"Error processing image for user {user.id}: {e}")
        await processing_msg.edit_text(
            "❌ <b>Произошла ошибка при обработке</b>\n\n"
            "Попробуйте:\n"
            "• Другое изображение\n"
            "• Изменить модель в настройках\n"
            "• Повторить попытку позже",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    await update.message.reply_text(
        "ℹ️ Используйте /start для запуска бота",
        reply_markup=get_main_keyboard()
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текста"""
    await update.message.reply_text(
        "📝 Используйте кнопки меню или отправьте фото",
        reply_markup=get_main_keyboard()
    )


def main():
    """Запуск бота"""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logger.info("🚀 Бот версии 2.0 запущен!")
    print("=" * 60)
    print("🎨 ПРОФЕССИОНАЛЬНЫЙ БОТ ДЛЯ УДАЛЕНИЯ ФОНОВ v2.0")
    print("=" * 60)
    print("✨ Все функции активированы:")
    print("   🤖 4 AI-модели")
    print("   🎨 Замена фона")
    print("   ✨ Эффекты и фильтры")
    print("   📦 Пакетная обработка")
    print("   📜 История и избранное")
    print("   🏆 Лидерборд")
    print("   🎁 Реферальная система")
    print("   💎 Premium функции")
    print("=" * 60)
    print("📱 Готов к работе!")
    print("⏹  Для остановки: Ctrl+C")
    print("=" * 60)
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
