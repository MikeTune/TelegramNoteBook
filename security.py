"""Telegram access control for the private personal bot.

Set ``ALLOWED_USER_IDS`` in ``.env`` to the owner's numeric Telegram user ID
(comma-separated for several accounts).  When the setting is empty the bot is
open, which is convenient for local development only.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from config import ALLOWED_USER_IDS

logger = logging.getLogger(__name__)


def is_allowed(user_id: int | None) -> bool:
    """Return True when the user may use the bot."""
    if not ALLOWED_USER_IDS:
        return True
    return user_id is not None and user_id in ALLOWED_USER_IDS


def restricted(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Reject updates from Telegram users that are not on the allow-list."""

    @wraps(func)
    async def wrapper(update, context):
        user = update.effective_user
        user_id = user.id if user else None
        if is_allowed(user_id):
            return await func(update, context)

        logger.warning("Blocked unauthorized Telegram user %s", user_id)
        if update.callback_query:
            await update.callback_query.answer(
                "This bot is private.", show_alert=True
            )
        elif update.effective_message:
            await update.effective_message.reply_text("This bot is private.")
        return None

    return wrapper
