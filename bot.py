"""
Random Coffee бот для "Подруги в Вильнюсе"

Логика:
- Каждую субботу в 12:00 бот присылает в чат опрос "Участвуешь в Random Coffee на этой неделе?"
- Каждый понедельник в 13:00 бот собирает всех, кто проголосовал "Да",
  случайно разбивает на пары (или тройку, если участниц нечётное число)
  и присылает результат в чат, упоминая пары по именам.
- Данные хранятся в простом файле SQLite (coffee.db), переживает перезапуски бота.

Настройка перед запуском:
1. pip install -r requirements.txt
2. Впиши свой токен бота в переменную окружения BOT_TOKEN (или в .env файл)
3. Впиши ID своего чата в GROUP_CHAT_ID (см. инструкцию в README.md, как узнать ID)
4. Добавь бота в группу и дай права администратора (нужно для чтения списка участников)
"""

import asyncio
import logging
import os
import random
import re
import sqlite3
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Poll
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    TypeHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН")
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0"))  # напр. -1001234567890
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))  # твой личный Telegram user_id
GROUP_TOPIC_ID = int(os.environ.get("GROUP_TOPIC_ID", "0")) or None  # ID темы (topic) в группе, если бот должен писать в конкретную тему
DB_PATH = os.environ.get("DB_PATH", "coffee.db")

POLL_QUESTION = "Участвуешь в Random Coffee на этой неделе? ☕"
POLL_OPTIONS = ["Да, давайте! ☕", "Не в этот раз"]

# ---------- База данных ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS current_poll (
            poll_id TEXT PRIMARY KEY,
            created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS participants (
            poll_id TEXT,
            user_id INTEGER,
            first_name TEXT,
            username TEXT,
            PRIMARY KEY (poll_id, user_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pairs_published (
            poll_id TEXT PRIMARY KEY,
            published_at TEXT
        )"""
    )
    conn.commit()
    conn.close()


def is_pairs_published(poll_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM pairs_published WHERE poll_id = ?", (poll_id,)).fetchone()
    conn.close()
    return row is not None


def mark_pairs_published(poll_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO pairs_published (poll_id, published_at) VALUES (?, ?)",
        (poll_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def clear_pairs_published(poll_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM pairs_published WHERE poll_id = ?", (poll_id,))
    conn.commit()
    conn.close()


def save_poll_id(poll_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM current_poll")  # храним только последний активный опрос
    conn.execute(
        "INSERT INTO current_poll (poll_id, created_at) VALUES (?, ?)",
        (poll_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_current_poll_id():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT poll_id FROM current_poll").fetchone()
    conn.close()
    return row[0] if row else None


def add_participant(poll_id, user_id, first_name, username):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO participants (poll_id, user_id, first_name, username) VALUES (?, ?, ?, ?)",
        (poll_id, user_id, first_name, username or ""),
    )
    conn.commit()
    conn.close()


def remove_participant(poll_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "DELETE FROM participants WHERE poll_id = ? AND user_id = ?",
        (poll_id, user_id),
    )
    conn.commit()
    conn.close()


def get_participants(poll_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT user_id, first_name, username FROM participants WHERE poll_id = ?",
        (poll_id,),
    ).fetchall()
    conn.close()
    return rows




def set_manual_pick(poll_id, partner_id, partner_name):
    """Сохраняет заранее выбранную админом пару на текущую неделю."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS manual_pick (
            poll_id TEXT PRIMARY KEY,
            partner_id INTEGER,
            partner_name TEXT
        )"""
    )
    conn.execute("DELETE FROM manual_pick")  # только один активный выбор за раз
    conn.execute(
        "INSERT INTO manual_pick (poll_id, partner_id, partner_name) VALUES (?, ?, ?)",
        (poll_id, partner_id, partner_name),
    )
    conn.commit()
    conn.close()


def get_manual_pick(poll_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS manual_pick (
            poll_id TEXT PRIMARY KEY,
            partner_id INTEGER,
            partner_name TEXT
        )"""
    )
    row = conn.execute(
        "SELECT partner_id, partner_name FROM manual_pick WHERE poll_id = ?",
        (poll_id,),
    ).fetchone()
    conn.close()
    return row  # (partner_id, partner_name) или None


# ---------- Логика бота ----------

async def send_weekly_poll(context: ContextTypes.DEFAULT_TYPE):
    """Отправляется каждую субботу."""
    message = await context.bot.send_poll(
        chat_id=GROUP_CHAT_ID,
        question=POLL_QUESTION,
        options=POLL_OPTIONS,
        is_anonymous=False,  # обязательно False, чтобы видеть, кто именно проголосовал
        message_thread_id=GROUP_TOPIC_ID,
    )
    save_poll_id(message.poll.id)
    logger.info(f"Отправлен новый опрос Random Coffee: {message.poll.id}")


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает при любом голосовании в опросе — фильтруем по актуальному poll_id."""
    answer = update.poll_answer
    current_poll_id = get_current_poll_id()
    if answer.poll_id != current_poll_id:
        return  # старый/чужой опрос, игнорируем

    user = answer.user
    if 0 in answer.option_ids:  # индекс 0 = "Да, давайте!"
        add_participant(current_poll_id, user.id, full_name(user), user.username)
        logger.info(f"{full_name(user)} присоединилась к Random Coffee")
    else:
        remove_participant(current_poll_id, user.id)


def make_pairs(participants):
    """Случайно разбивает список участниц на пары (последняя тройка, если нечётное число)."""
    shuffled = participants[:]
    random.shuffle(shuffled)
    pairs = []
    i = 0
    while i < len(shuffled):
        if len(shuffled) - i == 3:  # последняя тройка вместо пары+одиночки
            pairs.append(shuffled[i:i + 3])
            i += 3
        else:
            pairs.append(shuffled[i:i + 2])
            i += 2
    return pairs


def mention(user_id, first_name):
    return f'<a href="tg://user?id={user_id}">{first_name}</a>'


def full_name(entity):
    """Полное имя (Имя + Фамилия) из объекта User/Chat, чтобы участниц не путать по одному имени."""
    if not entity:
        return None
    name = entity.first_name or ""
    if getattr(entity, "last_name", None):
        name = f"{name} {entity.last_name}".strip()
    return name or (getattr(entity, "username", None) or "Без имени")


async def announce_pairs(context: ContextTypes.DEFAULT_TYPE):
    """Отправляется каждый понедельник — собирает пары и публикует результат.

    Возвращает статус: "no_poll" (нет активного опроса), "already_published" (пары для
    этого опроса уже отправлялись), "too_few" (меньше 2 участниц) или "ok" (опубликовано) —
    чтобы вызывающий код мог сообщить об этом админу.
    """
    current_poll_id = get_current_poll_id()
    if not current_poll_id:
        logger.info("Нет активного опроса — пропускаем распределение пар")
        return "no_poll"

    if is_pairs_published(current_poll_id):
        logger.info("Пары для этого опроса уже опубликованы — пропускаем повтор")
        return "already_published"

    participants = get_participants(current_poll_id)

    # --- Ручной выбор администратора (если сделан командой /pick) ---
    fixed_pair = None
    manual_pick = get_manual_pick(current_poll_id) if ADMIN_USER_ID else None
    if manual_pick:
        partner_id, partner_name = manual_pick
        # достаём имя админа из списка участниц (если голосовала) или используем заглушку
        admin_name = next((name for uid, name, _ in participants if uid == ADMIN_USER_ID), "Я")
        fixed_pair = [(ADMIN_USER_ID, admin_name, None), (partner_id, partner_name, None)]
        # убираем админа и её партнёршу из общего пула — они не участвуют в рандоме
        participants = [
            p for p in participants if p[0] not in (ADMIN_USER_ID, partner_id)
        ]

    if len(participants) < 2 and not fixed_pair:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text="На этой неделе набралось меньше 2 участниц для Random Coffee 😔 Попробуем на следующей!",
            message_thread_id=GROUP_TOPIC_ID,
        )
        return "too_few"

    pairs = make_pairs(participants) if len(participants) >= 2 else []
    if fixed_pair:
        random.shuffle(fixed_pair)  # чтобы админ не оказывался всегда первой в паре
        pairs.append(fixed_pair)
    random.shuffle(pairs)  # чтобы пара админа не оказывалась всегда первой в списке

    if participants and len(participants) == 1:
        # осталась одна лишняя участница без пары (из-за того что админ забрал себе партнёршу)
        leftover = participants[0]
        lines_extra = f"\n\n{mention(leftover[0], leftover[1])} — на этой неделе пары не хватило, но обязательно в следующий раз! 🫶🏻"
    else:
        lines_extra = ""

    lines = ["☕ Пары для Random Coffee на этой неделе:\n"]
    for group in pairs:
        names = " + ".join(mention(uid, name) for uid, name, _ in group)
        lines.append(f"• {names}")
    lines.append("\nНапишите друг другу и договоритесь о встрече на этой неделе 🫶🏻")
    if lines_extra:
        lines.append(lines_extra)

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text="\n".join(lines),
        parse_mode="HTML",
        message_thread_id=GROUP_TOPIC_ID,
    )
    mark_pairs_published(current_poll_id)
    logger.info(f"Опубликованы пары: {len(pairs)} групп")
    return "ok"


async def restrict_private_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """В личке бот отвечает только администратору — остальным просто молчит."""
    chat = update.effective_chat
    user = update.effective_user
    if ADMIN_USER_ID and chat and chat.type == "private" and user and user.id != ADMIN_USER_ID:
        raise ApplicationHandlerStop


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот Random Coffee для «Подруги в Вильнюсе». "
        "Каждую субботу в чате появляется опрос — проголосуй, если хочешь "
        "случайную пару для кофе на неделе, а по понедельникам я объявлю пары ☕"
    )


ADMIN_HELP_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("☕ Прислать опрос сейчас", callback_data="coffee_now")],
        [InlineKeyboardButton("🎲 Разбить пары сейчас", callback_data="pairs_now")],
        [InlineKeyboardButton("🫶🏻 Выбрать пару", callback_data="pick_prompt")],
        [InlineKeyboardButton("✍️ Собрать пары вручную", callback_data="manual_start")],
    ]
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = bool(ADMIN_USER_ID) and update.effective_user.id == ADMIN_USER_ID

    if is_admin:
        await update.message.reply_text(
            "Команды администратора:\n\n"
            "☕ /coffee_now — прислать опрос вне расписания\n"
            "🎲 /pairs_now — разбить пары вне расписания\n"
            "✍️ /manual_pairs — разбить на пары участниц вручную, без привязки к опросу "
            "(каждая с новой строки; одинаковый номер перед именами — закрепляет пару)\n"
            "🫶🏻 /pick — заранее выбрать себе пару на неделю "
            "(reply в группе, или в личке ID/@username/ссылка/пересланное сообщение)\n"
            "🧵 /topicid — узнать ID темы группы\n"
            "🔓 /unlock_pairs — снять блокировку, если пары для текущего опроса уже отправлялись, а нужно переотправить\n\n"
            "Кнопки ниже делают то же самое, что и команды выше — просто быстрее:",
            reply_markup=ADMIN_HELP_KEYBOARD,
        )
    else:
        await update.message.reply_text(
            "Я бот Random Coffee для «Подруги в Вильнюсе» ☕\n\n"
            "Каждую субботу в теме появляется опрос — голосуй, если хочешь "
            "случайную пару для встречи на неделе, а в понедельник я объявлю пары там же."
        )


PAIRS_STATUS_MESSAGES = {
    "no_poll": "Сейчас нет активного опроса Random Coffee — сначала отправь /coffee_now.",
    "already_published": "Пары для этого опроса уже отправлялись — повторно не шлю. Если правда нужно переотправить — сначала /unlock_pairs.",
    "too_few": "Меньше 2 участниц проголосовало — сообщение об этом уже ушло в чат.",
    "ok": "Пары отправлены в чат! ☕",
}


async def handle_help_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not ADMIN_USER_ID or query.from_user.id != ADMIN_USER_ID:
        await query.answer("Эта кнопка доступна только администратору чата.", show_alert=True)
        return

    await query.answer()
    if query.data == "coffee_now":
        await send_weekly_poll(context)
        await query.message.reply_text("Опрос отправлен в чат!")
    elif query.data == "pairs_now":
        status = await announce_pairs(context)
        await query.message.reply_text(PAIRS_STATUS_MESSAGES.get(status, "Готово."))
    elif query.data == "pick_prompt":
        current_poll_id = get_current_poll_id()
        if not current_poll_id:
            await query.message.reply_text("Сейчас нет активного опроса Random Coffee — сначала должен появиться новый.")
            return
        context.user_data["awaiting_pick"] = True
        await query.message.reply_text(PICK_USAGE)
    elif query.data == "manual_start":
        current_poll_id = get_current_poll_id()
        if not current_poll_id:
            await query.message.reply_text("Сейчас нет активного опроса Random Coffee — сначала должен появиться новый.")
            return
        if is_pairs_published(current_poll_id):
            await query.message.reply_text(PAIRS_STATUS_MESSAGES["already_published"])
            return
        people = [(uid, name) for uid, name, _ in get_participants(current_poll_id)]
        if len(people) < 2:
            await query.message.reply_text("В текущем опросе меньше 2 проголосовавших — собирать пока не из кого.")
            return
        context.user_data["manual_pool"] = [[uid, name] for uid, name in people]
        context.user_data["manual_pairs"] = []
        context.user_data["manual_selected"] = None
        text, markup = render_manual_state(context)
        await query.message.reply_text(text, reply_markup=markup)


def render_manual_state(context: ContextTypes.DEFAULT_TYPE):
    pool = context.user_data.get("manual_pool", [])
    pairs = context.user_data.get("manual_pairs", [])
    selected = context.user_data.get("manual_selected")

    lines = ["✍️ Собираем пары из последнего опроса — тапай по именам (сначала одну, потом вторую):\n"]
    if pairs:
        lines.append("Уже готово:")
        for group in pairs:
            lines.append("• " + " + ".join(name for _, name in group))
        lines.append("")
    if selected is not None:
        sel_name = next((name for uid, name in pool if uid == selected), "")
        lines.append(f"Выбрано: {sel_name} — теперь тапни вторую участницу")
    elif pool:
        lines.append("Осталось разобрать:")
    else:
        lines.append("Все разобраны!")

    buttons = []
    row = []
    for uid, name in pool:
        label = f"✅ {name}" if uid == selected else name
        row.append(InlineKeyboardButton(label, callback_data=f"mtoggle:{uid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    controls = []
    if len(pool) >= 2:
        controls.append(InlineKeyboardButton("🎲 Остальных случайно", callback_data="mrandom"))
    if pairs:
        controls.append(InlineKeyboardButton("✅ Опубликовать", callback_data="mdone"))
    controls.append(InlineKeyboardButton("❌ Отмена", callback_data="mcancel"))
    buttons.append(controls)

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _clear_manual_state(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("manual_pool", None)
    context.user_data.pop("manual_pairs", None)
    context.user_data.pop("manual_selected", None)


async def handle_manual_picker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not ADMIN_USER_ID or query.from_user.id != ADMIN_USER_ID:
        await query.answer("Эта кнопка доступна только администратору чата.", show_alert=True)
        return
    await query.answer()

    pool = context.user_data.get("manual_pool")
    if pool is None:
        await query.edit_message_text("Сессия сборки пар устарела — начни заново через /help.")
        return

    if query.data == "mcancel":
        _clear_manual_state(context)
        await query.edit_message_text("Отменено.")
        return

    if query.data == "mrandom":
        new_pairs = make_pairs([tuple(p) for p in pool])
        context.user_data["manual_pairs"].extend([list(group) for group in new_pairs])
        context.user_data["manual_pool"] = []
        context.user_data["manual_selected"] = None
        text, markup = render_manual_state(context)
        await query.edit_message_text(text, reply_markup=markup)
        return

    if query.data == "mdone":
        pairs = context.user_data.get("manual_pairs", [])
        if not pairs:
            await query.answer("Пока нет ни одной пары.", show_alert=True)
            return
        current_poll_id = get_current_poll_id()
        if current_poll_id and is_pairs_published(current_poll_id):
            _clear_manual_state(context)
            await query.edit_message_text(PAIRS_STATUS_MESSAGES["already_published"])
            return
        random.shuffle(pairs)
        for group in pairs:
            random.shuffle(group)

        lines = ["☕ Пары для Random Coffee на этой неделе:\n"]
        for group in pairs:
            names = " + ".join(mention(uid, name) for uid, name in group)
            lines.append(f"• {names}")
        lines.append("\nНапишите друг другу и договоритесь о встрече на этой неделе 🫶🏻")

        leftover = context.user_data.get("manual_pool", [])
        if leftover:
            leftover_uid, leftover_name = leftover[0]
            lines.append(f"\n\n{mention(leftover_uid, leftover_name)} — на этой неделе пары не хватило, но обязательно в следующий раз! 🫶🏻")

        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text="\n".join(lines),
            parse_mode="HTML",
            message_thread_id=GROUP_TOPIC_ID,
        )
        if current_poll_id:
            mark_pairs_published(current_poll_id)
        _clear_manual_state(context)
        await query.edit_message_text("Пары отправлены в чат! ☕")
        return

    if query.data.startswith("mtoggle:"):
        uid = int(query.data.split(":", 1)[1])
        selected = context.user_data.get("manual_selected")
        if selected is None:
            context.user_data["manual_selected"] = uid
        elif selected == uid:
            context.user_data["manual_selected"] = None
        else:
            name_a = next((name for i, name in pool if i == selected), None)
            name_b = next((name for i, name in pool if i == uid), None)
            context.user_data["manual_pairs"].append([[selected, name_a], [uid, name_b]])
            context.user_data["manual_pool"] = [p for p in pool if p[0] not in (selected, uid)]
            context.user_data["manual_selected"] = None
        text, markup = render_manual_state(context)
        await query.edit_message_text(text, reply_markup=markup)


async def cmd_topicid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Служебная команда — напиши её в нужной теме группы, чтобы узнать её ID."""
    thread_id = update.effective_message.message_thread_id
    if thread_id:
        await update.message.reply_text(f"ID этой темы: {thread_id}")
    else:
        await update.message.reply_text(
            "Это главная тема (или чат без тем) — отдельного ID темы тут нет, "
            "GROUP_TOPIC_ID указывать не нужно."
        )


async def cmd_coffee_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск опроса — на случай если нужно вне расписания (только для админа)."""
    await send_weekly_poll(context)
    await update.message.reply_text("Опрос отправлен в чат!")


async def cmd_pairs_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск распределения пар — на случай если нужно вне расписания (только для админа)."""
    status = await announce_pairs(context)
    await update.message.reply_text(PAIRS_STATUS_MESSAGES.get(status, "Готово."))


def _format_manual_entry(name, ident):
    if ident and ident.startswith("@"):
        return f'<a href="https://t.me/{ident[1:]}">{name}</a>'
    if ident and ident.isdigit():
        return f'<a href="tg://user?id={ident}">{name}</a>'
    return name


MANUAL_GROUP_RE = re.compile(r"^(\d+)\s+(.+)$")


async def cmd_manual_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разбивает на пары участниц, присланных вручную (без привязки к опросу). Только для админа.

    Использование — каждая участница с новой строки, в одном из форматов:
        Имя               — без ссылки, просто текстом
        Имя @username     — кликабельная ссылка на профиль
        Имя 123456789     — кликабельная ссылка по Telegram ID

    Чтобы задать саму пару вручную (не полагаясь на рандом), поставь перед именами
    одинаковый номер — все с одним номером станут одной группой/парой. Участницы без
    номера будут случайно разбиты между собой.
    """
    if not ADMIN_USER_ID or update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда доступна только администратору чата.")
        return

    current_poll_id = get_current_poll_id()
    if current_poll_id and is_pairs_published(current_poll_id):
        await update.message.reply_text(PAIRS_STATUS_MESSAGES["already_published"])
        return

    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text(
            "Пришли участниц — каждую с новой строки, в одном из форматов:\n\n"
            "Аня — просто по имени, без ссылки\n"
            "Маша @masha_username — кликабельная ссылка на профиль\n"
            "Катя 123456789 — кликабельная ссылка по Telegram ID\n"
            "1 Оля — номер перед именем закрепляет пару/группу (у всех с одним номером)\n\n"
            "Например (Аня+Маша и Катя+Оля — сама выбрала, Вера — в случайную пару с кем-то ещё):\n"
            "/manual_pairs\n1 Аня\n1 Маша @masha_username\n2 Катя 123456789\n2 Оля\nВера"
        )
        return

    groups = {}
    free_entries = []
    for line in text.split("\n"):
        line = line.strip().strip(",")
        if not line:
            continue
        group_match = MANUAL_GROUP_RE.match(line)
        group_num, rest = (group_match.group(1), group_match.group(2)) if group_match else (None, line)
        parts = rest.rsplit(maxsplit=1)
        if len(parts) == 2 and (parts[1].startswith("@") or parts[1].isdigit()):
            entry = (parts[0].strip(), parts[1])
        else:
            entry = (rest, None)
        if group_num:
            groups.setdefault(group_num, []).append(entry)
        else:
            free_entries.append(entry)

    total = len(free_entries) + sum(len(g) for g in groups.values())
    if total < 2:
        await update.message.reply_text("Нужно минимум 2 участницы (каждая с новой строки).")
        return

    pairs = make_pairs(free_entries) if free_entries else []
    for group in groups.values():
        random.shuffle(group)  # чтобы внутри закреплённой пары порядок тоже не был предсказуемым
        pairs.append(group)
    random.shuffle(pairs)  # чтобы закреплённые пары не были всегда первыми в списке

    lines = ["☕ Пары для Random Coffee на этой неделе:\n"]
    for group in pairs:
        names = " + ".join(_format_manual_entry(name, ident) for name, ident in group)
        lines.append(f"• {names}")
    lines.append("\nНапишите друг другу и договоритесь о встрече на этой неделе 🫶🏻")

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text="\n".join(lines),
        parse_mode="HTML",
        message_thread_id=GROUP_TOPIC_ID,
    )
    if current_poll_id:
        mark_pairs_published(current_poll_id)
    await update.message.reply_text("Пары отправлены в чат!")


async def cmd_unlock_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Снимает блокировку повторной отправки пар для текущего опроса (только для админа)."""
    if not ADMIN_USER_ID or update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда доступна только администратору чата.")
        return
    current_poll_id = get_current_poll_id()
    if not current_poll_id:
        await update.message.reply_text("Сейчас нет активного опроса Random Coffee.")
        return
    clear_pairs_published(current_poll_id)
    await update.message.reply_text("Готово, можно отправлять пары для этого опроса заново.")


USERNAME_RE = re.compile(r"(?:t\.me/|telegram\.me/|@)?([A-Za-z][A-Za-z0-9_]{4,31})$")


async def _lookup_by_id(bot, current_poll_id, partner_id):
    """Ищет имя участницы по её ID — сперва среди проголосовавших, потом напрямую у Telegram."""
    participants = get_participants(current_poll_id)
    found = next((name for uid, name, _ in participants if uid == partner_id), None)
    if found:
        return found
    try:
        chat = await bot.get_chat(partner_id)
        return full_name(chat)
    except Exception:
        return None


async def _lookup_by_username(bot, username):
    """Ищет участницу по @username или ссылке t.me/username."""
    try:
        chat = await bot.get_chat(f"@{username}")
        return chat.id, full_name(chat) or username
    except Exception:
        return None, None


async def _resolve_partner(bot, current_poll_id, message, text_override=None):
    """
    Определяет участницу из чего угодно: reply, пересланное сообщение,
    числовой ID, @username или ссылка t.me/username.
    Возвращает (partner_id, partner_name) или (None, None), если не вышло.
    text_override нужен для /pick — Message от Telegram неизменяемый, поэтому текст
    аргументов команды передаём отдельно, а не через message.text.
    """
    # Reply на сообщение (только в группе)
    if message.reply_to_message:
        partner = message.reply_to_message.from_user
        return partner.id, full_name(partner)

    # Пересланное сообщение
    forward_user = message.forward_origin.sender_user if (
        message.forward_origin and hasattr(message.forward_origin, "sender_user")
    ) else None
    if forward_user:
        return forward_user.id, full_name(forward_user)

    text = text_override if text_override is not None else (message.text or "").strip()
    text = text.strip()
    if not text:
        return None, None

    if text.isdigit():
        partner_id = int(text)
        name = await _lookup_by_id(bot, current_poll_id, partner_id)
        return (partner_id, name) if name else (None, None)

    match = USERNAME_RE.search(text)
    if match:
        return await _lookup_by_username(bot, match.group(1))

    return None, None


PICK_USAGE = (
    "Пришли что угодно из этого:\n"
    "— перешли сюда любое её сообщение\n"
    "— @username или просто username\n"
    "— ссылку на профиль (t.me/username)\n"
    "— её Telegram ID числом\n"
    "— или (в группе) ответь на её сообщение командой /pick"
)


async def cmd_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Только для администратора — заранее закрепить себе пару на неделю. См. PICK_USAGE."""
    if not ADMIN_USER_ID or update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда доступна только администратору чата.")
        return

    current_poll_id = get_current_poll_id()
    if not current_poll_id:
        await update.message.reply_text("Сейчас нет активного опроса Random Coffee — сначала должен появиться новый.")
        return

    if not update.message.reply_to_message and not context.args:
        await update.message.reply_text(PICK_USAGE)
        return

    # /pick <что угодно> — соберём аргумент(ы) обратно в текст для единого разбора
    text_arg = " ".join(context.args) if context.args else None
    partner_id, partner_name = await _resolve_partner(context.bot, current_poll_id, update.message, text_override=text_arg)
    if not partner_name:
        await update.message.reply_text(
            "Не смогла найти этого человека — проверь юзернейм/ID/ссылку, или пусть она сначала "
            "хоть раз напишет что-нибудь в общем чате, тогда бот её узнает."
        )
        return

    set_manual_pick(current_poll_id, partner_id, partner_name)
    await update.message.reply_text(
        f"Готово! На этой неделе твоей парой будет {partner_name} 🫶🏻"
    )


async def handle_pick_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит ответ на кнопку «Выбрать пару» — что угодно из PICK_USAGE."""
    if not context.user_data.get("awaiting_pick"):
        return  # обычное сообщение в личке, не связанное с /pick — игнорируем

    context.user_data["awaiting_pick"] = False

    current_poll_id = get_current_poll_id()
    if not current_poll_id:
        await update.message.reply_text("Сейчас нет активного опроса Random Coffee — сначала должен появиться новый.")
        return

    partner_id, partner_name = await _resolve_partner(context.bot, current_poll_id, update.message)
    if not partner_name:
        await update.message.reply_text(
            "Не смогла найти этого человека — проверь юзернейм/ID/ссылку, или перешли её сообщение целиком."
        )
        return

    set_manual_pick(current_poll_id, partner_id, partner_name)
    await update.message.reply_text(
        f"Готово! На этой неделе твоей парой будет {partner_name} 🫶🏻"
    )


def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(TypeHandler(Update, restrict_private_to_admin), group=-1)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("topicid", cmd_topicid))
    application.add_handler(CommandHandler("coffee_now", cmd_coffee_now))
    application.add_handler(CommandHandler("pairs_now", cmd_pairs_now))
    application.add_handler(CommandHandler("manual_pairs", cmd_manual_pairs))
    application.add_handler(CommandHandler("unlock_pairs", cmd_unlock_pairs))
    application.add_handler(CommandHandler("pick", cmd_pick))
    application.add_handler(CallbackQueryHandler(
        handle_help_buttons, pattern="^(coffee_now|pairs_now|pick_prompt|manual_start)$"
    ))
    application.add_handler(CallbackQueryHandler(handle_manual_picker, pattern=r"^(mtoggle:\d+|mrandom|mdone|mcancel)$"))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_pick_input))
    application.add_handler(PollAnswerHandler(handle_poll_answer))

    scheduler = AsyncIOScheduler(timezone="Europe/Vilnius")
    scheduler.add_job(
        send_weekly_poll,
        CronTrigger(day_of_week="sat", hour=12, minute=0),
        kwargs={"context": application},
    )
    scheduler.add_job(
        announce_pairs,
        CronTrigger(day_of_week="mon", hour=13, minute=0),
        kwargs={"context": application},
    )

    async def start_scheduler(app):
        # Планировщик стартуем только после того, как у Application появится
        # активный event loop (на новых версиях Python scheduler.start() до
        # этого момента падает с RuntimeError: no current event loop).
        scheduler.start()

    application.post_init = start_scheduler

    logger.info("Бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
