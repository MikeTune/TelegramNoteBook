"""Personal Knowledge Library - Telegram Bot.

Part 1: bot connects and responds to /start.
"""

import asyncio
import logging
import os
from html import escape

from telegram import Update
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes

from config import ALLOWED_USER_IDS, DATA_DIR, TIMEZONE_NAME, ensure_directories
from database import init_db
from handlers.notes import note_handlers
from handlers.reminders import reminder_handlers, start_scheduler, stop_scheduler
from handlers.export import export_handlers
from security import restricted

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logs full request URLs at INFO level, which include the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled handler exceptions with their update context."""
    if isinstance(context.error, Conflict):
        logger.critical(
            "Another bot instance is polling with the same token. Stop the other "
            "instance; this process will exit so the service manager can restart it."
        )
        application = getattr(context, "application", None)
        if application is not None:
            application.stop_running()
        return
    logger.error("Exception while handling an update:", exc_info=context.error)


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_text(
        f"Hi {escape(user.first_name, quote=False)}! \U0001F44B Welcome to "
        "<b>Personal Knowledge Library</b>.\n\n"
        "I'm your personal knowledge assistant.\n\n"
        "Send me a document using these three symbols:\n"
        "<b>@</b> Title — starts a new document\n"
        "<b>#</b> Subtitle / section heading\n"
        "<b>*</b> Paragraph / body text\n\n"
        "Example:\n"
        "<pre>@My Document\n#Section\n*Paragraph text</pre>\n\n"
        "Inside a paragraph, an apostrophe starts a new line: "
        "<code>first line'second line</code>.\n\n"
        "You can also attach photos. Send /done when finished, or /cancel to abort.\n\n"
        "Available commands:\n"
        "/start - show this welcome message\n"
        "/new - start a new document (choose a category)\n"
        "/library - browse your documents by category\n"
        "/remind - set a reminder (interactive date + time)\n"
        "/export - export a document to PDF\n"
        "/done - finish building current document\n"
        "/cancel - cancel current document",
        parse_mode="HTML",
    )


async def _post_init(application: Application) -> None:
    """Attach the running bot and event loop to the reminder scheduler."""
    start_scheduler(application.bot, asyncio.get_running_loop())


def main() -> None:
    """Start the bot."""
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN not found in .env file. "
            "Please create a .env file with BOT_TOKEN=your_token and try again."
        )

    ensure_directories()

    # Initialize the database schema, restore reminders, and migrate legacy data
    init_db()

    if ALLOWED_USER_IDS:
        logger.info("Access restricted to %d Telegram user(s).", len(ALLOWED_USER_IDS))
    else:
        logger.warning(
            "ALLOWED_USER_IDS is empty: any Telegram user can use this bot."
        )
    logger.info("Data directory: %s", DATA_DIR)
    logger.info("Timezone: %s", TIMEZONE_NAME)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .job_queue(None)  # reminders use the persistent APScheduler in handlers.reminders
        .post_init(_post_init)
        .build()
    )

    # Part 1 handler
    application.add_handler(CommandHandler("start", start))

    # Parts 2, 3, 5: notes (doc builder, library, view, edit, delete, photos)
    for handler in note_handlers:
        application.add_handler(handler)

    # Part 4: reminders (commands + callbacks; text handled by notes router)
    for handler in reminder_handlers:
        application.add_handler(handler)

    # Part 5: export
    for handler in export_handlers:
        application.add_handler(handler)

    application.add_error_handler(on_error)

    logger.info("Personal Knowledge Library bot is starting...")

    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        stop_scheduler()


if __name__ == "__main__":
    main()