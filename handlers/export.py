"""PDF export handlers.

Part 5: /export -> choose a document -> generate clean PDF with
user-defined title, subtitles, paragraphs, and inline photos.
"""

import asyncio
import logging
import os
from html import escape

from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
)

from config import EXPORTS_DIR
from database import get_all_documents, get_document_by_id
from pdf_generator import generate_document_pdf
from security import restricted

logger = logging.getLogger(__name__)

EXPORT_DIR = EXPORTS_DIR


@restricted
async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export: list all documents to choose one for PDF export."""
    user_id = update.effective_user.id
    docs = get_all_documents(user_id)

    if not docs:
        await update.message.reply_text(
            "No documents found yet. Start one with an @Title block, then add "
            "#Subtitle and *Paragraph blocks."
        )
        return

    buttons = []
    for doc in docs:
        label = f"{doc['title']} [{doc['category']}]"[:60]
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"export_doc:{doc['id']}")]
        )

    await update.message.reply_text(
        "📄 Choose a document to export as PDF:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@restricted
async def export_document_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send the PDF for the selected document."""
    query = update.callback_query
    await query.answer()

    doc_id = int(query.data.split(":", 1)[1])
    doc = get_document_by_id(doc_id)
    if not doc or doc.get("user_id") != query.from_user.id:
        await query.edit_message_text("Document not found.")
        return

    os.makedirs(EXPORT_DIR, exist_ok=True)
    safe_name = "".join(
        c for c in doc["title"] if c.isalnum() or c in (" ", "_", "-")
    ).strip()[:80]
    if not safe_name:
        safe_name = f"document-{doc_id}"
    output_path = os.path.join(EXPORT_DIR, f"{safe_name}-{doc_id}.pdf")

    try:
        # ReportLab is synchronous; keep the bot's event loop responsive.
        await asyncio.to_thread(generate_document_pdf, doc, output_path)
    except Exception:
        logger.exception("PDF generation failed for document %s", doc_id)
        await query.edit_message_text(
            "❌ Could not generate the PDF. Please try again."
        )
        return

    chat_id = query.message.chat_id if query.message else query.from_user.id
    with open(output_path, "rb") as pdf_file:
        await context.bot.send_document(
            chat_id=chat_id,
            document=InputFile(pdf_file, filename=os.path.basename(output_path)),
            caption=f"📄 {doc['title']}"[:1000],
        )

    await query.edit_message_text(
        f"✅ PDF exported: <b>{escape(doc['title'], quote=False)}</b>",
        parse_mode="HTML",
    )


# Handlers to register in main.py
export_handlers = [
    CommandHandler("export", export_command),
    CallbackQueryHandler(export_document_cb, pattern=r"^export_doc:"),
]
