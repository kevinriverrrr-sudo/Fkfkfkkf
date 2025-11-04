#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Профессиональный Telegram бот для удаления фонов с изображений
Использует AI-модель для качественного удаления фона
"""

import logging
import sqlite3
import os
from datetime import datetime
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from rembg import remove
from PIL import Image

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


class Database:
    """Класс для работы с базой данных пользователей"""
    
    def __init__(self, db_file):
        self.db_file = db_file
        self.init_db()
    
    def init_db(self):
        """Инициализация базы данных"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                images_processed INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()
    
    def add_user(self, user_id, username, first_name, last_name):
        """Добавление нового пользователя"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name))
            conn.commit()
        except sqlite3.IntegrityError:
            # Пользователь уже существует, обновляем информацию
            cursor.execute('''
                UPDATE users 
                SET username = ?, first_name = ?, last_name = ?, last_active = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (username, first_name, last_name, user_id))
            conn.commit()
        finally:
            conn.close()
    
    def increment_images(self, user_id):
        """Увеличение счётчика обработанных изображений"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET images_processed = images_processed + 1, last_active = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (user_id,))
        conn.commit()
        conn.close()
    
    def get_user_stats(self, user_id):
        """Получение статистики пользователя"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT username, first_name, last_name, created_at, images_processed
            FROM users
            WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result
    
    def get_total_stats(self):
        """Получение общей статистики"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*), SUM(images_processed) FROM users')
        result = cursor.fetchone()
        conn.close()
        return result


# Инициализация базы данных
db = Database(DB_FILE)


def get_main_keyboard():
    """Создание главной клавиатуры"""
    keyboard = [
        [InlineKeyboardButton("🎨 Удалить фон", callback_data="remove_bg")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton("📊 Статистика бота", callback_data="stats")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)


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
    
    welcome_text = f"""
👋 <b>Привет, {user.first_name}!</b>

Я профессиональный бот для удаления фона с изображений! 🎨

<b>Что я умею:</b>
✨ Удалять фон с любых фотографий
🤖 Использую AI для высокого качества
⚡ Быстрая обработка изображений
📊 Отслеживание вашей статистики

<b>Как использовать:</b>
1️⃣ Нажмите кнопку "🎨 Удалить фон"
2️⃣ Отправьте мне изображение
3️⃣ Получите фото с прозрачным фоном!

Готовы начать? Выберите действие ниже 👇
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
    
    if query.data == "remove_bg":
        text = """
🎨 <b>Режим удаления фона активирован!</b>

📤 Отправьте мне изображение, и я удалю с него фон.

<b>Поддерживаемые форматы:</b>
• JPG/JPEG
• PNG
• WEBP

<b>Советы для лучшего результата:</b>
✅ Используйте четкие изображения
✅ Объект должен быть хорошо освещен
✅ Избегайте слишком мелких деталей

Жду ваше изображение! 📸
"""
        context.user_data['waiting_for_image'] = True
        await query.edit_message_text(text, parse_mode='HTML')
        
    elif query.data == "profile":
        stats = db.get_user_stats(user.id)
        if stats:
            username, first_name, last_name, created_at, images_processed = stats
            full_name = f"{first_name} {last_name or ''}".strip()
            
            # Форматируем дату регистрации
            created_date = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
            formatted_date = created_date.strftime('%d.%m.%Y')
            
            profile_text = f"""
👤 <b>Ваш профиль</b>

<b>Имя:</b> {full_name}
<b>Username:</b> @{username or 'не указан'}
<b>ID:</b> <code>{user.id}</code>

📊 <b>Статистика:</b>
🎨 Обработано изображений: <b>{images_processed}</b>
📅 Дата регистрации: {formatted_date}

Продолжайте использовать бота! 🚀
"""
        else:
            profile_text = "❌ Профиль не найден. Попробуйте /start"
        
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]]
        await query.edit_message_text(
            profile_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    elif query.data == "stats":
        total_stats = db.get_total_stats()
        total_users, total_images = total_stats
        total_images = total_images or 0
        
        stats_text = f"""
📊 <b>Статистика бота</b>

👥 Всего пользователей: <b>{total_users}</b>
🎨 Обработано изображений: <b>{total_images}</b>
⚡ Статус: <b>Активен</b>

Присоединяйтесь к нашему сообществу! 🌟
"""
        
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]]
        await query.edit_message_text(
            stats_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    elif query.data == "help":
        help_text = """
ℹ️ <b>Справка по использованию</b>

<b>Основные команды:</b>
/start - Запустить бота
/help - Показать эту справку

<b>Как удалить фон:</b>
1. Нажмите "🎨 Удалить фон"
2. Отправьте изображение
3. Дождитесь обработки
4. Скачайте результат в PNG

<b>Особенности:</b>
• Результат сохраняется в PNG с прозрачностью
• Обработка занимает 5-30 секунд
• Качество зависит от исходного изображения

<b>Проблемы?</b>
Попробуйте отправить другое изображение или обратитесь к администратору.

Удачи! 🍀
"""
        
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]]
        await query.edit_message_text(
            help_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    elif query.data == "back_to_menu":
        welcome_text = f"""
👋 <b>Главное меню</b>

Выберите действие ниже 👇
"""
        await query.edit_message_text(
            welcome_text,
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик получения фотографии"""
    user = update.effective_user
    
    # Отправляем сообщение о начале обработки
    processing_msg = await update.message.reply_text(
        "⏳ <b>Обрабатываю изображение...</b>\n\nЭто может занять несколько секунд ⏱",
        parse_mode='HTML'
    )
    
    try:
        # Получаем файл с наибольшим разрешением
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        
        # Загружаем изображение в память
        photo_bytes = BytesIO()
        await photo_file.download_to_memory(photo_bytes)
        photo_bytes.seek(0)
        
        # Удаляем фон с помощью rembg
        logger.info(f"Processing image for user {user.id}")
        input_image = Image.open(photo_bytes)
        
        # Применяем удаление фона
        output_image = remove(input_image)
        
        # Сохраняем результат в память
        output_bytes = BytesIO()
        output_image.save(output_bytes, format='PNG')
        output_bytes.seek(0)
        output_bytes.name = 'no_background.png'
        
        # Обновляем сообщение о процессе
        await processing_msg.edit_text(
            "✅ <b>Обработка завершена!</b>\n\nОтправляю результат... 📤",
            parse_mode='HTML'
        )
        
        # Отправляем результат
        await update.message.reply_document(
            document=output_bytes,
            filename='no_background.png',
            caption="✨ <b>Готово!</b>\n\nФон успешно удален. Изображение сохранено в формате PNG с прозрачным фоном.\n\n🎨 Хотите обработать еще одно изображение?",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
        
        # Удаляем сообщение о процессе
        await processing_msg.delete()
        
        # Обновляем статистику пользователя
        db.increment_images(user.id)
        
        logger.info(f"Successfully processed image for user {user.id}")
        
        # Очищаем флаг ожидания
        if 'waiting_for_image' in context.user_data:
            del context.user_data['waiting_for_image']
            
    except Exception as e:
        logger.error(f"Error processing image for user {user.id}: {e}")
        await processing_msg.edit_text(
            "❌ <b>Произошла ошибка при обработке изображения</b>\n\n"
            "Возможные причины:\n"
            "• Изображение повреждено\n"
            "• Формат не поддерживается\n"
            "• Технические неполадки\n\n"
            "Попробуйте отправить другое изображение или повторите попытку позже.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_text = """
ℹ️ <b>Справка по использованию</b>

<b>Основные команды:</b>
/start - Запустить бота
/help - Показать эту справку

<b>Как удалить фон:</b>
1. Нажмите "🎨 Удалить фон"
2. Отправьте изображение
3. Дождитесь обработки
4. Скачайте результат в PNG

<b>Особенности:</b>
• Результат сохраняется в PNG с прозрачностью
• Обработка занимает 5-30 секунд
• Качество зависит от исходного изображения

Удачи! 🍀
"""
    
    await update.message.reply_text(
        help_text,
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    await update.message.reply_text(
        "📝 Я понимаю только команды и изображения.\n\n"
        "Используйте кнопки меню или отправьте изображение для обработки.",
        reply_markup=get_main_keyboard()
    )


def main():
    """Запуск бота"""
    # Создаём приложение
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Запускаем бота
    logger.info("🚀 Бот запущен и готов к работе!")
    print("=" * 50)
    print("🎨 БОТ ДЛЯ УДАЛЕНИЯ ФОНА ЗАПУЩЕН!")
    print("=" * 50)
    print("📱 Готов обрабатывать изображения...")
    print("⏹  Для остановки нажмите Ctrl+C")
    print("=" * 50)
    
    # Запускаем polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
