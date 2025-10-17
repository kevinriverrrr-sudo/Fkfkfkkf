import requests
import time
import json
from threading import Thread

TOKEN = "8337922006:AAEFTWqohFwFez5K4GkU8cvMkOFxIHCS__4"  # ← вставь сюда свой токен
URL = f"https://api.telegram.org/bot{TOKEN}/"
DB_FILE = "codes.json"

# --- Функции для работы с базой данных ---
def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"codes": [], "stats": {}}

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

db = load_db()

# --- Telegram API helpers ---
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(URL + "sendMessage", data=payload)

def get_updates(offset=None):
    params = {"timeout": 100, "offset": offset}
    r = requests.get(URL + "getUpdates", params=params)
    return r.json()

# --- Основная логика ---
def main():
    offset = None
    while True:
        updates = get_updates(offset)
        if "result" in updates:
            for update in updates["result"]:
                offset = update["update_id"] + 1
                if "message" in update:
                    handle_message(update["message"])
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])

def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user = msg["from"]
    text = msg.get("text", "")
    name = user.get("first_name", "Пользователь")
    username = user.get("username", "")

    if text == "/start":
        keyboard = {
            "inline_keyboard": [
                [{"text": "📥 Получить код", "callback_data": "get_code"}],
                [{"text": "📤 Поделиться кодом", "callback_data": "share_code"}],
                [{"text": "🏆 Список почёта", "callback_data": "top"}],
            ]
        }
        send_message(chat_id, "Привет! 👋 Это обменник кодами Sora 2.\nВыбери действие:", keyboard)
        return

    # Если пользователь отправил свой код
    if chat_id in waiting_for_code:
        code = text.strip().upper()
        db["codes"].append({"code": code, "name": name, "username": username})
        db["stats"][str(chat_id)] = db["stats"].get(str(chat_id), 0) + 1
        save_db(db)
        waiting_for_code.remove(chat_id)
        send_message(chat_id, "✅ Код успешно добавлен! Спасибо за помощь ❤️")
        return

def handle_callback(query):
    chat_id = query["message"]["chat"]["id"]
    data = query["data"]
    user = query["from"]
    name = user.get("first_name", "")
    username = user.get("username", "")

    if data == "get_code":
        if not db["codes"]:
            send_message(chat_id, "❌ Сейчас нет доступных кодов. Поделись своим, чтобы помочь другим!")
            return

        code_entry = db["codes"][0]
        code = code_entry["code"]
        sharer_name = code_entry["name"]
        sharer_username = code_entry["username"]

        text = (
            f"Сейчас ты можешь получить код, потому что кто-то другой им поделился с тобой.\n\n"
            f"Твой код: <b>{code}</b>\n\n"
            f"👤 Кодом поделился: {sharer_name} (@{sharer_username})\n\n"
            f"📱 Инструкция по установке:\n"
            f"Смени регион App Store на США (инструкция: https://t-j.ru/apple-region/)\n"
            f"Скачай приложение: https://apps.apple.com/us/app/sora-by-openai/id6744034028\n"
            f"Для использования может понадобиться VPN\n"
            f"💬 Если есть вопросы — задай их @Tesfjcvv\n\n"
            f"Если всё ок — нажми 'Спасибо, работает!', если нет — 'Код недействителен', и выдадим новый."
        )

        keyboard = {
            "inline_keyboard": [
                [{"text": "✅ Спасибо, работает!", "callback_data": f"valid_{code}"}],
                [{"text": "❌ Код недействителен", "callback_data": f"invalid_{code}"}],
            ]
        }
        send_message(chat_id, text, keyboard)

    elif data == "share_code":
        waiting_for_code.add(chat_id)
        send_message(chat_id, "Отправь свой код одним сообщением. Спасибо за помощь другим пользователям!")

    elif data.startswith("valid_"):
        code = data.split("_", 1)[1]
        send_message(chat_id, "🎉 Отлично! Код подтверждён.")
        # Ничего не удаляем, код остаётся

    elif data.startswith("invalid_"):
        code = data.split("_", 1)[1]
        db["codes"] = [c for c in db["codes"] if c["code"] != code]
        save_db(db)
        send_message(chat_id, "🚫 Код удалён как недействительный.")

    elif data == "top":
        if not db["stats"]:
            send_message(chat_id, "Пока никто не делился кодами 😢")
            return
        sorted_stats = sorted(db["stats"].items(), key=lambda x: x[1], reverse=True)
        text = "🏆 <b>Список почёта:</b>\n\n"
        for i, (uid, count) in enumerate(sorted_stats[:10], 1):
            text += f"{i}. <b>{count} код(ов)</b>\n"
        send_message(chat_id, text)

# --- запуск ---
waiting_for_code = set()
print("Бот запущен...")
Thread(target=main).start()