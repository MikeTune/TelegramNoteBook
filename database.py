"""Database layer for Personal Knowledge Library (SQLite).

Stores user-defined documents with Title / Subtitle / Paragraph structure
and optional photo attachments. Legacy notes are migrated on startup.
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta

from config import DB_PATH, PHOTOS_DIR
from document_parser import (
    PARAGRAPH,
    SUBTITLE,
    TITLE,
    expand_paragraph_line_breaks,
    parse_document_blocks,
)

logger = logging.getLogger(__name__)

NEW_CATEGORIES = [
    "Tech",
    "DEV",
    "Hardware",
    "Logistics",
    "Finance",
    "Ideas",
    "Personal Development",
]

# Maps legacy category names to the new 7-category set.
CATEGORY_MIGRATION = {
    "Technology": "Tech",
    "Programming": "DEV",
    "Artificial Intelligence": "Tech",
    "Computer Hardware": "Hardware",
    "Business": "Ideas",
    "Management": "Ideas",
    "Logistics": "Logistics",
    "Finance": "Finance",
    "Personal Development": "Personal Development",
    "Books": "Ideas",
    "Ideas": "Ideas",
    "Lessons": "Personal Development",
}


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist and migrate legacy data."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    conn = get_connection()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            raw_text TEXT NOT NULL,
            category TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            formatted_text TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            section_type TEXT NOT NULL,
            content TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            section_id INTEGER,
            file_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
            FOREIGN KEY (section_id) REFERENCES document_sections(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            run_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_reminders_pending
        ON reminders (sent, run_at)
        """
    )

    conn.commit()
    _migrate_legacy_notes(conn)
    _repair_symbol_documents(conn)
    _purge_sent_reminders(conn)
    conn.close()


def _migrate_legacy_notes(conn: sqlite3.Connection) -> None:
    """Migrate legacy notes into the new document structure (once)."""
    note_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if note_count == 0 or doc_count > 0:
        return

    notes = conn.execute(
        "SELECT id, user_id, raw_text, category, timestamp, formatted_text FROM notes ORDER BY id"
    ).fetchall()

    for note in notes:
        old_category = note["category"]
        new_category = CATEGORY_MIGRATION.get(old_category, "Ideas")

        raw_text = note["raw_text"] or ""
        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        title = lines[0] if lines else "Untitled Document"
        paragraph_content = "\n".join(lines[1:]) if len(lines) > 1 else ""

        now = datetime.now().isoformat()
        cur = conn.execute(
            "INSERT INTO documents (user_id, category, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (note["user_id"], new_category, title, now, now),
        )
        doc_id = cur.lastrowid

        conn.execute(
            "INSERT INTO document_sections (document_id, section_type, content, order_index) VALUES (?, 'title', ?, 0)",
            (doc_id, title),
        )
        if paragraph_content:
            conn.execute(
                "INSERT INTO document_sections (document_id, section_type, content, order_index) VALUES (?, 'paragraph', ?, 1)",
                (doc_id, paragraph_content),
            )

    conn.commit()


def _repair_symbol_documents(conn: sqlite3.Connection) -> None:
    """Repair documents saved by the older, partially migrated parser.

    Two shapes reached the database before the symbol workflow was complete:

    * the raw ``@...`` Telegram line was stored as the document title;
    * a paragraph marker followed by a space (``* Qwen``) stayed inside a
      subtitle.

    The repair re-parses only those surfaces.  Section ids are kept, so
    uploaded photos attached to a section stay attached.  Running it again on
    already-clean data changes nothing.
    """
    documents = conn.execute(
        "SELECT id, title FROM documents ORDER BY id"
    ).fetchall()

    for document in documents:
        try:
            _repair_symbol_document(conn, document["id"], document["title"])
        except Exception:
            logger.exception("Could not repair document %s", document["id"])

    conn.commit()


def _repair_symbol_document(
    conn: sqlite3.Connection, document_id: int, document_title: str
) -> None:
    """Normalize one document in place; see :func:`_repair_symbol_documents`."""
    sections = conn.execute(
        """
        SELECT id, section_type, content, order_index
        FROM document_sections
        WHERE document_id = ?
        ORDER BY order_index, id
        """,
        (document_id,),
    ).fetchall()
    if not sections or sections[0]["section_type"] != TITLE:
        return

    title_section = sections[0]
    raw_title = (document_title or "").strip()
    if not raw_title.startswith("@") and (title_section["content"] or "").strip().startswith("@"):
        raw_title = title_section["content"].strip()

    extra_blocks: list[dict] = []
    new_title = title_section["content"]
    if raw_title.startswith("@"):
        parsed = parse_document_blocks(raw_title)
        if parsed and parsed[0]["type"] == TITLE:
            new_title = parsed[0]["content"]
            extra_blocks = parsed[1:]

    changed = False
    next_order = 0

    if new_title != title_section["content"]:
        conn.execute(
            "UPDATE document_sections SET content = ? WHERE id = ?",
            (new_title, title_section["id"]),
        )
        changed = True
    if new_title and new_title != document_title:
        conn.execute(
            "UPDATE documents SET title = ? WHERE id = ?",
            (new_title, document_id),
        )
        changed = True
    if title_section["order_index"] != next_order:
        conn.execute(
            "UPDATE document_sections SET order_index = ? WHERE id = ?",
            (next_order, title_section["id"]),
        )
    next_order += 1

    for block in extra_blocks:
        conn.execute(
            """
            INSERT INTO document_sections
                (document_id, section_type, content, order_index)
            VALUES (?, ?, ?, ?)
            """,
            (document_id, block["type"], block["content"], next_order),
        )
        changed = True
        next_order += 1

    for section in sections[1:]:
        if section["section_type"] == SUBTITLE:
            parsed = parse_document_blocks("#" + (section["content"] or ""))
            replacement = parsed if parsed and parsed[0]["type"] == SUBTITLE else [
                {"type": SUBTITLE, "content": section["content"]}
            ]
        elif section["section_type"] == PARAGRAPH:
            # Only normalize manual line breaks; never split paragraph text
            # again (a paragraph may legitimately contain "*" or "#").
            replacement = [
                {
                    "type": PARAGRAPH,
                    "content": expand_paragraph_line_breaks(section["content"] or ""),
                }
            ]
        else:
            replacement = [
                {"type": section["section_type"], "content": section["content"]}
            ]

        first = replacement[0]
        if (
            first["type"] != section["section_type"]
            or first["content"] != section["content"]
        ):
            conn.execute(
                "UPDATE document_sections SET section_type = ?, content = ? "
                "WHERE id = ?",
                (first["type"], first["content"], section["id"]),
            )
            changed = True

        if section["order_index"] != next_order:
            conn.execute(
                "UPDATE document_sections SET order_index = ? WHERE id = ?",
                (next_order, section["id"]),
            )
        next_order += 1

        for block in replacement[1:]:
            conn.execute(
                """
                INSERT INTO document_sections
                    (document_id, section_type, content, order_index)
                VALUES (?, ?, ?, ?)
                """,
                (document_id, block["type"], block["content"], next_order),
            )
            changed = True
            next_order += 1

    if changed:
        logger.info(
            "Repaired legacy symbol document %s: %r", document_id, new_title
        )


def _purge_sent_reminders(conn: sqlite3.Connection, older_than_days: int = 30) -> None:
    """Remove delivered reminders that are no longer needed."""
    cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
    conn.execute(
        "DELETE FROM reminders WHERE sent = 1 AND created_at < ?",
        (cutoff,),
    )
    conn.commit()


def add_reminder(user_id: int, chat_id: int, text: str, run_at: str) -> int:
    """Persist a reminder and return its id."""
    conn = get_connection()
    cur = conn.execute(
        """
        INSERT INTO reminders (user_id, chat_id, text, run_at, created_at, sent)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (user_id, chat_id, text, run_at, datetime.now().isoformat()),
    )
    reminder_id = cur.lastrowid
    conn.commit()
    conn.close()
    return reminder_id


def get_reminder(reminder_id: int) -> dict | None:
    """Return one reminder, or None when it no longer exists."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_pending_reminders() -> list[dict]:
    """Return undelivered reminders, oldest first."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, user_id, chat_id, text, run_at, created_at
        FROM reminders
        WHERE sent = 0
        ORDER BY run_at, id
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def mark_reminder_sent(reminder_id: int) -> None:
    """Mark a reminder as delivered so it is not sent again on restart."""
    conn = get_connection()
    conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()


def create_document(user_id: int, category: str, title: str) -> int:
    """Create a new document with a title section. Returns document id."""
    now = datetime.now().isoformat()
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO documents (user_id, category, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, category, title, now, now),
    )
    doc_id = cur.lastrowid
    conn.execute(
        "INSERT INTO document_sections (document_id, section_type, content, order_index) VALUES (?, 'title', ?, 0)",
        (doc_id, title),
    )
    conn.commit()
    conn.close()
    return doc_id


def add_section(document_id: int, section_type: str, content: str) -> int:
    """Add a subtitle or paragraph section. Returns section id."""
    conn = get_connection()
    row = conn.execute(
        "SELECT MAX(order_index) FROM document_sections WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    next_order = (row[0] or 0) + 1
    cur = conn.execute(
        "INSERT INTO document_sections (document_id, section_type, content, order_index) VALUES (?, ?, ?, ?)",
        (document_id, section_type, content, next_order),
    )
    section_id = cur.lastrowid
    conn.execute(
        "UPDATE documents SET updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), document_id),
    )
    conn.commit()
    conn.close()
    return section_id


def add_photo(document_id: int, section_id: int | None, file_id: str, file_path: str) -> int:
    """Add a photo to a document (optionally tied to a section). Returns photo id."""
    conn = get_connection()
    row = conn.execute(
        "SELECT MAX(order_index) FROM document_photos WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    next_order = (row[0] or 0) + 1
    cur = conn.execute(
        "INSERT INTO document_photos (document_id, section_id, file_id, file_path, order_index) VALUES (?, ?, ?, ?, ?)",
        (document_id, section_id, file_id, file_path, next_order),
    )
    photo_id = cur.lastrowid
    conn.commit()
    conn.close()
    return photo_id


def get_documents_by_category(category: str, user_id: int) -> list[dict]:
    """Return documents in a category, newest first."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT d.id, d.title, d.category, d.created_at, d.updated_at
        FROM documents d
        WHERE d.category = ? AND d.user_id = ?
        ORDER BY d.updated_at DESC
        """,
        (category, user_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_documents(user_id: int) -> list[dict]:
    """Return all documents for a user, newest first."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT d.id, d.title, d.category, d.created_at, d.updated_at
        FROM documents d
        WHERE d.user_id = ?
        ORDER BY d.updated_at DESC
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_document_by_id(document_id: int) -> dict | None:
    """Return a full document with its sections and photos."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    doc = dict(row)

    sections = conn.execute(
        """
        SELECT s.id, s.section_type, s.content, s.order_index
        FROM document_sections s
        WHERE s.document_id = ?
        ORDER BY s.order_index
        """,
        (document_id,),
    ).fetchall()
    doc["sections"] = []
    for s in sections:
        section = dict(s)
        photos = conn.execute(
            """
            SELECT id, file_id, file_path, order_index
            FROM document_photos
            WHERE document_id = ? AND section_id = ?
            ORDER BY order_index
            """,
            (document_id, section["id"]),
        ).fetchall()
        section["photos"] = [dict(p) for p in photos]
        doc["sections"].append(section)

    standalone = conn.execute(
        """
        SELECT id, file_id, file_path, order_index
        FROM document_photos
        WHERE document_id = ? AND section_id IS NULL
        ORDER BY order_index
        """,
        (document_id,),
    ).fetchall()
    doc["standalone_photos"] = [dict(p) for p in standalone]

    conn.close()
    return doc


def delete_document(document_id: int) -> bool:
    """Delete a document and its sections/photos. Returns True if deleted."""
    conn = get_connection()
    photo_paths = [
        r["file_path"] for r in conn.execute(
            "SELECT file_path FROM document_photos WHERE document_id = ?",
            (document_id,),
        ).fetchall()
    ]
    cur = conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()

    import os as _os
    for p in photo_paths:
        try:
            if _os.path.exists(p):
                _os.remove(p)
        except Exception:
            pass
    return deleted


def get_categories_with_counts(user_id: int) -> list[dict]:
    """Return categories that have documents, with counts."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT d.category, COUNT(d.id) as count
        FROM documents d
        WHERE d.user_id = ?
        GROUP BY d.category
        ORDER BY d.category
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return [{"category": r["category"], "count": r["count"]} for r in rows]