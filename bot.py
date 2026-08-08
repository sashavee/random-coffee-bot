"""
Random Coffee бот для "Подруги в Вильнюсе"

Логика:
- Каждую пятницу в 12:00 бот присылает в чат опрос "Участвуешь в Random Coffee на этой неделе?"
- Каждый понедельник в 10:00 бот собирает всех, кто проголосовал "Да",
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
import sqlite3
from datetime import datetime

from telegram import Update, Poll
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    PollAnswerHandler,
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
    """Отправляется каждую пятницу."""
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
        add_participant(current_poll_id, user.id, user.first_name, user.username)
        logger.info(f"{user.first_name} присоединилась к Random Coffee")
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


async def announce_pairs(context: ContextTypes.DEFAULT_TYPE):
    """Отправляется каждый понедельник — собирает пары и публикует результат."""
    current_poll_id = get_current_poll_id()
    if not current_poll_id:
        logger.info("Нет активного опроса — пропускаем распределение пар")
        return

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
        return

    pairs = make_pairs(participants) if len(participants) >= 2 else []
    if fixed_pair:
        pairs.insert(0, fixed_pair)  # пара админа — первая в списке

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
    logger.info(f"Опубликованы пары: {len(pairs)} групп")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот Random Coffee для «Подруги в Вильнюсе». "
        "Каждую пятницу в чате появляется опрос — проголосуй, если хочешь "
        "случайную пару для кофе на неделе, а по понедельникам я объявлю пары ☕"
    )


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
    await announce_pairs(context)


async def cmd_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Только для администратора. Два способа использования:

    1) В группе, ответом (reply) на сообщение нужной участницы:
       /pick

    2) В личке с ботом, указав её Telegram user_id:
       /pick 123456789

    Тогда при распределении пар она автоматически станет твоей парой,
    а остальные будут разбиты случайно между собой. Выбор через личку
    никак не виден в общем чате.
    """
    if not ADMIN_USER_ID or update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда доступна только администратору чата.")
        return

    current_poll_id = get_current_poll_id()
    if not current_poll_id:
        await update.message.reply_text("Сейчас нет активного опроса Random Coffee — сначала должен появиться новый.")
        return

    partner_id = None
    partner_name = None

    # Способ 1 — reply на сообщение в группе
    if update.message.reply_to_message:
        partner = update.message.reply_to_message.from_user
        partner_id, partner_name = partner.id, partner.first_name

    # Способ 2 — id передан текстом (можно в личке, чтобы никто не видел)
    elif context.args:
        try:
            partner_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("ID должен быть числом. Например: /pick 123456789")
            return

        # пробуем найти имя среди уже проголосовавших в этом опросе
        participants = get_participants(current_poll_id)
        found = next((name for uid, name, _ in participants if uid == partner_id), None)
        if found:
            partner_name = found
        else:
            # запасной вариант — спросить у Telegram напрямую
            try:
                chat = await context.bot.get_chat(partner_id)
                partner_name = chat.first_name or chat.username or "Без имени"
            except Exception:
                await update.message.reply_text(
                    "Не смогла найти этого человека — проверь ID, или пусть она сначала "
                    "хоть раз напишет что-нибудь в общем чате, тогда бот её узнает."
                )
                return
    else:
        await update.message.reply_text(
            "Использование:\n"
            "— В группе, ответом на её сообщение: /pick\n"
            "— В личке с ботом: /pick 123456789 (её Telegram ID)"
        )
        return

    set_manual_pick(current_poll_id, partner_id, partner_name)
    await update.message.reply_text(
        f"Готово! На этой неделе твоей парой будет {partner_name} 🫶🏻"
    )


def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("topicid", cmd_topicid))
    application.add_handler(CommandHandler("coffee_now", cmd_coffee_now))
    application.add_handler(CommandHandler("pairs_now", cmd_pairs_now))
    application.add_handler(CommandHandler("pick", cmd_pick))
    application.add_handler(PollAnswerHandler(handle_poll_answer))

    scheduler = AsyncIOScheduler(timezone="Europe/Vilnius")
    scheduler.add_job(
        send_weekly_poll,
        CronTrigger(day_of_week="fri", hour=12, minute=0),
        kwargs={"context": application},
    )
    scheduler.add_job(
        announce_pairs,
        CronTrigger(day_of_week="mon", hour=10, minute=0),
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
