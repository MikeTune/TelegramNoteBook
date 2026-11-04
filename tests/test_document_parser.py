"""Unit tests for the authoritative symbol-based document parser."""

import unittest

from document_parser import (
    PARAGRAPH,
    SUBTITLE,
    TITLE,
    group_documents,
    parse_document_blocks,
    parse_documents,
)


class CompactInputTests(unittest.TestCase):
    def test_three_markers_on_one_line(self):
        blocks = parse_document_blocks(
            "@Windows #Uninstall stubborn apps *Qwen3.5-9B-Abliterated"
        )
        self.assertEqual(
            blocks,
            [
                {"type": TITLE, "content": "Windows"},
                {"type": SUBTITLE, "content": "Uninstall stubborn apps"},
                {"type": PARAGRAPH, "content": "Qwen3.5-9B-Abliterated"},
            ],
        )

    def test_inline_markers_may_be_followed_by_a_space(self):
        blocks = parse_document_blocks("@Doc # Subtitle here * Paragraph here")
        self.assertEqual(
            blocks,
            [
                {"type": TITLE, "content": "Doc"},
                {"type": SUBTITLE, "content": "Subtitle here"},
                {"type": PARAGRAPH, "content": "Paragraph here"},
            ],
        )

    def test_inline_hash_followed_by_space_stays_in_subtitle(self):
        blocks = parse_document_blocks("#issue # 5")
        self.assertEqual(blocks, [{"type": SUBTITLE, "content": "issue # 5"}])

    def test_markers_with_punctuation_and_symbols(self):
        blocks = parse_document_blocks("@Q&A 2026 #C++ & APIs *cost: 3.5 < 4 > 2")
        self.assertEqual(blocks[0]["content"], "Q&A 2026")
        self.assertEqual(blocks[1]["content"], "C++ & APIs")
        self.assertEqual(blocks[2]["content"], "cost: 3.5 < 4 > 2")


class MultilineInputTests(unittest.TestCase):
    def test_blank_lines_between_blocks(self):
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
        blocks = parse_document_blocks(text)
        self.assertEqual(
            [(b["type"], b["content"]) for b in blocks],
            [
                (TITLE, "Computer Architecture"),
                (SUBTITLE, "CPU"),
                (PARAGRAPH, "The CPU executes instructions and processes data."),
                (SUBTITLE, "RAM"),
                (
                    PARAGRAPH,
                    "RAM temporarily stores information currently being used by the computer.",
                ),
            ],
        )

    def test_multiple_paragraphs_under_one_subtitle(self):
        text = "@My Doc\n#Section\n*First para.\n*Second para.\n*Third para."
        blocks = parse_document_blocks(text)
        self.assertEqual(
            [(b["type"], b["content"]) for b in blocks],
            [
                (TITLE, "My Doc"),
                (SUBTITLE, "Section"),
                (PARAGRAPH, "First para."),
                (PARAGRAPH, "Second para."),
                (PARAGRAPH, "Third para."),
            ],
        )

    def test_multiline_block_keeps_line_breaks(self):
        text = "@Doc\n*Line one\nLine two\n  indented line"
        blocks = parse_document_blocks(text)
        self.assertEqual(blocks[1]["content"], "Line one\nLine two\n  indented line")

    def test_indented_markers(self):
        blocks = parse_document_blocks("   @Doc\n\t#Section\n  *Paragraph")
        self.assertEqual(
            [b["type"] for b in blocks], [TITLE, SUBTITLE, PARAGRAPH]
        )


class PreservationTests(unittest.TestCase):
    def test_url_and_pipe_command(self):
        text = "@Uninstall\n#Edge\n*irm https://christitus.com/win | iex"
        blocks = parse_document_blocks(text)
        self.assertEqual(blocks[2]["content"], "irm https://christitus.com/win | iex")

    def test_backslashes_and_windows_paths(self):
        text = "@Paths\n#Drivers\n*C:\\Users\\Mike\\docs\n*irm https://x.test/a?b=1&c=2"
        blocks = parse_document_blocks(text)
        self.assertEqual(blocks[2]["content"], "C:\\Users\\Mike\\docs")
        self.assertEqual(blocks[3]["content"], "irm https://x.test/a?b=1&c=2")

    def test_unicode_and_emoji(self):
        text = "@Résumé 🚀 #Überblick *naïve café — 日本語"
        blocks = parse_document_blocks(text)
        self.assertEqual(blocks[0]["content"], "Résumé 🚀")
        self.assertEqual(blocks[2]["content"], "naïve café — 日本語")


class FalsePositiveTests(unittest.TestCase):
    def test_email_mention_csharp_multiplication_and_url_fragment(self):
        text = (
            "#Notes\n"
            "Email user@example.com, write C# code, compute 2 * 3 = 6, "
            "open https://example.com/#frag and ask @alice."
        )
        blocks = parse_document_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], SUBTITLE)
        for fragment in (
            "user@example.com",
            "C#",
            "2 * 3",
            "https://example.com/#frag",
            "@alice",
        ):
            self.assertIn(fragment, blocks[0]["content"])

    def test_inline_at_is_not_a_title(self):
        blocks = parse_document_blocks("@Doc\n*Ping @alice about the deploy")
        self.assertEqual([b["type"] for b in blocks], [TITLE, PARAGRAPH])
        self.assertIn("@alice", blocks[1]["content"])

    def test_inline_hash_or_star_followed_by_space_is_not_a_marker(self):
        blocks = parse_document_blocks(
            "@Doc\n*Issue # 5 and 2 * 3 are ordinary content"
        )
        self.assertEqual([b["type"] for b in blocks], [TITLE, PARAGRAPH])
        self.assertEqual(
            blocks[1]["content"], "Issue # 5 and 2 * 3 are ordinary content"
        )

    def test_url_at_start_of_line_is_not_a_document(self):
        self.assertEqual(parse_document_blocks("https://t.me/@someone"), [])

    def test_plain_text_has_no_blocks(self):
        self.assertEqual(
            parse_document_blocks("Title: My Doc\nSubtitle: Section\nParagraph: Body"),
            [],
        )

    def test_markdown_strong_emphasis_is_not_a_paragraph(self):
        self.assertEqual(parse_document_blocks("**bold** text"), [])


class EscapingTests(unittest.TestCase):
    def test_escaped_markers_remain_content(self):
        blocks = parse_document_blocks(
            "@Doc\n*Literal \\@ mention and \\# hash and \\* star"
        )
        self.assertEqual([b["type"] for b in blocks], [TITLE, PARAGRAPH])
        self.assertEqual(
            blocks[1]["content"], "Literal \\@ mention and \\# hash and \\* star"
        )


class ParagraphLineBreakTests(unittest.TestCase):
    def test_apostrophe_creates_manual_line_breaks(self):
        blocks = parse_document_blocks(
            "*The CPU processes instructions'It performs calculations"
            "'It controls other components."
        )
        self.assertEqual(
            blocks,
            [
                {
                    "type": PARAGRAPH,
                    "content": (
                        "The CPU processes instructions\n"
                        "It performs calculations\n"
                        "It controls other components."
                    ),
                }
            ],
        )

    def test_normal_apostrophes_are_preserved(self):
        content = (
            "It's O'Brien and d'Artagnan's plan; students' work, "
            "rock 'n' roll, 5'10\"."
        )
        blocks = parse_document_blocks("*" + content)
        self.assertEqual(blocks[0]["content"], content)

    def test_apostrophe_in_subtitle_is_not_a_line_break(self):
        blocks = parse_document_blocks("@Doc #Section'Heading *Body")
        self.assertEqual(blocks[1]["content"], "Section'Heading")

    def test_line_breaks_only_inside_the_same_paragraph(self):
        blocks = parse_document_blocks(
            "@Doc\n#Section\n*First line'Second line\n*Another paragraph"
        )
        self.assertEqual([b["type"] for b in blocks], [TITLE, SUBTITLE, PARAGRAPH, PARAGRAPH])
        self.assertEqual(blocks[2]["content"], "First line\nSecond line")
        self.assertEqual(blocks[3]["content"], "Another paragraph")


class DocumentGroupingTests(unittest.TestCase):
    def test_new_title_starts_a_new_document(self):
        docs = parse_documents("@One\n#A\n*first\n@Two\n#B\n*second")
        self.assertEqual([d["title"] for d in docs], ["One", "Two"])
        self.assertEqual(
            [(b["type"], b["content"]) for b in docs[0]["blocks"]],
            [(SUBTITLE, "A"), (PARAGRAPH, "first")],
        )
        self.assertEqual(
            [(b["type"], b["content"]) for b in docs[1]["blocks"]],
            [(SUBTITLE, "B"), (PARAGRAPH, "second")],
        )

    def test_group_documents_without_title(self):
        docs = group_documents(parse_document_blocks("#Orphan\n*body"))
        self.assertEqual(len(docs), 1)
        self.assertIsNone(docs[0]["title"])
        self.assertEqual(len(docs[0]["blocks"]), 2)


if __name__ == "__main__":
    unittest.main()
