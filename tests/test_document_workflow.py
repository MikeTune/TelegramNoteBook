"""End-to-end tests for the Telegram document workflow.

Updates are built from real Telegram update JSON and dispatched through a
python-telegram-bot ``Application`` with the same handler registration order as
``main.py``.  A recording bot captures outgoing API calls, so routing, state,
database writes, photo handling and PDF export are all exercised without
network access.
"""

import asyncio
import os
import tempfile
import time
import traceback
import unittest
from datetime import datetime, timedelta

from PIL import Image
from pypdf import PdfReader
from telegram import Bot, Update, User
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler

import database
import handlers.export as export_module
import handlers.notes as notes_module
import handlers.reminders as reminders_module
import security
from handlers.export import export_handlers
from handlers.notes import note_handlers
from handlers.reminders import (
    TZ,
    reload_reminders,
    reminder_handlers,
    scheduler,
    start_scheduler,
    stop_scheduler,
)
from main import on_error, start as start_command

USER_ID = 424242
BOT_ID = 987654

OUTGOING: list[dict] = []
HANDLER_ERRORS: list[BaseException] = []
PHOTO_SOURCE = {"path": None}


def _record(method: str, args, kwargs):
    OUTGOING.append({"method": method, "args": args, "kwargs": kwargs})
    return None


class FakeFile:
    """Stand-in for telegram.File that copies a local JPEG."""

    def __init__(self, source_path: str, file_id: str):
        self.source_path = source_path
        self.file_id = file_id

    async def download_to_drive(self, custom_path: str):
        with open(self.source_path, "rb") as source:
            data = source.read()
        with open(custom_path, "wb") as destination:
            destination.write(data)
        return custom_path


class RecordingBot(Bot):
    """Bot that records API calls instead of talking to Telegram."""

    async def initialize(self):
        self._requests_initialized = True
        self._bot_initialized = True
        self._bot_user = User(
            id=BOT_ID,
            first_name="TestBot",
            is_bot=True,
            username="test_bot",
        )

    async def shutdown(self):
        pass

    async def send_message(self, *args, **kwargs):
        return _record("send_message", args, kwargs)

    async def edit_message_text(self, *args, **kwargs):
        return _record("edit_message_text", args, kwargs)

    async def send_document(self, *args, **kwargs):
        return _record("send_document", args, kwargs)

    async def answer_callback_query(self, *args, **kwargs):
        return _record("answer_callback_query", args, kwargs)

    async def get_file(self, file_id, *args, **kwargs):
        return FakeFile(PHOTO_SOURCE["path"], file_id)


class DocumentWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 5.0
        OUTGOING.clear()
        HANDLER_ERRORS.clear()
        PHOTO_SOURCE["path"] = None

        self._saved_allowed = security.ALLOWED_USER_IDS
        security.ALLOWED_USER_IDS = frozenset({USER_ID})
        self.addCleanup(self._restore_security)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self._saved_paths = {
            "db": database.DB_PATH,
            "db_photos": database.PHOTOS_DIR,
            "notes_photos": notes_module.PHOTOS_DIR,
            "exports": export_module.EXPORT_DIR,
        }
        self.addCleanup(self._restore_paths)

        photos_dir = os.path.join(self.tmp.name, "photos")
        database.DB_PATH = os.path.join(self.tmp.name, "knowledge_library.db")
        database.PHOTOS_DIR = photos_dir
        notes_module.PHOTOS_DIR = photos_dir
        export_module.EXPORT_DIR = os.path.join(self.tmp.name, "exports")
        database.init_db()

        self.bot = RecordingBot(token="987654:TESTTOKEN")
        self.app = Application.builder().bot(self.bot).build()
        self.app.add_handler(CommandHandler("start", start_command))
        for handler in (*note_handlers, *reminder_handlers, *export_handlers):
            self.app.add_handler(handler)
        self.app.add_error_handler(self._on_error)
        await self.app.initialize()
        self.addAsyncCleanup(self._shutdown)

        self._update_id = 0
        self._message_id = 100

    async def _shutdown(self):
        await self.app.shutdown()

    def _restore_security(self):
        security.ALLOWED_USER_IDS = self._saved_allowed

    def _restore_paths(self):
        database.DB_PATH = self._saved_paths["db"]
        database.PHOTOS_DIR = self._saved_paths["db_photos"]
        notes_module.PHOTOS_DIR = self._saved_paths["notes_photos"]
        export_module.EXPORT_DIR = self._saved_paths["exports"]

    async def _on_error(self, update, context):
        HANDLER_ERRORS.append(context.error)

    def _assert_no_errors(self):
        if HANDLER_ERRORS:
            error = HANDLER_ERRORS[0]
            raise AssertionError(
                "Handler raised an exception:\n"
                + "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
            )

    def _next_ids(self):
        self._update_id += 1
        self._message_id += 1
        return self._update_id, self._message_id

    async def send(self, text: str, user_id: int = USER_ID):
        update_id, message_id = self._next_ids()
        message = {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": {"id": user_id, "type": "private"},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Mike",
                "username": "mike",
            },
            "text": text,
        }
        if text.startswith("/"):
            command = text.split(maxsplit=1)[0]
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(command)}
            ]
        update = Update.de_json(
            {"update_id": update_id, "message": message}, self.app.bot
        )
        await self.app.process_update(update)
        self._assert_no_errors()

    async def click(
        self,
        data: str,
        message_text: str = "Choose a category",
        user_id: int = USER_ID,
    ):
        update_id, message_id = self._next_ids()
        update = Update.de_json(
            {
                "update_id": update_id,
                "callback_query": {
                    "id": f"callback-{update_id}",
                    "from": {
                        "id": user_id,
                        "is_bot": False,
                        "first_name": "Mike",
                        "username": "mike",
                    },
                    "chat_instance": "test-chat-instance",
                    "data": data,
                    "message": {
                        "message_id": message_id,
                        "date": int(time.time()),
                        "chat": {"id": user_id, "type": "private"},
                        "from": {
                            "id": BOT_ID,
                            "is_bot": True,
                            "first_name": "TestBot",
                            "username": "test_bot",
                        },
                        "text": message_text,
                    },
                },
            },
            self.app.bot,
        )
        await self.app.process_update(update)
        self._assert_no_errors()

    async def send_photo(self, file_id: str = "large"):
        update_id, message_id = self._next_ids()
        update = Update.de_json(
            {
                "update_id": update_id,
                "message": {
                    "message_id": message_id,
                    "date": int(time.time()),
                    "chat": {"id": USER_ID, "type": "private"},
                    "from": {
                        "id": USER_ID,
                        "is_bot": False,
                        "first_name": "Mike",
                        "username": "mike",
                    },
                    "photo": [
                        {
                            "file_id": "small",
                            "file_unique_id": "small-unique",
                            "width": 90,
                            "height": 60,
                        },
                        {
                            "file_id": file_id,
                            "file_unique_id": "large-unique",
                            "width": 240,
                            "height": 160,
                        },
                    ],
                },
            },
            self.app.bot,
        )
        await self.app.process_update(update)
        self._assert_no_errors()

    def last(self, method: str):
        for call in reversed(OUTGOING):
            if call["method"] == method:
                return call
        return None

    def last_text(self, method: str = "send_message") -> str:
        call = self.last(method)
        self.assertIsNotNone(call, f"No {method} call was recorded")
        return call["kwargs"].get("text", "")

    def callbacks(self, call) -> list[str]:
        markup = call["kwargs"].get("reply_markup")
        if not markup:
            return []
        return [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]

    def documents(self):
        return database.get_all_documents(USER_ID)

    def document(self, doc_id: int):
        return database.get_document_by_id(doc_id)

    async def start_document(self, text: str, category: str = "Tech"):
        await self.send(text)
        prompt = self.last("send_message")
        self.assertIn("Choose a category", prompt["kwargs"]["text"])
        self.assertIn(f"cat:{category}", self.callbacks(prompt))
        self.assertEqual(self.documents(), [])

        await self.click(f"cat:{category}")
        docs = self.documents()
        self.assertEqual(len(docs), 1)
        return self.document(docs[0]["id"])

    def export_pdf_path(self) -> str:
        files = [
            os.path.join(export_module.EXPORT_DIR, name)
            for name in os.listdir(export_module.EXPORT_DIR)
            if name.lower().endswith(".pdf")
        ]
        self.assertEqual(len(files), 1, f"Expected one PDF, found {files}")
        return files[0]

    def pdf_text(self, path: str | None = None) -> str:
        reader = PdfReader(path or self.export_pdf_path())
        return "\n".join((page.extract_text() or "") for page in reader.pages)

    def pdf_images(self, path: str | None = None):
        reader = PdfReader(path or self.export_pdf_path())
        return [image for page in reader.pages for image in page.images]

    def pdf_spans(self, path: str | None = None):
        """Return ``(text, font_size)`` spans in PDF painting order."""
        reader = PdfReader(path or self.export_pdf_path())
        spans = []

        def visitor(text, cm, tm, font_dict, font_size):
            if text and text.strip():
                spans.append((text.strip(), font_size))

        for page in reader.pages:
            page.extract_text(visitor_text=visitor)
        return spans

    # ------------------------------------------------------------------
    # Welcome / instructions
    # ------------------------------------------------------------------

    async def test_start_welcome_documents_symbol_format_only(self):
        await self.send("/start")
        text = self.last_text()
        for expected in (
            "@My Document",
            "#Section",
            "*Paragraph text",
            "/new",
        ):
            self.assertIn(expected, text)
        for obsolete in ("Title:", "Subtitle:", "Paragraph:"):
            self.assertNotIn(obsolete, text)

    # ------------------------------------------------------------------
    # Core parsing / creation
    # ------------------------------------------------------------------

    async def test_compact_message_creates_structured_document(self):
        await self.send(
            "@Windows #Uninstall stubborn apps *Qwen3.5-9B-Abliterated"
        )
        prompt = self.last("send_message")
        self.assertEqual(prompt["kwargs"]["text"], "📂 Choose a category for your new document:")
        self.assertIn("cat:Tech", self.callbacks(prompt))
        self.assertEqual(self.documents(), [])

        await self.click("cat:Tech")
        confirmation = self.last_text("edit_message_text")
        self.assertIn("Document created", confirmation)
        self.assertIn("Windows", confirmation)
        for obsolete in ("Title:", "Subtitle:", "Paragraph:"):
            self.assertNotIn(obsolete, confirmation)

        docs = self.documents()
        self.assertEqual(len(docs), 1)
        doc = self.document(docs[0]["id"])
        self.assertEqual(doc["title"], "Windows")
        self.assertEqual(doc["category"], "Tech")
        self.assertEqual(
            [(s["section_type"], s["content"]) for s in doc["sections"]],
            [
                ("title", "Windows"),
                ("subtitle", "Uninstall stubborn apps"),
                ("paragraph", "Qwen3.5-9B-Abliterated"),
            ],
        )

    async def test_multiline_document_with_multiple_sections(self):
        text = (
            "@Computer Architecture\n"
            "\n"
            "#CPU\n"
            "\n"
            "*The CPU executes instructions and processes data.\n"
            "\n"
            "#RAM\n"
            "\n"
            "*RAM temporarily stores information currently being used by the computer."
        )
        doc = await self.start_document(text)
        self.assertEqual(doc["title"], "Computer Architecture")
        self.assertEqual(
            [(s["section_type"], s["content"]) for s in doc["sections"]],
            [
                ("title", "Computer Architecture"),
                ("subtitle", "CPU"),
                ("paragraph", "The CPU executes instructions and processes data."),
                ("subtitle", "RAM"),
                (
                    "paragraph",
                    "RAM temporarily stores information currently being used by the computer.",
                ),
            ],
        )

    async def test_multiple_paragraphs_and_special_characters(self):
        doc = await self.start_document(
            "@Uninstall\n#Edge\n*irm https://christitus.com/win | iex\n"
            "*Path: C:\\Users\\Mike\\docs\n*Use 2 * 3 = 6 & C# is fine"
        )
        paragraphs = [
            s["content"] for s in doc["sections"] if s["section_type"] == "paragraph"
        ]
        self.assertEqual(
            paragraphs,
            [
                "irm https://christitus.com/win | iex",
                "Path: C:\\Users\\Mike\\docs",
                "Use 2 * 3 = 6 & C# is fine",
            ],
        )

    async def test_multiple_paragraphs_under_one_subtitle_across_messages(self):
        doc = await self.start_document("@My Doc #Section")
        await self.send("*First para.")
        await self.send("*Second para.")
        await self.send("*Third para.")
        refreshed = self.document(doc["id"])
        self.assertEqual(
            [(s["section_type"], s["content"]) for s in refreshed["sections"]],
            [
                ("title", "My Doc"),
                ("subtitle", "Section"),
                ("paragraph", "First para."),
                ("paragraph", "Second para."),
                ("paragraph", "Third para."),
            ],
        )

    async def test_separate_messages_extend_the_same_document(self):
        doc = await self.start_document("@My Doc #Intro *First paragraph.")
        await self.send("#Section Two")
        await self.send("*Second paragraph.")
        await self.send("*Third paragraph.")

        docs = self.documents()
        self.assertEqual(len(docs), 1)
        refreshed = self.document(doc["id"])
        self.assertEqual(
            [(s["section_type"], s["content"]) for s in refreshed["sections"]],
            [
                ("title", "My Doc"),
                ("subtitle", "Intro"),
                ("paragraph", "First paragraph."),
                ("subtitle", "Section Two"),
                ("paragraph", "Second paragraph."),
                ("paragraph", "Third paragraph."),
            ],
        )
        for _ in range(3):
            self.assertIn("Added to", self.last_text())

    async def test_new_title_starts_a_second_document(self):
        first = await self.start_document("@First #A *one")
        await self.send("@Second #B *two")

        docs = self.documents()
        self.assertEqual(len(docs), 2)
        titles = {doc["title"] for doc in docs}
        self.assertEqual(titles, {"First", "Second"})
        self.assertEqual(
            len(self.document(first["id"])["sections"]), 3
        )
        second = next(d for d in docs if d["title"] == "Second")
        self.assertEqual(
            [(s["section_type"], s["content"])
             for s in self.document(second["id"])["sections"]],
            [
                ("title", "Second"),
                ("subtitle", "B"),
                ("paragraph", "two"),
            ],
        )

    async def test_many_documents_in_one_message_stay_within_limit(self):
        text = "\n".join(f"@Doc {index}" for index in range(12))
        await self.send(text)
        await self.click("cat:Tech")

        confirmation = self.last_text("edit_message_text")
        self.assertLess(len(confirmation), 4096)
        self.assertIn("more document", confirmation)
        self.assertEqual(len(self.documents()), 12)

    async def test_messages_before_category_selection_are_buffered(self):
        await self.send("@Split Doc")
        await self.send("#Section")
        await self.send("*Body text")
        self.assertEqual(self.documents(), [])

        await self.click("cat:Ideas")
        docs = self.documents()
        self.assertEqual(len(docs), 1)
        doc = self.document(docs[0]["id"])
        self.assertEqual(doc["category"], "Ideas")
        self.assertEqual(
            [(s["section_type"], s["content"]) for s in doc["sections"]],
            [
                ("title", "Split Doc"),
                ("subtitle", "Section"),
                ("paragraph", "Body text"),
            ],
        )

    # ------------------------------------------------------------------
    # Old input system must be gone
    # ------------------------------------------------------------------

    async def test_old_text_prefix_format_is_rejected(self):
        await self.send(
            "Title: My Doc\nSubtitle: Section\nParagraph: Body text"
        )
        self.assertEqual(self.documents(), [])
        text = self.last_text()
        self.assertIn("Use the three document symbols", text)
        self.assertNotIn("Choose a category", text)
        self.assertNotIn("Title:", text)
        self.assertNotIn("Subtitle:", text)

    async def test_plain_text_inside_document_mode_adds_nothing(self):
        doc = await self.start_document("@Doc #Section *Body")
        before = len(self.document(doc["id"])["sections"])

        await self.send("just some plain text")
        after = len(self.document(doc["id"])["sections"])
        self.assertEqual(before, after)
        self.assertIn("No @ / # / * blocks", self.last_text())

    # ------------------------------------------------------------------
    # Category selection stays separate
    # ------------------------------------------------------------------

    async def test_category_first_then_immediate_document(self):
        await self.send("/new")
        self.assertIn("Choose a category", self.last_text())
        await self.click("cat:DEV")
        selected = self.last_text("edit_message_text")
        self.assertIn("Category", selected)
        self.assertIn("@My Document", selected)
        for obsolete in ("Title:", "Subtitle:", "Paragraph:"):
            self.assertNotIn(obsolete, selected)

        await self.send("@Immediate\n#Section\n*Body")
        docs = self.documents()
        self.assertEqual(len(docs), 1)
        doc = self.document(docs[0]["id"])
        self.assertEqual(doc["title"], "Immediate")
        self.assertEqual(doc["category"], "DEV")

    async def test_category_callback_does_not_create_document_by_itself(self):
        await self.click("cat:Finance")
        self.assertEqual(self.documents(), [])
        self.assertIn("Category", self.last_text("edit_message_text"))

    # ------------------------------------------------------------------
    # /done and /cancel
    # ------------------------------------------------------------------

    async def test_done_finalizes_document(self):
        doc = await self.start_document("@Done Doc #S *P")
        await self.send("/done")
        saved = self.last_text()
        self.assertIn("Document saved", saved)
        self.assertIn("Done Doc", saved)
        self.assertIn("Sections: 1 subtitle(s), 1 paragraph(s)", saved)

        self.assertFalse(self.app.user_data[USER_ID].get("doc_mode"))
        self.assertIsNotNone(self.document(doc["id"]))

        await self.send("@Another #N *P")
        self.assertEqual(len(self.documents()), 1)
        self.assertIn("Choose a category", self.last_text())

    async def test_cancel_deletes_open_document(self):
        doc = await self.start_document("@Temp Doc #S *P")
        await self.send("/cancel")
        self.assertIn("cancelled", self.last_text())
        self.assertIsNone(self.document(doc["id"]))
        self.assertEqual(self.documents(), [])

    async def test_cancel_while_editing_keeps_existing_document(self):
        doc = await self.start_document("@Saved Doc #Original *Body")
        await self.send("/done")

        await self.click(f"edit:{doc['id']}")
        self.assertIn("Editing", self.last_text("edit_message_text"))
        await self.send("#Added Section")
        await self.send("/cancel")

        self.assertIn("remains saved", self.last_text())
        refreshed = self.document(doc["id"])
        self.assertIsNotNone(refreshed)
        self.assertEqual(
            [(s["section_type"], s["content"]) for s in refreshed["sections"]],
            [
                ("title", "Saved Doc"),
                ("subtitle", "Original"),
                ("paragraph", "Body"),
                ("subtitle", "Added Section"),
            ],
        )

    # ------------------------------------------------------------------
    # Library browsing and deletion
    # ------------------------------------------------------------------

    async def test_library_browse_view_and_delete(self):
        doc = await self.start_document("@Library Doc #Heading *Body")
        await self.send("/library")
        library = self.last("send_message")
        self.assertIn("browse:Tech", self.callbacks(library))

        await self.click("browse:Tech")
        listed_call = self.last("edit_message_text")
        self.assertIn("Tech", listed_call["kwargs"]["text"])
        self.assertIn(f"view:{doc['id']}", self.callbacks(listed_call))

        await self.click(f"view:{doc['id']}")
        viewed = self.last_text("edit_message_text")
        self.assertIn("Library Doc", viewed)
        self.assertIn("Heading", viewed)

        await self.click(f"del_confirm:{doc['id']}")
        self.assertIn("Delete document?", self.last_text("edit_message_text"))
        await self.click("del_do:")
        self.assertIsNone(self.document(doc["id"]))
        self.assertEqual(self.documents(), [])

    # ------------------------------------------------------------------
    # Photos and PDF export
    # ------------------------------------------------------------------

    async def test_photo_attachment_and_pdf_export(self):
        src = os.path.join(self.tmp.name, "source.jpg")
        Image.new("RGB", (240, 160), (40, 120, 200)).save(src, "JPEG")
        PHOTO_SOURCE["path"] = src

        doc = await self.start_document("@Photo Doc #Section *Body text")
        await self.send_photo()
        self.assertIn("Photo saved", self.last_text())

        refreshed = self.document(doc["id"])
        photos = refreshed["standalone_photos"] + [
            photo for section in refreshed["sections"] for photo in section["photos"]
        ]
        self.assertEqual(len(photos), 1)
        self.assertTrue(os.path.exists(photos[0]["file_path"]))

        await self.click(f"export_doc:{doc['id']}")
        sent = self.last("send_document")
        self.assertIsNotNone(sent, "No PDF document was sent")
        self.assertIn("Photo Doc", sent["kwargs"]["caption"])

        pdf_path = self.export_pdf_path()
        reader = PdfReader(pdf_path)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        self.assertIn("Photo Doc", text)
        self.assertIn("Section", text)
        self.assertIn("Body text", text)
        images = [image for page in reader.pages for image in page.images]
        self.assertTrue(images, "The exported PDF does not contain the photo")

        self.assertIn(
            "PDF exported", self.last_text("edit_message_text")
        )

    async def test_pdf_preserves_special_characters_and_line_breaks(self):
        doc = await self.start_document(
            "@Symbols & <Notes> #Section *Line one\nLine two & <tag>"
        )
        await self.click(f"export_doc:{doc['id']}")

        reader = PdfReader(self.export_pdf_path())
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        self.assertIn("Symbols & <Notes>", text)
        self.assertIn("Line one", text)
        self.assertIn("Line two & <tag>", text)

    async def test_exact_message_pdf_contains_structure_not_markers(self):
        doc = await self.start_document(
            "@Windows #Uninstall stubborn apps *Qwen3.5-9B-Abliterated"
        )
        await self.click(f"export_doc:{doc['id']}")

        text = self.pdf_text()
        self.assertIn("Windows", text)
        self.assertIn("Uninstall stubborn apps", text)
        self.assertIn("Qwen3.5-9B-Abliterated", text)

        self.assertLess(text.index("Windows"), text.index("Uninstall stubborn apps"))
        self.assertLess(
            text.index("Uninstall stubborn apps"),
            text.index("Qwen3.5-9B-Abliterated"),
        )
        for marker in ("@", "#", "*"):
            self.assertNotIn(marker, text)

        # Title / subtitle / paragraph must use distinct visual hierarchy.
        sizes = {}
        for span_text, font_size in self.pdf_spans():
            for label in (
                "Windows",
                "Uninstall stubborn apps",
                "Qwen3.5-9B-Abliterated",
            ):
                if label in span_text:
                    sizes.setdefault(label, font_size)
        self.assertGreater(sizes["Windows"], sizes["Uninstall stubborn apps"])
        self.assertGreater(
            sizes["Uninstall stubborn apps"], sizes["Qwen3.5-9B-Abliterated"]
        )

    async def test_pdf_accepts_markers_followed_by_spaces(self):
        doc = await self.start_document(
            "@Windows # Uninstall stubborn apps * Qwen3.5-9B-Abliterated"
        )
        self.assertEqual(
            [(s["section_type"], s["content"]) for s in doc["sections"]],
            [
                ("title", "Windows"),
                ("subtitle", "Uninstall stubborn apps"),
                ("paragraph", "Qwen3.5-9B-Abliterated"),
            ],
        )
        await self.click(f"export_doc:{doc['id']}")

        text = self.pdf_text()
        self.assertIn("Windows", text)
        self.assertIn("Uninstall stubborn apps", text)
        self.assertIn("Qwen3.5-9B-Abliterated", text)
        for marker in ("@", "#", "*"):
            self.assertNotIn(marker, text)

    async def test_long_document_pdf_keeps_section_order_and_photos(self):
        src = os.path.join(self.tmp.name, "section-photo.jpg")
        Image.new("RGB", (320, 180), (30, 140, 90)).save(src, "JPEG")
        PHOTO_SOURCE["path"] = src

        doc = await self.start_document(
            "@Computer Architecture #CPU *The CPU executes instructions and processes data."
        )
        await self.send_photo("cpu-photo")
        await self.send("#RAM")
        await self.send(
            "*RAM temporarily stores information currently being used by the computer."
        )
        await self.send_photo("ram-photo")

        refreshed = self.document(doc["id"])
        sections = refreshed["sections"]
        self.assertEqual(
            [s["section_type"] for s in sections],
            ["title", "subtitle", "paragraph", "subtitle", "paragraph"],
        )
        self.assertEqual(len(sections[2]["photos"]), 1)
        self.assertEqual(len(sections[4]["photos"]), 1)
        self.assertEqual(sections[2]["photos"][0]["file_id"], "cpu-photo")
        self.assertEqual(sections[4]["photos"][0]["file_id"], "ram-photo")

        await self.click(f"export_doc:{doc['id']}")
        text = self.pdf_text()
        ordered = [
            "Computer Architecture",
            "CPU",
            "The CPU executes instructions and processes data.",
            "RAM",
            "RAM temporarily stores information currently being used by the computer.",
        ]
        positions = [text.index(value) for value in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(self.pdf_images()), 2)
        for marker in ("@", "#", "*"):
            self.assertNotIn(marker, text)

    async def test_pdf_apostrophe_line_breaks_stay_in_one_paragraph(self):
        doc = await self.start_document(
            "@Line Breaks #Paragraph "
            "*The CPU processes instructions'It performs calculations"
            "'It controls other components."
        )
        paragraph = next(
            s for s in doc["sections"] if s["section_type"] == "paragraph"
        )
        self.assertEqual(
            paragraph["content"],
            "The CPU processes instructions\n"
            "It performs calculations\n"
            "It controls other components.",
        )

        await self.click(f"export_doc:{doc['id']}")
        text = self.pdf_text()
        self.assertNotIn("'", text)
        for line in (
            "The CPU processes instructions",
            "It performs calculations",
            "It controls other components.",
        ):
            self.assertIn(line, text)
        self.assertIn(
            "The CPU processes instructions\nIt performs calculations", text
        )

    async def test_legacy_raw_documents_are_repaired_before_export(self):
        now = datetime.now().isoformat()
        raw_title = "@Windows #Uninstall stubborn apps * Qwen3.5-9B-Abliterated"
        conn = database.get_connection()

        cursor = conn.execute(
            "INSERT INTO documents (user_id, category, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (USER_ID, "Tech", raw_title, now, now),
        )
        raw_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO document_sections "
            "(document_id, section_type, content, order_index) VALUES (?, 'title', ?, 0)",
            (raw_id, raw_title),
        )

        cursor = conn.execute(
            "INSERT INTO documents (user_id, category, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (USER_ID, "Tech", "Windows", now, now),
        )
        merged_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO document_sections "
            "(document_id, section_type, content, order_index) VALUES (?, 'title', ?, 0)",
            (merged_id, "Windows"),
        )
        conn.execute(
            "INSERT INTO document_sections "
            "(document_id, section_type, content, order_index) VALUES (?, 'subtitle', ?, 1)",
            (merged_id, "Uninstall stubborn apps * Qwen3.5-9B-Abliterated"),
        )
        conn.commit()
        conn.close()

        database.init_db()

        for document_id in (raw_id, merged_id):
            repaired = self.document(document_id)
            self.assertEqual(repaired["title"], "Windows")
            self.assertEqual(
                [(s["section_type"], s["content"]) for s in repaired["sections"]],
                [
                    ("title", "Windows"),
                    ("subtitle", "Uninstall stubborn apps"),
                    ("paragraph", "Qwen3.5-9B-Abliterated"),
                ],
            )

        # The repair is idempotent: a second startup changes nothing.
        database.init_db()
        again = self.document(raw_id)
        self.assertEqual(
            [(s["section_type"], s["content"]) for s in again["sections"]],
            [
                ("title", "Windows"),
                ("subtitle", "Uninstall stubborn apps"),
                ("paragraph", "Qwen3.5-9B-Abliterated"),
            ],
        )

        await self.click(f"export_doc:{raw_id}")
        text = self.pdf_text()
        self.assertIn("Windows", text)
        self.assertIn("Uninstall stubborn apps", text)
        self.assertIn("Qwen3.5-9B-Abliterated", text)
        for marker in ("@", "#", "*"):
            self.assertNotIn(marker, text)

    # ------------------------------------------------------------------
    # Private-bot access control
    # ------------------------------------------------------------------

    async def test_unauthorized_user_is_blocked(self):
        await self.send("/start", user_id=999999)
        self.assertEqual(self.last_text(), "This bot is private.")

        await self.click("cat:Tech", user_id=999999)
        answer = self.last("answer_callback_query")
        self.assertIsNotNone(answer)
        self.assertTrue(answer["kwargs"]["show_alert"])
        self.assertIn("private", answer["kwargs"]["text"].lower())
        self.assertEqual(self.documents(), [])

        # The owner is still able to use the bot.
        await self.send("/start")
        self.assertIn("Personal Knowledge Library", self.last_text())

    async def test_conflict_error_stops_polling(self):
        class FakeApplication:
            stopped = False

            def stop_running(self):
                self.stopped = True

        class FakeContext:
            def __init__(self):
                self.error = Conflict("terminated by other getUpdates request")
                self.application = FakeApplication()

        context = FakeContext()
        await on_error(None, context)
        self.assertTrue(context.application.stopped)

    # ------------------------------------------------------------------
    # Reminders must keep working
    # ------------------------------------------------------------------

    async def test_reminder_flow_persists_and_reschedules(self):
        start_scheduler()
        self.addCleanup(self._stop_scheduler)

        self.assertEqual(str(scheduler.timezone), "Africa/Addis_Ababa")

        await self.send("/remind")
        self.assertIn("Choose a date", self.last_text())

        tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        await self.click(f"remind_date:{tomorrow}")
        await self.click("remind_time:09:00")
        await self.send("Buy milk")

        reply = self.last_text()
        self.assertIn("Reminder set", reply)
        self.assertIn("Buy milk", reply)
        self.assertIn("Africa/Addis_Ababa", reply)

        pending = database.get_pending_reminders()
        self.assertEqual(len(pending), 1)
        reminder = pending[0]
        self.assertEqual(reminder["user_id"], USER_ID)
        self.assertEqual(reminder["chat_id"], USER_ID)
        run_at = datetime.fromisoformat(reminder["run_at"])
        self.assertIsNotNone(run_at.tzinfo)
        self.assertIn(
            f"reminder_{reminder['id']}",
            [job.id for job in scheduler.get_jobs()],
        )

        # Simulate the in-memory scheduler being empty after a process restart:
        # the pending row in SQLite must be restored.
        scheduler.remove_all_jobs()
        reload_reminders()
        self.assertIn(
            f"reminder_{reminder['id']}",
            [job.id for job in scheduler.get_jobs()],
        )

    async def test_due_reminder_delivers_and_marks_sent(self):
        reminder_id = database.add_reminder(
            USER_ID,
            USER_ID,
            "Stand up and stretch",
            (datetime.now(TZ) - timedelta(minutes=1)).isoformat(),
        )
        reminders_module._bot = self.bot
        reminders_module._loop = asyncio.get_running_loop()
        self.addCleanup(self._clear_reminder_runtime)

        await asyncio.to_thread(reminders_module._fire_reminder, reminder_id)

        sent = self.last("send_message")
        self.assertIsNotNone(sent)
        self.assertIn("Stand up and stretch", sent["kwargs"]["text"])
        self.assertEqual(database.get_reminder(reminder_id)["sent"], 1)

    def _clear_reminder_runtime(self):
        reminders_module._bot = None
        reminders_module._loop = None

    def _stop_scheduler(self):
        for job in scheduler.get_jobs():
            job.remove()
        stop_scheduler()


if __name__ == "__main__":
    unittest.main()
