"""PDF generation for note exports.

Part 5: Clean, professional study-document layout.

- User-defined Title / Subtitle / Paragraph hierarchy only.
- No automatic metadata: no "Added:" dates, no "Exported Notes" header,
  no "Category:" lines, no note counters, no "#" symbols.
- Optional photos are placed inline at their exact position in the
  document, with preserved aspect ratio and automatic page breaks.
- Footer shows only a page number.
"""

import os
from xml.sax.saxutils import escape as xml_escape
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image,
    HRFlowable,
)
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as canvas_module

# Theme colors
DARK_BLUE = colors.HexColor("#1F4E79")
GREY_TEXT = colors.HexColor("#555555")
LIGHT_GREY = colors.HexColor("#F2F2F2")
PAGE_WIDTH, PAGE_HEIGHT = letter
MARGIN = 0.75 * inch
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN


class PageNumCanvas(canvas_module.Canvas):
    """Canvas that draws only a centered page number in the footer."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total):
        self.setFont("Helvetica", 8)
        self.setFillColor(GREY_TEXT)
        self.drawCentredString(
            PAGE_WIDTH / 2.0, 0.45 * inch,
            f"Page {self._pageNumber} of {total}",
        )


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------
styles = getSampleStyleSheet()

title_style = ParagraphStyle(
    "DocTitle",
    parent=styles["Title"],
    fontName="Helvetica-Bold",
    fontSize=22,
    textColor=DARK_BLUE,
    spaceAfter=10,
    alignment=0,  # left
)

subtitle_style = ParagraphStyle(
    "DocSubtitle",
    parent=styles["Heading2"],
    fontName="Helvetica-Bold",
    fontSize=15,
    textColor=DARK_BLUE,
    spaceBefore=16,
    spaceAfter=6,
)

paragraph_style = ParagraphStyle(
    "DocParagraph",
    parent=styles["Normal"],
    fontName="Helvetica",
    fontSize=11,
    leading=16,
    textColor=colors.black,
    spaceAfter=8,
    alignment=0,
)

section_divider = ParagraphStyle(
    "Divider",
    parent=styles["Normal"],
    fontName="Helvetica",
    fontSize=8,
    textColor=LIGHT_GREY,
    spaceAfter=4,
)


def _fit_image(file_path: str, max_width: float, max_height: float):
    """Return (width, height) preserving aspect ratio within limits."""
    img = ImageReader(file_path)
    iw, ih = img.getSize()
    if not iw or not ih:
        return max_width, max_height
    ratio = min(max_width / iw, max_height / ih)
    return iw * ratio, ih * ratio


def _flowable_text(text: str) -> str:
    """Escape user text for ReportLab and keep its original line breaks.

    ReportLab's Paragraph parses a small XML-like markup, so literal ``&``,
    ``<`` and ``>`` from the user must be escaped.  Newlines are converted to
    ``<br/>`` so multi-line paragraphs render exactly as written.
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return xml_escape(normalized).replace("\n", "<br/>")


def generate_document_pdf(document: dict, output_path: str) -> None:
    """Generate a clean, professional PDF from a document dict.

    `document` has keys: title, sections (list with section_type, content,
    photos), standalone_photos. Photos are placed inline at their exact
    position in the content flow.
    """
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=0.9 * inch,
        bottomMargin=0.85 * inch,
        title=document.get("title", "Untitled"),
        author="Personal Knowledge Library",
    )

    story = []

    # Document title (user-defined)
    story.append(
        Paragraph(
            _flowable_text(document.get("title") or "Untitled Document"),
            title_style,
        )
    )
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1.5, color=DARK_BLUE, spaceAfter=12))

    for section in document.get("sections", []):
        stype = section["section_type"]
        content = section["content"]

        if stype == "title":
            # The document title is already rendered; skip duplicate title sections
            continue

        if stype == "subtitle":
            story.append(Paragraph(_flowable_text(content), subtitle_style))

        elif stype == "paragraph":
            story.append(Paragraph(_flowable_text(content), paragraph_style))

        # Photos attached to this section, inline
        for photo in section.get("photos", []):
            file_path = photo["file_path"]
            if not os.path.exists(file_path):
                continue
            max_w = CONTENT_WIDTH
            max_h = 3.0 * inch
            w, h = _fit_image(file_path, max_w, max_h)
            story.append(Image(file_path, width=w, height=h))
            story.append(Spacer(1, 8))

    # Standalone photos (not attached to a section)
    for photo in document.get("standalone_photos", []):
        file_path = photo["file_path"]
        if not os.path.exists(file_path):
            continue
        max_w = CONTENT_WIDTH
        max_h = 3.0 * inch
        w, h = _fit_image(file_path, max_w, max_h)
        story.append(Image(file_path, width=w, height=h))
        story.append(Spacer(1, 8))

    doc.build(
        story,
        canvasmaker=PageNumCanvas,
    )