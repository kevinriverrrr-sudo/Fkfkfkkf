import os
import re
import json
import time
import tempfile
import traceback
from threading import Thread
from typing import Optional, Tuple, List

import requests
from yt_dlp import YoutubeDL

# === Configuration ===
TOKEN = "8108111206:AAFYUUUitrvmXfqIatDNPsu6HhrKX-0toqo"  # Telegram bot token
ADMIN_ID = 8108111206  # Admin (creator) user id
URL = f"https://api.telegram.org/bot{TOKEN}/"
DB_FILE = "stats.json"

# Use a conservative margin to avoid hitting Telegram API upload limits
MAX_UPLOAD_BYTES = 49 * 1024 * 1024  # 49 MB

# === Simple JSON storage ===

def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "users": {},  # chat_id -> successful downloads
            "downloads_success": 0,
            "downloads_failed": 0,
        }


def save_db(data):
    tmp_path = DB_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, DB_FILE)


db = load_db()

# === Telegram API helpers ===

def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        # Disable link previews for cleaner messages
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(URL + "sendMessage", data=payload, timeout=30)
    except Exception:
        pass


def send_chat_action(chat_id: int, action: str):
    try:
        requests.post(
            URL + "sendChatAction",
            data={"chat_id": chat_id, "action": action},
            timeout=30,
        )
    except Exception:
        pass


def send_video(chat_id: int, video_path: str, caption: Optional[str] = None) -> bool:
    try:
        with open(video_path, "rb") as f:
            files = {"video": (os.path.basename(video_path), f)}
            data = {
                "chat_id": chat_id,
                "supports_streaming": True,
            }
            if caption:
                data["caption"] = caption
            r = requests.post(URL + "sendVideo", data=data, files=files, timeout=600)
            try:
                resp = r.json()
            except Exception:
                resp = {"ok": False}
            return r.status_code == 200 and bool(resp.get("ok"))
    except Exception:
        return False


def send_document(chat_id: int, file_path: str, caption: Optional[str] = None) -> bool:
    try:
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f)}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            r = requests.post(URL + "sendDocument", data=data, files=files, timeout=600)
            try:
                resp = r.json()
            except Exception:
                resp = {"ok": False}
            return r.status_code == 200 and bool(resp.get("ok"))
    except Exception:
        return False


def get_updates(offset: Optional[int] = None):
    try:
        params = {"timeout": 60, "offset": offset}
        r = requests.get(URL + "getUpdates", params=params, timeout=90)
        return r.json()
    except Exception:
        return {"ok": False, "result": []}


# === YouTube download helpers ===

def extract_youtube_url(text: str) -> Optional[str]:
    if not text:
        return None
    # Basic YouTube URL matcher
    m = re.search(r"(https?://(?:www\.)?(?:youtube\.com/watch\?[^\s]+|youtu\.be/[^\s]+))", text)
    return m.group(1) if m else None


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def candidate_formats() -> List[str]:
    # Try to avoid requiring ffmpeg by preferring progressive MP4 first
    return [
        # Prefer progressive MP4 under size if size is known
        "b[ext=mp4][filesize<=48500000]/b[ext=mp4]",
        # Cap resolution to keep sizes reasonable
        "b[ext=mp4][height<=480]/b[height<=480]/b",
        "b[ext=mp4][height<=360]/b[height<=360]/b",
        # Separate streams fallback (may require ffmpeg; still try)
        "bv*[ext=mp4][height<=480]+ba[ext=m4a]/bv*[height<=480]+ba/best",
        # Last resort: best available
        "best",
    ]


def resolve_downloaded_path(info: dict, ydl: YoutubeDL, tmpdir: str) -> Optional[str]:
    paths: List[str] = []

    # 1) Try requested_downloads filepaths
    for key in ("requested_downloads",):
        if key in info and isinstance(info[key], list):
            for rd in info[key]:
                for k in ("filepath", "_filename"):
                    p = rd.get(k) if isinstance(rd, dict) else None
                    if p and os.path.exists(p):
                        paths.append(p)

    # 2) Try prepared filename and common extensions
    try:
        base = ydl.prepare_filename(info)
        base_no_ext, _ = os.path.splitext(base)
        for ext in (".mp4", ".mkv", ".webm", ".m4a", ".mp3"):
            p = base_no_ext + ext
            if os.path.exists(p):
                paths.append(p)
        if os.path.exists(base):
            paths.append(base)
    except Exception:
        pass

    # 3) Fallback: pick the largest recent file from tmpdir
    try:
        for name in os.listdir(tmpdir):
            p = os.path.join(tmpdir, name)
            if os.path.isfile(p):
                paths.append(p)
    except Exception:
        pass

    if not paths:
        return None

    # Prefer mp4, then mkv, then others; among same ext prefer largest
    def pref_key(p: str):
        ext = os.path.splitext(p)[1].lower()
        prio = {".mp4": 0, ".mkv": 1, ".webm": 2}.get(ext, 3)
        try:
            size = os.path.getsize(p)
        except Exception:
            size = -1
        return (prio, -size)

    paths = sorted(set(paths), key=pref_key)
    return paths[0]


def download_youtube_under_limit(url: str, tmpdir: str, max_bytes: int) -> Tuple[Optional[str], Optional[str]]:
    last_error: Optional[str] = None

    for fmt in candidate_formats():
        ydl_opts = {
            "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "nocheckcertificate": True,
            # If merging is needed
            "merge_output_format": "mp4",
            "format": fmt,
        }
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                fp = resolve_downloaded_path(info, ydl, tmpdir)
                if not fp or not os.path.exists(fp):
                    last_error = "Файл не найден после загрузки"
                    continue
                try:
                    size = os.path.getsize(fp)
                except Exception:
                    size = max_bytes + 1
                if size <= max_bytes:
                    return fp, None
                # Too big — remove and try a lower quality
                try:
                    os.remove(fp)
                except Exception:
                    pass
                last_error = f"Размер файла {human_size(size)} превышает лимит"
        except Exception as e:
            last_error = str(e)
            continue

    return None, last_error or "Не удалось загрузить видео"


# === Bot logic ===

def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "") or ""
    user = msg.get("from", {})
    first_name = user.get("first_name", "")

    if text.startswith("/start"):
        send_message(
            chat_id,
            (
                "Привет, "
                f"{first_name or 'друг'}!\n\n"
                "Отправь ссылку на видео с YouTube, и я скачаю его и загружу сюда.\n\n"
                "Поддерживаются форматы \"youtube.com/watch\" и \"youtu.be\".\n"
                "Если видео слишком большое, я попробую выбрать качество поменьше."
            ),
        )
        return

    if text.startswith("/help"):
        send_message(chat_id, "Просто пришли ссылку на YouTube видео. Я отправлю файл сюда.")
        return

    if text.startswith("/stats") and chat_id == ADMIN_ID:
        total_ok = int(db.get("downloads_success", 0))
        total_fail = int(db.get("downloads_failed", 0))
        top_lines: List[str] = []
        try:
            users = db.get("users", {})
            top = sorted(users.items(), key=lambda kv: kv[1], reverse=True)[:10]
            for uid, cnt in top:
                top_lines.append(f"<code>{uid}</code>: {cnt}")
        except Exception:
            pass
        send_message(
            chat_id,
            (
                "<b>Статистика</b>\n"
                f"Успешно: <b>{total_ok}</b>\n"
                f"Ошибок: <b>{total_fail}</b>\n\n"
                + ("Топ пользователей:\n" + "\n".join(top_lines) if top_lines else "")
            ),
        )
        return

    url = extract_youtube_url(text)
    if not url:
        send_message(chat_id, "Пришлите ссылку на YouTube видео.")
        return

    # Process download in a background thread so polling isn't blocked
    Thread(target=download_and_send, args=(chat_id, url), daemon=True).start()
    send_message(chat_id, "⏬ Принял ссылку. Начинаю загрузку… Это может занять несколько минут.")


def download_and_send(chat_id: int, url: str):
    tmpdir = tempfile.mkdtemp(prefix="yt2tg_")
    message_id: Optional[int] = None
    try:
        send_chat_action(chat_id, "upload_video")
        file_path, err = download_youtube_under_limit(url, tmpdir, MAX_UPLOAD_BYTES)
        if not file_path:
            db["downloads_failed"] = int(db.get("downloads_failed", 0)) + 1
            save_db(db)
            send_message(
                chat_id,
                (
                    "Не удалось скачать видео. "
                    "Проверьте ссылку или попробуйте другое видео.\n"
                    + (f"Детали: {err}" if err else "")
                ),
            )
            return

        file_name = os.path.basename(file_path)
        caption = f"{file_name}"

        # Try sending as video first, then document as fallback
        ok = send_video(chat_id, file_path, caption=caption)
        if not ok:
            ok = send_document(chat_id, file_path, caption=caption)

        if ok:
            # Update stats
            db["downloads_success"] = int(db.get("downloads_success", 0)) + 1
            users = db.setdefault("users", {})
            users[str(chat_id)] = int(users.get(str(chat_id), 0)) + 1
            save_db(db)
        else:
            db["downloads_failed"] = int(db.get("downloads_failed", 0)) + 1
            save_db(db)
            send_message(chat_id, "Не удалось отправить файл в Telegram. Попробуйте другое качество или видео.")

    except Exception as e:
        db["downloads_failed"] = int(db.get("downloads_failed", 0)) + 1
        save_db(db)
        err_text = str(e)
        try:
            send_message(chat_id, f"Произошла ошибка: {err_text}")
        except Exception:
            pass
    finally:
        # Cleanup temp directory
        try:
            for name in os.listdir(tmpdir):
                p = os.path.join(tmpdir, name)
                try:
                    os.remove(p)
                except Exception:
                    pass
            os.rmdir(tmpdir)
        except Exception:
            pass


# === Long polling loop ===

def main():
    offset = None
    while True:
        updates = get_updates(offset)
        try:
            results = updates.get("result", [])
        except Exception:
            results = []
        for update in results:
            try:
                offset = update.get("update_id", 0) + 1
                if "message" in update:
                    handle_message(update["message"])
            except Exception:
                # Keep polling even if one update fails
                continue
        time.sleep(0.5)


if __name__ == "__main__":
    print("Бот запущен…")
    main()
