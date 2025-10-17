import os
import json
import logging
import re
from datetime import datetime
from typing import Dict, Any, List, Optional

from telegram import (
    Update,
    Chat,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatType, ParseMode, MessageEntityType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# ---------------------- Config & Globals ----------------------
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "7694543415"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8144446584:AAFmxbIOQd4O7Xwkv_IlLZsefjO5P4dVOYM")

DATA_FILE = os.path.join(os.path.dirname(__file__), "deals.json")
COMPLETED_FILE = os.path.join(os.path.dirname(__file__), "completed_deals.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("escrow-bot")

# ---------------------- Persistence ----------------------

def _load_json(path: str, default_value: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default_value
    except Exception as e:
        logger.exception("Failed to load %s: %s", path, e)
        return default_value


def _save_json(path: str, data: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Failed to save %s: %s", path, e)


STATE: Dict[str, Any] = _load_json(DATA_FILE, {"active": {}})
COMPLETED: List[Dict[str, Any]] = _load_json(COMPLETED_FILE, [])
PENDING_DEALS: Dict[str, Any] = {}


def save_state() -> None:
    _save_json(DATA_FILE, STATE)


def save_completed() -> None:
    _save_json(COMPLETED_FILE, COMPLETED)


# ---------------------- Helpers ----------------------

def generate_deal_id(chat_id: int) -> str:
    # Simple deterministic-ish suffix from chat_id and time
    suffix = abs(hash((chat_id, datetime.utcnow().timestamp()))) % 10000
    return f"DEAL-{suffix:04d}"


def is_group(chat: Chat) -> bool:
    return chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


def get_deal(chat_id: int) -> Optional[Dict[str, Any]]:
    return STATE["active"].get(str(chat_id))


def ensure_deal(chat: Chat) -> Dict[str, Any]:
    chat_id = str(chat.id)
    deal = STATE["active"].get(chat_id)
    if deal:
        return deal
    deal_id = generate_deal_id(chat.id)
    deal = {
        "dealId": deal_id,
        "chatId": chat.id,
        "chatTitle": chat.title,
        "name": None,
        "description": None,
        "initiatorId": None,
        "participants": [],  # up to 2 user ids
        "confirmations": {},  # user_id -> bool
        "evidence": [],  # list of {userId, type, fileId, caption, date}
        "createdAt": datetime.utcnow().isoformat(),
        "state": "new",  # new | active | completed | cancelled
        "events": [],
    }
    STATE["active"][chat_id] = deal
    save_state()
    return deal


def add_event(deal: Dict[str, Any], event: str, by_user: Optional[int] = None) -> None:
    deal.setdefault("events", []).append({
        "at": datetime.utcnow().isoformat(),
        "by": by_user,
        "event": event,
    })
    save_state()


def set_chat_meta_safe(context: ContextTypes.DEFAULT_TYPE, chat_id: int, title: Optional[str], description: Optional[str]) -> None:
    async def _inner():
        try:
            if title:
                await context.bot.set_chat_title(chat_id=chat_id, title=title)
        except Exception as e:
            logger.warning("Cannot set chat title: %s", e)
        try:
            if description:
                await context.bot.set_chat_description(chat_id=chat_id, description=description)
        except Exception as e:
            logger.warning("Cannot set chat description: %s", e)
    # schedule fire-and-forget
    context.application.create_task(_inner())


def format_participants(context: ContextTypes.DEFAULT_TYPE, participant_ids: List[int]) -> str:
    usernames: List[str] = []
    for uid in participant_ids:
        usernames.append(f"<a href=\"tg://user?id={uid}\">user_{uid}</a>")
    return ", ".join(usernames) if usernames else "—"


SUSPICIOUS_LINK_RE = re.compile(r"https?://(?!t\.me|telegram\.me|telegra\.ph)[^\s]+", re.IGNORECASE)


# ---------------------- Handlers ----------------------

async def on_bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu: ChatMemberUpdated = update.my_chat_member
    if not cmu or not cmu.chat:
        return
    chat = cmu.chat
    if not is_group(chat):
        return

    old = cmu.old_chat_member.status if cmu.old_chat_member else None
    new = cmu.new_chat_member.status if cmu.new_chat_member else None

    if new in ("member", "administrator") and old in ("left", "kicked"):
        adder_id = cmu.from_user.id if cmu.from_user else None
        initiator_pending = PENDING_DEALS.get(str(adder_id)) if adder_id else None

        deal = ensure_deal(chat)
        add_event(deal, "bot_added")

        if initiator_pending:
            # Initialize deal from pending
            description = initiator_pending.get("description")
            initiator_id = initiator_pending.get("initiatorId")
            deal["name"] = description
            deal["description"] = description
            deal["initiatorId"] = initiator_id
            deal["participants"] = [initiator_id]
            deal["confirmations"] = {str(initiator_id): False}
            deal["state"] = "active"
            deal["inviteDeadlineAt"] = (datetime.utcnow()).isoformat()
            add_event(deal, "deal_started_from_dm", initiator_id)
            save_state()

            # Set chat title/description accordingly
            new_title = f"Сделка №{deal['dealId']} — {description[:48]}"
            new_desc = f"Обмен: {description}. Сделку контролирует бот‑гарант."
            set_chat_meta_safe(context, chat.id, new_title, new_desc)

            # Schedule 2-minute participant check
            job_name = f"deal-timeout-{chat.id}"
            def _cancel_existing_jobs():
                for j in context.job_queue.get_jobs_by_name(job_name):
                    j.schedule_removal()
            _cancel_existing_jobs()
            context.job_queue.run_once(deal_timeout_job, when=120, data={"chat_id": chat.id}, name=job_name)

            participants_fmt = format_participants(context, deal["participants"])  # clickable mentions
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"🔒 Создан временный чат сделки №{deal['dealId']}. Участники: {participants_fmt}.\n\n"
                    "Добавьте собеседника в этот чат в течение 2 минут.\n"
                    "Если второй участник не будет добавлен, бот покинет чат."
                ),
                parse_mode=ParseMode.HTML,
            )

            # Clear pending
            PENDING_DEALS.pop(str(adder_id), None)
        else:
            # Generic guidance if not created from DM
            tentative_title = f"Сделка №{deal['dealId']} — без названия"
            tentative_desc = "Обмен: не задан. Сделку контролирует бот-гарант. Команда: /start_deal \"описание\" (ответом на сообщение второй стороны)."
            set_chat_meta_safe(context, chat.id, tentative_title, tentative_desc)

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(text="Начать сделку", callback_data="noop")],
            ])
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    "🔒 Бот-гарант подключён.\n\n"
                    "Чтобы зарегистрировать сделку, отправьте команду \n"
                    "<b>/start_deal \"описание обмена\"</b> ответом на сообщение второй стороны.\n\n"
                    "После регистрации стороны могут отправлять доказательства (фото/файлы).\n"
                    "Подтверждение: <b>/confirm</b>. Проверка статуса: <b>/check</b>."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )

async def deal_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    if not chat_id:
        return
    deal = get_deal(chat_id)
    if not deal or deal.get("state") != "active":
        return
    # If less than 2 participants (excluding admin), leave the chat
    participant_ids = [uid for uid in deal.get("participants", []) if uid != ADMIN_USER_ID]
    if len(participant_ids) < 2:
        # Announce and schedule leave in 20 seconds
        try:
            await context.bot.send_message(chat_id, "⏳ Второй участник не добавлен. Покину чат через 20 секунд.")
        except Exception:
            pass

        leave_job_name = f"deal-auto-leave-{chat_id}"
        for j in context.job_queue.get_jobs_by_name(leave_job_name):
            j.schedule_removal()
        context.job_queue.run_once(deal_auto_leave_job, when=20, data={"chat_id": chat_id}, name=leave_job_name)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_group(update.effective_chat):
        await update.effective_message.reply_text(
            "Привет! Для начала сделки в ЛС:",
        )
        await update.effective_message.reply_text(
            "Используйте команду /new_deal \"описание\" — я помогу создать временный чат.")
        return
    await update.effective_message.reply_text(
        "Привет! Я бот‑гарант. В группе используйте /start_deal в ответ на собеседника. Команды: /help"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    is_admin = user and user.id == ADMIN_USER_ID
    lines = [
        "🆘 Команды бота:",
        "\n<b>Личные сообщения боту</b>",
        "/new_deal \"описание\" — начать сделку, создать временный чат",
        "\n<b>В группе сделки</b>",
        "/start_deal \"описание\" — зарегистрировать сделку",
        "/join_deal — присоединиться к сделке",
        "/confirm — подтвердить получение",
        "/check — проверить статус",
    ]
    if is_admin:
        lines += [
            "\n<b>Админ-команды</b>",
            "/admin_list — список активных сделок",
            "/admin_end — завершить сделку (в этом чате)",
            "/admin_leave — покинуть чат",
            "/admin_kick — исключить пользователя (ответом на его сообщение)",
            "/admin_unban — снять бан у пользователя (по ID)",
        ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_new_deal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user
    if not chat or chat.type != ChatType.PRIVATE:
        await msg.reply_text("Эта команда доступна только в личных сообщениях боту.")
        return

    description = _parse_description_from_message_text(msg.text or "")
    if not description:
        await msg.reply_text("Укажи описание: /new_deal \"обмен 500₽ на аккаунт Steam\"")
        return

    # Save pending
    PENDING_DEALS[str(user.id)] = {
        "initiatorId": user.id,
        "description": description,
        "createdAt": datetime.utcnow().isoformat(),
    }

    # Build startgroup deep link
    me = await context.bot.get_me()
    username = me.username
    startgroup_link = f"https://t.me/{username}?startgroup=deal"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(text="Создать временный чат", url=startgroup_link)],
    ])
    await msg.reply_text(
        (
            "Сейчас я помогу создать временный чат для сделки.\n"
            "Нажмите кнопку ниже, выберите 'Новая группа' и добавьте второго участника.\n"
            "У вас будет 2 минуты, чтобы добавить собеседника."
        ),
        reply_markup=kb,
    )


async def deal_auto_leave_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    if not chat_id:
        return
    deal = get_deal(chat_id)
    if deal:
        deal["state"] = "cancelled"
        deal["cancelReason"] = "no_second_participant"
        deal_copy = dict(deal)
        deal_copy["completedAt"] = datetime.utcnow().isoformat()
        COMPLETED.append(deal_copy)
        STATE["active"].pop(str(chat_id), None)
        save_completed()
        save_state()
    try:
        await context.bot.leave_chat(chat_id)
    except Exception:
        pass


def _parse_description_from_message_text(text: str) -> Optional[str]:
    # Support both /new_deal and /start_deal, with optional @botname, quoted or unquoted
    commands = ["new_deal", "start_deal"]
    for cmd in commands:
        # Quoted first
        m = re.search(rf"/{cmd}(?:@[A-Za-z0-9_]+)?\\s+\"([\\s\\S]+?)\"", text)
        if m:
            return m.group(1).strip()
        # Unquoted fallback
        m2 = re.search(rf"/{cmd}(?:@[A-Za-z0-9_]+)?\\s+(.+)$", text)
        if m2:
            return m2.group(1).strip()
    return None


async def cmd_start_deal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not is_group(chat):
        await msg.reply_text("Команда доступна только в групповых чатах.")
        return

    deal = ensure_deal(chat)
    if deal.get("state") in ("active", "completed"):
        await msg.reply_text("В этом чате сделка уже зарегистрирована.")
        return

    description = _parse_description_from_message_text(msg.text or "")
    if not description:
        await msg.reply_text(
            "Укажи описание: /start_deal \"обмен 500₽ на аккаунт Steam\"\n"
            "Лучше отправить команду ответом на сообщение второй стороны."
        )
        return

    # Detect counterparty from reply if present
    counterparty_id: Optional[int] = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        counterparty_id = msg.reply_to_message.from_user.id

    # Fallback: check for text_mention entities (user objects)
    if not counterparty_id and msg.entities:
        for ent in msg.entities:
            if ent.type == MessageEntityType.TEXT_MENTION and ent.user:
                counterparty_id = ent.user.id
                break

    # Initialize deal
    deal["name"] = description
    deal["description"] = description
    deal["initiatorId"] = user.id
    deal["participants"] = [user.id]
    deal["confirmations"] = {str(user.id): False}
    deal["state"] = "active"
    add_event(deal, "deal_started", user.id)

    if counterparty_id and counterparty_id != user.id:
        if counterparty_id not in deal["participants"]:
            deal["participants"].append(counterparty_id)
            deal["confirmations"][str(counterparty_id)] = False
        add_event(deal, "counterparty_auto_added", user.id)
    else:
        add_event(deal, "counterparty_pending", user.id)

    save_state()

    # Try to set chat title / description
    new_title = f"Сделка №{deal['dealId']} — {description[:48]}"
    new_desc = f"Обмен: {description}. Сделку контролирует бот‑гарант."
    set_chat_meta_safe(context, chat.id, new_title, new_desc)

    participants_fmt = format_participants(context, deal["participants"])  # clickable mentions
    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"🔒 Сделка №{deal['dealId']} начата. Участники: {participants_fmt}.\n\n"
            f"Отправляйте доказательства (фото/скриншоты/файлы). Участники подтверждают командой /confirm.\n"
            f"Второй участник, если не добавлен, используйте /join_deal."
        ),
        parse_mode=ParseMode.HTML,
    )


async def cmd_join_deal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    deal = get_deal(chat.id) if chat else None
    if not deal or deal.get("state") != "active":
        await msg.reply_text("Сделка не инициализирована. Используйте /start_deal.")
        return

    if user.id in deal["participants"]:
        await msg.reply_text("Вы уже участник сделки.")
        return

    if len(deal["participants"]) >= 2 and user.id != ADMIN_USER_ID:
        await msg.reply_text("У сделки уже две стороны. Свяжитесь с модератором.")
        return

    deal["participants"].append(user.id)
    deal["confirmations"][str(user.id)] = False
    add_event(deal, "participant_joined", user.id)
    save_state()

    await msg.reply_text("Вы добавлены как участник сделки. Подтвердите после получения: /confirm")


async def _finalize_deal(chat_id: int, deal: Dict[str, Any], context: ContextTypes.DEFAULT_TYPE) -> None:
    deal["state"] = "completed"
    deal["completedAt"] = datetime.utcnow().isoformat()
    COMPLETED.append(deal)
    save_completed()
    # Remove from active
    STATE["active"].pop(str(chat_id), None)
    save_state()

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Сделка №{deal['dealId']} успешно завершена.\n"
                "Все стороны подтвердили получение."
            ),
        )
        await context.bot.set_chat_description(chat_id, f"Сделка №{deal['dealId']} завершена.")
        await context.bot.send_message(chat_id, f"💬 Сделка #{deal['dealId'].split('-')[-1]} завершена! Всем удачи в дальнейшем.")
    except Exception as e:
        logger.warning("Finalize msg failed: %s", e)

    try:
        await context.bot.leave_chat(chat_id)
    except Exception:
        pass


async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    deal = get_deal(chat.id) if chat else None
    if not deal or deal.get("state") != "active":
        await msg.reply_text("Сделка не активна.")
        return

    if user.id not in deal["participants"] and user.id != ADMIN_USER_ID:
        await msg.reply_text("Только участники сделки могут подтверждать.")
        return

    deal["confirmations"][str(user.id)] = True
    add_event(deal, "confirm", user.id)
    save_state()

    await msg.reply_text("Ваше подтверждение учтено.")

    # Check if both confirmed
    participant_ids = [uid for uid in deal["participants"] if uid != ADMIN_USER_ID]
    if len(participant_ids) >= 2:
        if all(deal["confirmations"].get(str(uid)) for uid in participant_ids):
            await _finalize_deal(chat.id, deal, context)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    deal = get_deal(chat.id)
    if not deal:
        await update.effective_message.reply_text("Нет активной сделки.")
        return

    parts = deal["participants"]
    conf_lines = []
    for uid in parts:
        status = "✅" if deal["confirmations"].get(str(uid)) else "⏳"
        conf_lines.append(f"{status} <a href=\"tg://user?id={uid}\">user_{uid}</a>")

    await update.effective_message.reply_text(
        (
            f"Сделка №{deal['dealId']}\n"
            f"Описание: {deal.get('description') or '—'}\n"
            f"Участники: {', '.join(conf_lines) if conf_lines else '—'}\n"
            f"Доказательств: {len(deal.get('evidence', []))}"
        ),
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat:
        return
    deal = get_deal(chat.id)
    if not deal:
        await msg.reply_text("Нет активной сделки.")
        return
    if user.id != ADMIN_USER_ID:
        await msg.reply_text("Отмена доступна только модератору.")
        return

    deal["state"] = "cancelled"
    add_event(deal, "cancelled", user.id)
    # Move to completed list as cancelled record
    deal_copy = dict(deal)
    deal_copy["completedAt"] = datetime.utcnow().isoformat()
    COMPLETED.append(deal_copy)
    save_completed()
    STATE["active"].pop(str(chat.id), None)
    save_state()

    await msg.reply_text("Сделка отменена модератором.")


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not chat:
        return
    deal = get_deal(chat.id)
    if not deal or deal.get("state") != "active":
        return
    # Add first non-initiator human as counterparty
    for u in (msg.new_chat_members or []):
        if u.is_bot:
            continue
        if u.id not in deal["participants"]:
            deal["participants"].append(u.id)
            deal["confirmations"][str(u.id)] = False
            add_event(deal, "counterparty_joined", u.id)
            save_state()
            # Cancel timeout job
            job_name = f"deal-timeout-{chat.id}"
            for j in context.job_queue.get_jobs_by_name(job_name):
                j.schedule_removal()
            try:
                await msg.reply_text("Второй участник добавлен. Можно приступать к обмену. /confirm после получения.")
            except Exception:
                pass


async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    lines = ["Активные сделки:"]
    for chat_id, d in STATE.get("active", {}).items():
        lines.append(
            f"• {d.get('dealId')} — {d.get('chatTitle') or chat_id} — участников: {len(d.get('participants', []))}"
        )
    if len(lines) == 1:
        lines.append("(пусто)")
    await update.effective_message.reply_text("\n".join(lines))


async def admin_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or user.id != ADMIN_USER_ID or not chat:
        return
    deal = get_deal(chat.id)
    if not deal:
        await update.effective_message.reply_text("Нет активной сделки в этом чате.")
        return
    await _finalize_deal(chat.id, deal, context)


async def admin_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or user.id != ADMIN_USER_ID or not chat:
        return
    try:
        await context.bot.leave_chat(chat.id)
    except Exception:
        pass


async def admin_kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or user.id != ADMIN_USER_ID or not chat:
        return
    target = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user.id
    elif context.args:
        try:
            target = int(context.args[0])
        except Exception:
            pass
    if not target:
        await msg.reply_text("Укажите пользователя ответом на сообщение или ID.")
        return
    try:
        await context.bot.ban_chat_member(chat.id, target)
        await msg.reply_text("Пользователь исключён.")
    except Exception as e:
        await msg.reply_text(f"Не удалось исключить: {e}")


async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or user.id != ADMIN_USER_ID or not chat:
        return
    if not context.args:
        await msg.reply_text("Укажите ID пользователя: /admin_unban <user_id>")
        return
    try:
        target = int(context.args[0])
    except Exception:
        await msg.reply_text("Некорректный ID.")
        return
    try:
        await context.bot.unban_chat_member(chat.id, target, only_if_banned=True)
        await msg.reply_text("Пользователь разбанен (если был забанен).")
    except Exception as e:
        await msg.reply_text(f"Не удалось разбанить: {e}")


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not chat:
        return
    deal = get_deal(chat.id)
    if not deal or deal.get("state") != "active":
        return
    if user.id not in deal["participants"] and user.id != ADMIN_USER_ID:
        return

    evidence_type = None
    file_id = None
    caption = msg.caption or ""

    if msg.photo:
        evidence_type = "photo"
        file_id = msg.photo[-1].file_id
    elif msg.document:
        evidence_type = "document"
        file_id = msg.document.file_id
    elif msg.video:
        evidence_type = "video"
        file_id = msg.video.file_id

    if evidence_type and file_id:
        deal.setdefault("evidence", []).append({
            "userId": user.id,
            "type": evidence_type,
            "fileId": file_id,
            "caption": caption,
            "date": datetime.utcnow().isoformat(),
        })
        add_event(deal, f"evidence_{evidence_type}", user.id)
        save_state()
        try:
            await msg.reply_text("Доказательство сохранено.")
        except Exception:
            pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    # Simple phishing/suspicious link warning
    text = msg.text or msg.caption or ""
    if SUSPICIOUS_LINK_RE.search(text):
        try:
            await msg.reply_text("⚠️ Обнаружена подозрительная ссылка. Будьте осторожны и проверяйте адреса сайтов.")
        except Exception:
            pass

    # Ignore further processing for generic text


def main() -> None:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(TOKEN).build()

    # Bot added to group
    app.add_handler(ChatMemberHandler(on_bot_added, ChatMemberHandler.MY_CHAT_MEMBER))

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new_deal", cmd_new_deal))
    app.add_handler(CommandHandler("start_deal", cmd_start_deal))
    app.add_handler(CommandHandler("join_deal", cmd_join_deal))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("cancel_deal", cmd_cancel))

    # Media & text
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL | filters.VIDEO, handle_media))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Admin commands
    app.add_handler(CommandHandler("admin_list", admin_list))
    app.add_handler(CommandHandler("admin_end", admin_end))
    app.add_handler(CommandHandler("admin_leave", admin_leave))
    app.add_handler(CommandHandler("admin_kick", admin_kick))
    app.add_handler(CommandHandler("admin_unban", admin_unban))

    logger.info("Bot starting with admin %s", ADMIN_USER_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()
