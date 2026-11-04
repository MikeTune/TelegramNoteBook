"""Reminder handlers.

Part 4: /remind with interactive date + time selection.

Reminders are persisted in SQLite and rescheduled when the bot starts, so they
survive restarts.  All reminder times are interpreted in ``TIMEZONE``
(``Africa/Addis_Ababa`` by default), independent of the VPS clock timezone.
"""

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
)

from apscheduler.schedulers.background import BackgroundScheduler

from config import TIMEZONE_NAME
from database import (
    add_reminder,
    get_pending_reminders,
    get_reminder,
    mark_reminder_sent,
)
from security import restricted

logger = logging.getLogger(__name__)

try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError as exc:
    raise RuntimeError(
        f"Invalid TIMEZONE {TIMEZONE_NAME!r}. Set a valid IANA timezone and "
        "install the tzdata package if the system has no timezone database."
    ) from exc

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone=TZ)
_scheduler_started = False
_bot = None
_loop: asyncio.AbstractEventLoop | None = None


def _schedule_job(reminder_id: int, run_at: datetime) -> None:
    scheduler.add_job(
        _fire_reminder,
        "date",
        run_date=run_at,
        args=[reminder_id],
        id=f"reminder_{reminder_id}",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )


def reload_reminders() -> None:
    """Reschedule every undelivered reminder from SQLite.

    Runs at startup (after a restart) and is safe to call again.  Reminders
    whose time passed while the bot was offline are delivered shortly after
    startup instead of being lost.
    """
    if not _scheduler_started:
        return

    now = datetime.now(TZ)
    for reminder in get_pending_reminders():
        try:
            run_at = datetime.fromisoformat(reminder["run_at"])
        except (TypeError, ValueError):
            logger.exception(
                "Reminder %s has an invalid run_at value", reminder["id"]
            )
            continue

        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=TZ)
        else:
            run_at = run_at.astimezone(TZ)

        if run_at <= now:
            logger.info(
                "Reminder %s was missed while offline; delivering shortly",
                reminder["id"],
            )
            run_at = now + timedelta(seconds=5)

        _schedule_job(reminder["id"], run_at)
        logger.info(
            "Scheduled reminder %s for %s", reminder["id"], run_at.isoformat()
        )


def start_scheduler(bot=None, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Start the scheduler and restore persisted reminders."""
    global _scheduler_started, _bot, _loop

    if bot is not None:
        _bot = bot
    if loop is not None:
        _loop = loop

    if not _scheduler_started:
        scheduler.start()
        _scheduler_started = True

    reload_reminders()


def stop_scheduler() -> None:
    """Stop the scheduler; pending reminders remain in SQLite."""
    global _scheduler_started, _bot, _loop

    if _scheduler_started and scheduler.running:
        scheduler.shutdown(wait=False)
    _scheduler_started = False
    _bot = None
    _loop = None


def _fire_reminder(reminder_id: int) -> None:
    """Scheduler-thread entry point: deliver a due reminder on the bot loop."""
    reminder = get_reminder(reminder_id)
    if not reminder or reminder["sent"]:
        return

    bot, loop = _bot, _loop
    if bot is None or loop is None or loop.is_closed():
        logger.error(
            "Reminder %s is due but the bot event loop is unavailable; "
            "it will be retried after the next restart",
            reminder_id,
        )
        return

    try:
        future = asyncio.run_coroutine_threadsafe(
            _deliver_reminder(bot, reminder), loop
        )
        future.result(timeout=60)
    except Exception:
        logger.exception("Failed to deliver reminder %s", reminder_id)


async def _deliver_reminder(bot, reminder: dict) -> None:
    await bot.send_message(
        chat_id=reminder["chat_id"],
        text=f"⏰ Reminder: {reminder['text']}",
    )
    mark_reminder_sent(reminder["id"])
    logger.info("Delivered reminder %s", reminder["id"])


def schedule_reminder(
    bot,
    chat_id: int,
    run_date: datetime,
    text: str,
    user_id: int | None = None,
) -> str:
    """Persist a reminder and schedule it in the current process."""
    global _bot
    if bot is not None:
        _bot = bot

    if run_date.tzinfo is None:
        run_date = run_date.replace(tzinfo=TZ)
    else:
        run_date = run_date.astimezone(TZ)

    reminder_id = add_reminder(
        user_id if user_id is not None else chat_id,
        chat_id,
        text,
        run_date.isoformat(),
    )
    _schedule_job(reminder_id, run_date)
    logger.info(
        "Persisted reminder %s for chat %s at %s",
        reminder_id,
        chat_id,
        run_date.isoformat(),
    )
    return f"reminder_{reminder_id}"


# ---------------------------------------------------------------------------
# Conversation state keys (stored in context.user_data)
# ---------------------------------------------------------------------------
REMIND_DATE = "remind_date"
REMIND_TIME = "remind_time"
REMIND_MSG = "remind_msg"
REMIND_STEP = "remind_step"  # 'date' | 'time' | 'msg'


def _date_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard for date selection: Today, Tomorrow, +2, +3, +7, Custom."""
    today = datetime.now(TZ).date()
    buttons = [
        InlineKeyboardButton("Today", callback_data=f"remind_date:{today.isoformat()}"),
        InlineKeyboardButton(
            "Tomorrow", callback_data=f"remind_date:{(today + timedelta(days=1)).isoformat()}"
        ),
        InlineKeyboardButton(
            "+2 days", callback_data=f"remind_date:{(today + timedelta(days=2)).isoformat()}"
        ),
        InlineKeyboardButton(
            "+3 days", callback_data=f"remind_date:{(today + timedelta(days=3)).isoformat()}"
        ),
        InlineKeyboardButton(
            "+7 days", callback_data=f"remind_date:{(today + timedelta(days=7)).isoformat()}"
        ),
        InlineKeyboardButton("Custom date", callback_data="remind_date:custom"),
    ]
    return InlineKeyboardMarkup([buttons[i : i + 3] for i in range(0, len(buttons), 3)])


def _time_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard for common times plus a custom option."""
    common = ["08:00", "09:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"]
    buttons = [InlineKeyboardButton(t, callback_data=f"remind_time:{t}") for t in common]
    buttons.append(InlineKeyboardButton("Custom time", callback_data="remind_time:custom"))
    return InlineKeyboardMarkup([buttons[i : i + 3] for i in range(0, len(buttons), 3)])


def _validate_date_str(d: str) -> date | None:
    try:
        return date.fromisoformat(d)
    except ValueError:
        return None


def _validate_time_str(t: str) -> time | None:
    if not re.fullmatch(r"\d{1,2}:\d{2}", t):
        return None
    try:
        h, m = map(int, t.split(":"))
        if h > 23 or m > 59:
            return None
        return time(hour=h, minute=m)
    except ValueError:
        return None


@restricted
async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the reminder flow: pick a date first."""
    context.user_data[REMIND_STEP] = "date"
    context.user_data[REMIND_DATE] = None
    context.user_data[REMIND_TIME] = None
    context.user_data[REMIND_MSG] = None

    await update.message.reply_text(
        "⏰ Set a reminder\n\nStep 1 of 3 — Choose a date:",
        reply_markup=_date_keyboard(),
    )


@restricted
async def remind_date_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    _, val = data.split(":", 1)

    if val == "custom":
        context.user_data[REMIND_STEP] = "date_custom"
        await query.edit_message_text(
            "Send the date as YYYY-MM-DD (e.g. 2026-11-15)."
        )
        return

    d = _validate_date_str(val)
    if not d:
        await query.edit_message_text("Invalid date. Please try again.")
        return

    context.user_data[REMIND_DATE] = d.isoformat()
    context.user_data[REMIND_STEP] = "time"

    await query.edit_message_text(
        f"✅ Date set: {d.strftime('%A, %B %d, %Y')}\n\n"
        "Step 2 of 3 — Choose a time:",
        reply_markup=_time_keyboard(),
    )


async def remind_date_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get(REMIND_STEP) != "date_custom":
        return

    text = (update.message.text or "").strip()
    d = _validate_date_str(text)
    if not d:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.")
        return

    context.user_data[REMIND_DATE] = d.isoformat()
    context.user_data[REMIND_STEP] = "time"

    await update.message.reply_text(
        f"✅ Date set: {d.strftime('%A, %B %d, %Y')}\n\n"
        "Step 2 of 3 — Choose a time:",
        reply_markup=_time_keyboard(),
    )


@restricted
async def remind_time_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    _, val = data.split(":", 1)

    if val == "custom":
        context.user_data[REMIND_STEP] = "time_custom"
        await query.edit_message_text(
            "Send the time as HH:MM (e.g. 14:30)."
        )
        return

    t = _validate_time_str(val)
    if not t:
        await query.edit_message_text("Invalid time. Please try again.")
        return

    context.user_data[REMIND_TIME] = val
    context.user_data[REMIND_STEP] = "msg"

    await query.edit_message_text(
        f"✅ Time set: {val}\n\n"
        "Step 3 of 3 — Send the reminder message text."
    )


async def remind_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get(REMIND_STEP) != "time_custom":
        return

    text = (update.message.text or "").strip()
    t = _validate_time_str(text)
    if not t:
        await update.message.reply_text("Invalid time format. Use HH:MM.")
        return

    context.user_data[REMIND_TIME] = text
    context.user_data[REMIND_STEP] = "msg"

    await update.message.reply_text(
        f"✅ Time set: {text}\n\n"
        "Step 3 of 3 — Send the reminder message text."
    )


async def remind_msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get(REMIND_STEP) != "msg":
        return

    message = (update.message.text or "").strip()
    if not message:
        await update.message.reply_text("Please send a non-empty message.")
        return

    date_str = context.user_data.get(REMIND_DATE)
    time_str = context.user_data.get(REMIND_TIME)
    if not date_str or not time_str:
        await update.message.reply_text("Date or time missing. Start over with /remind.")
        context.user_data.pop(REMIND_STEP, None)
        return

    try:
        d = date.fromisoformat(date_str)
        t = time.fromisoformat(time_str)
        run_date = datetime.combine(d, t, tzinfo=TZ)
    except ValueError:
        await update.message.reply_text("Invalid date/time. Start over with /remind.")
        context.user_data.pop(REMIND_STEP, None)
        return

    now = datetime.now(TZ)
    if run_date <= now:
        await update.message.reply_text(
            "⚠ The selected date/time is in the past. Please choose a future date/time."
        )
        return

    schedule_reminder(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        run_date=run_date,
        text=message,
        user_id=update.effective_user.id,
    )

    context.user_data.pop(REMIND_STEP, None)
    context.user_data.pop(REMIND_DATE, None)
    context.user_data.pop(REMIND_TIME, None)
    context.user_data.pop(REMIND_MSG, None)

    await update.message.reply_text(
        f"✅ Reminder set for {run_date.strftime('%A, %B %d, %Y at %H:%M')} "
        f"({TIMEZONE_NAME})\n"
        f"📨 \"{message}\""
    )


async def remind_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route text messages when a reminder step is awaiting input."""
    step = context.user_data.get(REMIND_STEP)
    if step == "date_custom":
        await remind_date_text(update, context)
    elif step == "time_custom":
        await remind_time_text(update, context)
    elif step == "msg":
        await remind_msg_text(update, context)


# Handlers to register in main.py
reminder_handlers = [
    CommandHandler("remind", remind_command),
    CallbackQueryHandler(remind_date_cb, pattern=r"^remind_date:"),
    CallbackQueryHandler(remind_time_cb, pattern=r"^remind_time:"),
    # Note: remind_text_router (MessageHandler) is registered via notes.py's
    # receive_message to avoid duplicate TEXT handler conflicts.
]
