"""Note saving and browsing handlers.

Part 2: save documents with category selection (7 categories).
Part 3: /library browsing (categories -> documents -> view document).
Part 5: Symbol-based document structure (@ title, # subtitle, * paragraph),
        optional photo attachments, individual document deletion.

Document input is parsed only by :mod:`document_parser`; this module persists
the blocks it returns.  It never inspects raw text for the old field-prefix
format.
"""

import os
from html import escape

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    CommandHandler,
)

from database import (
    create_document,
    add_section,
    add_photo,
    get_documents_by_category,
    get_document_by_id,
    delete_document,
    get_categories_with_counts,
    NEW_CATEGORIES,
    PHOTOS_DIR,
)
from document_parser import (
    TITLE,
    MARKER_FOR_TYPE,
    parse_document_blocks,
)
from security import restricted

CATEGORIES = NEW_CATEGORIES

HELP_EXAMPLE = "@My Document\n#Section\n*Paragraph text"
CONTINUE_HINT = (
    "Continue with @ / # / *, attach a photo, or send /done to finish "
    "(/cancel to abort)."
)


class DocumentInputError(Exception):
    """Raised when blocks cannot be persisted as a document."""


# ---------------------------------------------------------------------------
# Keyboard / text helpers
# ---------------------------------------------------------------------------


def _category_keyboard() -> InlineKeyboardMarkup:
    """Build a 2-column keyboard: 4 categories on the left, 3 on the right."""
    left = CATEGORIES[:4]
    right = CATEGORIES[4:]
    rows = []
    for i in range(4):
        row = []
        if i < len(left):
            row.append(InlineKeyboardButton(left[i], callback_data=f"cat:{left[i]}"))
        if i < len(right):
            row.append(InlineKeyboardButton(right[i], callback_data=f"cat:{right[i]}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _pre(text: str) -> str:
    """Render literal text as a Telegram HTML <pre> block."""
    return f"<pre>{escape(text, quote=False)}</pre>"


def _usage_text(prefix: str | None = None) -> str:
    """Symbol-format instructions (the only document-input help text)."""
    parts = []
    if prefix:
        parts.append(prefix)
    parts.append(
        "Use the three document symbols:\n"
        "<b>@</b> Title — starts a new document\n"
        "<b>#</b> Subtitle / section heading\n"
        "<b>*</b> Paragraph / body text"
    )
    parts.append(f"Example:\n{_pre(HELP_EXAMPLE)}")
    return "\n\n".join(parts)


def _category_prompt_text() -> str:
    return "📂 Choose a category for your new document:"


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------


def _is_doc_mode(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("doc_mode"))


def _is_awaiting_category(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("awaiting_category"))


def _exit_doc_mode(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Leave document-building mode, keeping only last_category."""
    for key in (
        "doc_mode",
        "awaiting_category",
        "pending_text",
        "current_category",
        "current_doc_id",
        "current_doc_title",
        "current_section_id",
        "current_doc_is_new",
    ):
        context.user_data.pop(key, None)


def _remember_document(
    context: ContextTypes.DEFAULT_TYPE,
    doc_id: int,
    title: str | None,
    section_id: int | None,
    is_new: bool = False,
) -> None:
    context.user_data["current_doc_id"] = doc_id
    context.user_data["current_doc_title"] = title
    context.user_data["current_section_id"] = section_id
    if is_new:
        context.user_data["current_doc_is_new"] = True


def _get_owned_document(doc_id: int, user_id: int) -> dict | None:
    """Fetch a document only when it belongs to ``user_id``."""
    doc = get_document_by_id(doc_id)
    if not doc or doc.get("user_id") != user_id:
        return None
    return doc


# ---------------------------------------------------------------------------
# Persistence of parsed blocks
# ---------------------------------------------------------------------------


def _apply_blocks(
    user_id: int,
    category: str,
    blocks: list[dict],
    doc_id: int | None = None,
    doc_title: str | None = None,
) -> tuple[int | None, str | None, int | None, list[dict]]:
    """Persist parsed blocks and return ``(doc_id, title, section_id, events)``.

    A ``@`` block always creates a new document; ``#``/``*`` blocks append to
    the document that is currently being built.  ``events`` describes what
    happened so the caller can report it to the user.
    """
    if not blocks:
        raise DocumentInputError("No document content found.")

    if doc_id is None and blocks[0]["type"] != TITLE:
        raise DocumentInputError(
            "A document must start with an <b>@Title</b> block."
        )

    events: list[dict] = []
    active_event: dict | None = None
    last_section_id: int | None = None

    for block in blocks:
        block_type = block["type"]
        content = block["content"]

        if block_type == TITLE:
            doc_id = create_document(user_id, category, content)
            doc_title = content
            last_section_id = None
            active_event = {
                "action": "created",
                "doc_id": doc_id,
                "title": doc_title,
                "blocks": [],
            }
            events.append(active_event)
            continue

        if doc_id is None:
            raise DocumentInputError(
                "A document must start with an <b>@Title</b> block."
            )

        if active_event is None:
            active_event = {
                "action": "appended",
                "doc_id": doc_id,
                "title": doc_title,
                "blocks": [],
            }
            events.append(active_event)

        last_section_id = add_section(doc_id, block_type, content)
        active_event["blocks"].append(block)

    return doc_id, doc_title, last_section_id, events


def _outline(blocks: list[dict], limit_per_block: int = 100, limit_total: int = 800) -> str:
    """Render a compact, escaped outline of blocks for confirmation messages."""
    if limit_total <= 0:
        return ""
    lines: list[str] = []
    used = 0
    for block in blocks:
        marker = MARKER_FOR_TYPE[block["type"]]
        summary = " ".join(block["content"].split())
        if len(summary) > limit_per_block:
            summary = summary[: limit_per_block - 1].rstrip() + "…"
        line = f"{marker} {escape(summary, quote=False)}"
        if used + len(line) > limit_total:
            lines.append("…")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _format_events(events: list[dict], category: str | None = None) -> str:
    """Build the confirmation message for one processed input message.

    The outline is bounded so that even a deliberately huge message cannot
    exceed Telegram's 4096-character limit.
    """
    max_events = 8
    outline_budget = 2400
    parts: list[str] = []

    for event in events[:max_events]:
        title = escape(event["title"] or "document", quote=False)
        if event["action"] == "created":
            header = f"✅ Document created: <b>{title}</b>"
            if category:
                header += f"\nCategory: {escape(category, quote=False)}"
        else:
            header = f"✅ Added to <b>{title}</b>:"

        outline = _outline(event["blocks"], limit_total=outline_budget)
        outline_budget -= len(outline)
        parts.append(f"{header}\n\n{outline}" if outline else header)

    if len(events) > max_events:
        parts.append(f"…and {len(events) - max_events} more document(s).")
    elif len(events) > 1:
        last_title = escape(events[-1]["title"] or "document", quote=False)
        parts.append(f"Active document: <b>{last_title}</b>")

    parts.append(CONTINUE_HINT)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Text routing: document input
# ---------------------------------------------------------------------------


@restricted
async def receive_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for any non-command text message.

    Reminder input takes priority; otherwise the message is either a new
    symbol-based document (which first needs a category) or an extension of
    the document currently being built.
    """
    step = context.user_data.get("remind_step")
    if step in ("date_custom", "time_custom", "msg"):
        from handlers.reminders import remind_text_router

        await remind_text_router(update, context)
        return

    text = update.message.text or ""

    if _is_doc_mode(context):
        await _handle_document_input(update, context, text)
        return

    blocks = parse_document_blocks(text)

    if not blocks:
        await update.message.reply_text(_usage_text(), parse_mode="HTML")
        return

    if blocks[0]["type"] != TITLE and not (
        _is_awaiting_category(context) and context.user_data.get("pending_text")
    ):
        await update.message.reply_text(
            _usage_text("Start a new document with an <b>@Title</b> block."),
            parse_mode="HTML",
        )
        return

    # Hold the document text until the user picks a category.  Additional
    # messages sent before the category is chosen are appended to the pending
    # document instead of being lost.
    if _is_awaiting_category(context):
        pending = context.user_data.get("pending_text", "")
        context.user_data["pending_text"] = f"{pending}\n{text}".strip()
    else:
        context.user_data["pending_text"] = text
        context.user_data["awaiting_category"] = True

    await update.message.reply_text(
        _category_prompt_text(),
        reply_markup=_category_keyboard(),
    )


async def _handle_document_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Persist a symbol-based message into the open document."""
    blocks = parse_document_blocks(text)

    if not blocks:
        await update.message.reply_text(
            _usage_text("No @ / # / * blocks were found in that message."),
            parse_mode="HTML",
        )
        return

    doc_id = context.user_data.get("current_doc_id")
    doc_title = context.user_data.get("current_doc_title")
    category = context.user_data.get("current_category") or context.user_data.get(
        "last_category"
    )

    if not category:
        # Should not happen while doc_mode is set, but never guess a category.
        context.user_data["awaiting_category"] = True
        context.user_data["pending_text"] = text
        context.user_data.pop("doc_mode", None)
        await update.message.reply_text(
            _category_prompt_text(),
            reply_markup=_category_keyboard(),
        )
        return

    try:
        doc_id, doc_title, section_id, events = _apply_blocks(
            user_id=update.effective_user.id,
            category=category,
            blocks=blocks,
            doc_id=doc_id,
            doc_title=doc_title,
        )
    except DocumentInputError as exc:
        await update.message.reply_text(
            _usage_text(str(exc)),
            parse_mode="HTML",
        )
        return

    _remember_document(
        context,
        doc_id,
        doc_title,
        section_id,
        is_new=events[-1]["action"] == "created",
    )
    await update.message.reply_text(
        _format_events(events, category),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Category selection
# ---------------------------------------------------------------------------


@restricted
async def category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply the chosen category, then create the pending symbol document."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("cat:"):
        return

    category = data.split(":", 1)[1]
    if category not in CATEGORIES:
        await query.edit_message_text(
            "Unknown category. Send /new to choose again."
        )
        return

    pending = context.user_data.get("pending_text")

    # A stale category keyboard must not silently abandon an open document.
    if _is_doc_mode(context) and context.user_data.get("current_doc_id") and not pending:
        doc_title = context.user_data.get("current_doc_title") or "your document"
        await query.edit_message_text(
            f"A document is already open: <b>{escape(doc_title, quote=False)}</b>.\n"
            "Send /done to save it or /cancel to discard it, then /new again.",
            parse_mode="HTML",
        )
        return

    context.user_data.pop("pending_text", None)
    context.user_data.pop("awaiting_category", None)
    context.user_data["doc_mode"] = True
    context.user_data["current_category"] = category
    context.user_data["last_category"] = category
    context.user_data["current_doc_id"] = None
    context.user_data["current_doc_title"] = None
    context.user_data["current_section_id"] = None
    context.user_data["current_doc_is_new"] = False

    if not pending:
        await query.edit_message_text(
            f"✅ Category <b>{escape(category, quote=False)}</b> selected.\n\n"
            "Send your document with these three symbols:\n"
            "<b>@</b> Title — starts the document\n"
            "<b>#</b> Subtitle / section heading\n"
            "<b>*</b> Paragraph / body text\n\n"
            f"Example:\n{_pre(HELP_EXAMPLE)}\n\n"
            "Attach photos any time. Send /done when finished, or /cancel to abort.",
            parse_mode="HTML",
        )
        return

    blocks = parse_document_blocks(pending)
    if not blocks or blocks[0]["type"] != TITLE:
        await query.edit_message_text(
            _usage_text(
                f"✅ Category <b>{escape(category, quote=False)}</b> selected.\n\n"
                "That message needs to start with an <b>@Title</b> block."
            ),
            parse_mode="HTML",
        )
        return

    try:
        doc_id, doc_title, section_id, events = _apply_blocks(
            user_id=query.from_user.id,
            category=category,
            blocks=blocks,
        )
    except DocumentInputError as exc:
        await query.edit_message_text(
            _usage_text(str(exc)),
            parse_mode="HTML",
        )
        return

    _remember_document(context, doc_id, doc_title, section_id, is_new=True)
    await query.edit_message_text(
        _format_events(events, category),
        parse_mode="HTML",
    )


@restricted
async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/new: choose a category, then start writing immediately with symbols."""
    if _is_doc_mode(context) and context.user_data.get("current_doc_id"):
        doc_title = context.user_data.get("current_doc_title") or "your document"
        await update.message.reply_text(
            f"A document is already open: <b>{escape(doc_title, quote=False)}</b>.\n"
            "Send /done to save it or /cancel to discard it, then send /new again.",
            parse_mode="HTML",
        )
        return

    _exit_doc_mode(context)
    context.user_data["awaiting_category"] = True
    await update.message.reply_text(
        _category_prompt_text(),
        reply_markup=_category_keyboard(),
    )


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------


@restricted
async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an uploaded photo, associating it with the current document/section."""
    if not _is_doc_mode(context):
        if _is_awaiting_category(context):
            message = _category_prompt_text()
            await update.message.reply_text(
                message,
                reply_markup=_category_keyboard(),
            )
        else:
            await update.message.reply_text(
                _usage_text(
                    "Photos are attached to a document. Start one with an "
                    "<b>@Title</b> block first."
                ),
                parse_mode="HTML",
            )
        return

    doc_id = context.user_data.get("current_doc_id")
    if not doc_id:
        await update.message.reply_text(
            _usage_text(
                "No document has been started yet. Send an <b>@Title</b> block first."
            ),
            parse_mode="HTML",
        )
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id

    tg_file = await context.bot.get_file(file_id)
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    file_path = os.path.join(PHOTOS_DIR, f"{file_id}.jpg")
    await tg_file.download_to_drive(file_path)

    section_id = context.user_data.get("current_section_id")
    add_photo(doc_id, section_id, file_id, file_path)

    where = "to the current section" if section_id else "to the document"
    await update.message.reply_text(
        f"📷 Photo saved {where}.\n{CONTINUE_HINT}"
    )


# ---------------------------------------------------------------------------
# /done and /cancel
# ---------------------------------------------------------------------------


@restricted
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Finish building the current document."""
    if not _is_doc_mode(context):
        await update.message.reply_text(
            "No document is being built. Send /new to start one."
        )
        return

    doc_id = context.user_data.get("current_doc_id")
    if not doc_id:
        await update.message.reply_text(
            _usage_text(
                "No document content yet. Send an <b>@Title</b> block first, "
                "or /cancel to abort."
            ),
            parse_mode="HTML",
        )
        return

    doc = get_document_by_id(doc_id)
    if not doc:
        _exit_doc_mode(context)
        await update.message.reply_text(
            "That document no longer exists. Send /new to start another one."
        )
        return

    _exit_doc_mode(context)

    subtitles = sum(1 for s in doc["sections"] if s["section_type"] == "subtitle")
    paragraphs = sum(1 for s in doc["sections"] if s["section_type"] == "paragraph")
    inline_photos = sum(len(s.get("photos", [])) for s in doc["sections"])
    standalone = len(doc.get("standalone_photos", []))

    await update.message.reply_text(
        f"✅ Document saved: <b>{escape(doc['title'], quote=False)}</b>\n"
        f"Category: <b>{escape(doc['category'], quote=False)}</b>\n"
        f"Sections: {subtitles} subtitle(s), {paragraphs} paragraph(s)\n"
        f"Photos: {standalone} standalone + {inline_photos} inline.",
        parse_mode="HTML",
    )


@restricted
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the current document being built.

    A document created in this session is discarded.  Cancelling an edit of an
    existing library document only leaves edit mode; the saved document (and
    any sections already added to it) is kept.
    """
    if not _is_doc_mode(context):
        if _is_awaiting_category(context):
            _exit_doc_mode(context)
            await update.message.reply_text("❌ Category selection cancelled.")
            return
        await update.message.reply_text("No document is being built.")
        return

    doc_id = context.user_data.get("current_doc_id")
    is_new = context.user_data.get("current_doc_is_new", True)
    if doc_id and is_new:
        delete_document(doc_id)

    _exit_doc_mode(context)

    if doc_id and not is_new:
        await update.message.reply_text(
            "Editing stopped. The document remains saved in your library."
        )
    else:
        await update.message.reply_text("❌ Document creation cancelled.")


# ---------------------------------------------------------------------------
# Library browsing
# ---------------------------------------------------------------------------


@restricted
async def library_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/library: show categories with document counts."""
    user_id = update.effective_user.id
    counts = {c["category"]: c["count"] for c in get_categories_with_counts(user_id)}

    left = CATEGORIES[:4]
    right = CATEGORIES[4:]
    rows = []
    for i in range(4):
        row = []
        if i < len(left):
            cat = left[i]
            row.append(
                InlineKeyboardButton(
                    f"{cat} ({counts.get(cat, 0)})", callback_data=f"browse:{cat}"
                )
            )
        if i < len(right):
            cat = right[i]
            row.append(
                InlineKeyboardButton(
                    f"{cat} ({counts.get(cat, 0)})", callback_data=f"browse:{cat}"
                )
            )
        rows.append(row)

    await update.message.reply_text(
        "📚 <b>Personal Knowledge Library</b>\nChoose a category to browse:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )


@restricted
async def browse_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List documents in a category."""
    query = update.callback_query
    await query.answer()

    category = query.data.split(":", 1)[1]
    docs = get_documents_by_category(category, query.from_user.id)

    if not docs:
        await query.edit_message_text(
            f"📂 <b>{escape(category, quote=False)}</b>\n\nNo documents yet.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅ Back", callback_data="lib:back")]]
            ),
        )
        return

    buttons = []
    for doc in docs:
        label = doc["title"][:50]
        buttons.append(
            InlineKeyboardButton(label, callback_data=f"view:{doc['id']}")
        )
    buttons.append(InlineKeyboardButton("⬅ Back to categories", callback_data="lib:back"))

    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    await query.edit_message_text(
        f"📂 <b>{escape(category, quote=False)}</b> — {len(docs)} document(s)",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )


@restricted
async def view_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a document's content with action buttons."""
    query = update.callback_query
    await query.answer()

    doc_id = int(query.data.split(":", 1)[1])
    doc = _get_owned_document(doc_id, query.from_user.id)
    if not doc:
        await query.edit_message_text("Document not found.")
        return

    parts = [
        f"📝 <b>{escape(doc['title'], quote=False)}</b>\n"
        f"Category: <b>{escape(doc['category'], quote=False)}</b>"
    ]
    for section in doc["sections"]:
        if section["section_type"] == "subtitle":
            parts.append(f"\n• {escape(section['content'], quote=False)}")
        elif section["section_type"] == "paragraph":
            preview = section["content"][:80]
            if len(section["content"]) > 80:
                preview += "…"
            parts.append(f"  {escape(preview, quote=False)}")
            break

    text = "\n".join(parts)

    keyboard = [
        [
            InlineKeyboardButton("✏ Edit", callback_data=f"edit:{doc_id}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"del_confirm:{doc_id}"),
        ],
        [
            InlineKeyboardButton("📄 Export PDF", callback_data=f"export_doc:{doc_id}"),
            InlineKeyboardButton("⬅ Back", callback_data=f"browse:{doc['category']}"),
        ],
    ]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


@restricted
async def edit_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enter doc mode to add sections to an existing document."""
    query = update.callback_query
    await query.answer()

    doc_id = int(query.data.split(":", 1)[1])
    doc = _get_owned_document(doc_id, query.from_user.id)
    if not doc:
        await query.edit_message_text("Document not found.")
        return

    context.user_data["doc_mode"] = True
    context.user_data["current_category"] = doc["category"]
    context.user_data["last_category"] = doc["category"]
    context.user_data["current_doc_id"] = doc_id
    context.user_data["current_doc_title"] = doc["title"]
    context.user_data["current_section_id"] = None
    context.user_data["current_doc_is_new"] = False

    await query.edit_message_text(
        f"✏ Editing: <b>{escape(doc['title'], quote=False)}</b>\n\n"
        "Add more content with the three symbols:\n"
        "<b>#</b> Subtitle / section heading\n"
        "<b>*</b> Paragraph / body text\n\n"
        f"Example:\n{_pre('#Section\n*Paragraph text')}\n\n"
        "A new <b>@Title</b> block starts a separate document.\n"
        "Attach a photo, send /done to save, or /cancel to abort.",
        parse_mode="HTML",
    )


@restricted
async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation before deleting a document."""
    query = update.callback_query
    await query.answer()

    doc_id = int(query.data.split(":", 1)[1])
    doc = _get_owned_document(doc_id, query.from_user.id)
    if not doc:
        await query.edit_message_text("Document not found.")
        return

    context.user_data["pending_delete_id"] = doc_id

    preview = doc["title"][:60]
    section_count = len(doc["sections"])
    photo_count = len(doc.get("standalone_photos", [])) + sum(
        len(s.get("photos", [])) for s in doc["sections"]
    )

    await query.edit_message_text(
        f"🗑 <b>Delete document?</b>\n\n"
        f"Document: <b>{escape(preview, quote=False)}</b>\n"
        f"Category: <b>{escape(doc['category'], quote=False)}</b>\n"
        f"Sections: {section_count} | Photos: {photo_count}\n\n"
        "This cannot be undone.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Yes, delete", callback_data="del_do:"),
                    InlineKeyboardButton("❌ Cancel", callback_data="del_abort:"),
                ]
            ]
        ),
        parse_mode="HTML",
    )


@restricted
async def delete_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Perform the deletion after confirmation."""
    query = update.callback_query
    await query.answer()

    doc_id = context.user_data.pop("pending_delete_id", None)
    if not doc_id:
        await query.edit_message_text("No deletion pending.")
        return

    success = delete_document(doc_id)
    if success:
        await query.edit_message_text(f"✅ Document #{doc_id} deleted permanently.")
    else:
        await query.edit_message_text("❌ Could not delete document.")


@restricted
async def delete_abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abort deletion."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_delete_id", None)
    await query.edit_message_text("Deletion cancelled.")


@restricted
async def library_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to the category list."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    counts = {c["category"]: c["count"] for c in get_categories_with_counts(user_id)}

    left = CATEGORIES[:4]
    right = CATEGORIES[4:]
    rows = []
    for i in range(4):
        row = []
        if i < len(left):
            cat = left[i]
            row.append(
                InlineKeyboardButton(
                    f"{cat} ({counts.get(cat, 0)})", callback_data=f"browse:{cat}"
                )
            )
        if i < len(right):
            cat = right[i]
            row.append(
                InlineKeyboardButton(
                    f"{cat} ({counts.get(cat, 0)})", callback_data=f"browse:{cat}"
                )
            )
        rows.append(row)

    await query.edit_message_text(
        "📚 <b>Personal Knowledge Library</b>\nChoose a category to browse:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )


# Handlers to register in main.py
note_handlers = [
    CommandHandler("new", new_command),
    CommandHandler("library", library_command),
    CommandHandler("done", done_command),
    CommandHandler("cancel", cancel_command),
    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_message),
    MessageHandler(filters.PHOTO, receive_photo),
    CallbackQueryHandler(category_selected, pattern=r"^cat:"),
    CallbackQueryHandler(browse_category, pattern=r"^browse:"),
    CallbackQueryHandler(view_document, pattern=r"^view:"),
    CallbackQueryHandler(edit_document, pattern=r"^edit:"),
    CallbackQueryHandler(delete_confirm, pattern=r"^del_confirm:"),
    CallbackQueryHandler(delete_do, pattern=r"^del_do:"),
    CallbackQueryHandler(delete_abort, pattern=r"^del_abort:"),
    CallbackQueryHandler(library_back, pattern=r"^lib:back$"),
]
