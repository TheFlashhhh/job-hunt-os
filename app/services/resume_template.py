"""The resume's visual system, matched to the candidate's existing Spark-style resume.

Measured from that PDF rather than guessed at:

    page            US Letter, 612 x 792 pt
    body face       TeX Gyre Termes — a Times clone (the LaTeX default)
    body size       10 pt
    name            ~24 pt bold, small caps, centred
    section heads   12 pt small caps, thin rule to the right edge
    margins         0.34 in left, 0.46 in right, 0.37 in top

ReportLab's built-in Times-Roman/Bold/Italic is the same typeface family as
TeX Gyre Termes and is one of the PDF base-14 fonts, so it needs no font file
shipped with the project and renders identically everywhere.

Two constraints shape the implementation:

**Small caps are simulated.** The base-14 Times has no small-caps variant, so a
heading is drawn as a full-size first letter followed by the remainder at 78%.
That is what the reference does optically.

**Right-aligned dates use two-column tables, not tab stops.** A table cell pair
extracts as left-then-right in reading order, so the date lands beside its
employer for a human and after it for a parser. Tab-leader alignment would put
the geometry first and the reading order second.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from os import PathLike
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.fonts import addMapping
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# -- geometry, from the reference -------------------------------------------
PAGE_SIZE = LETTER
LEFT_MARGIN = 0.4 * inch
RIGHT_MARGIN = 0.4 * inch
TOP_MARGIN = 0.36 * inch
BOTTOM_MARGIN = 0.36 * inch
CONTENT_WIDTH = PAGE_SIZE[0] - LEFT_MARGIN - RIGHT_MARGIN

# -- typography --------------------------------------------------------------
#
# ATS-SAFE BULLETS.
#
# ReportLab's WinAnsi table carries `bullet` twice — at 0x7F as well as at its
# real 0x95 — and encodes to the first match, so every bullet on the page was
# written as byte 0x7F. WinAnsi leaves 0x7F undefined, so a text extractor
# (pypdf here, and the same table is what Tika and pdfbox read) decoded each one
# as U+007F DELETE: the document a parser saw began every bullet line with a
# control character. Measured, not assumed — see `tests/test_resume_template.py`.
#
# Clearing 0x7F in a copy of the encoding pushes the encoder onto 0x95, which
# round-trips as U+2022. The fonts, metrics and glyphs are the base-14 Times
# faces either way, so nothing about the rendered page changes.
_ATS_ENCODING = "ATSWinAnsiEncoding"

_WINANSI_CODEC = codecs.lookup("cp1252")


def _find_ats_codec(name: str) -> codecs.CodecInfo | None:
    """ReportLab encodes Type-1 text through a Python codec named for the
    encoding, and a custom encoding name has none. WinAnsi *is* cp1252, so the
    alias points at it — the difference is only in the PDF encoding vector."""
    return _WINANSI_CODEC if name.lower().replace("-", "_") == "atswinansiencoding" else None


def _register_ats_fonts() -> None:
    """Register Times under an encoding whose 0x7F slot is empty. Idempotent."""
    if "Times-Roman-ATS" in pdfmetrics.getRegisteredFontNames():
        return
    codecs.register(_find_ats_codec)
    encoding = pdfmetrics.Encoding(_ATS_ENCODING, "WinAnsiEncoding")
    encoding[0x7F] = None
    pdfmetrics.registerEncoding(encoding)
    for base, bold, italic in (
        ("Times-Roman", 0, 0),
        ("Times-Bold", 1, 0),
        ("Times-Italic", 0, 1),
        ("Times-BoldItalic", 1, 1),
    ):
        pdfmetrics.registerFont(pdfmetrics.Font(f"{base}-ATS", base, _ATS_ENCODING))
        # Without the mapping, ReportLab cannot resolve <b> and <i> inside a
        # paragraph set in one of these faces.
        addMapping("times-ats", bold, italic, f"{base}-ATS")


_register_ats_fonts()

SERIF = "Times-Roman-ATS"
SERIF_BOLD = "Times-Bold-ATS"
SERIF_ITALIC = "Times-Italic-ATS"

BODY_SIZE = 10.0
BODY_LEADING = 11.6
NAME_SIZE = 22.0
HEADING_SIZE = 11.5
#: Small caps: the letters after the first are drawn at this fraction.
SMALL_CAP_RATIO = 0.78


def small_caps(text: str, *, size: float) -> str:
    """Simulated small caps as ReportLab inline markup.

    The base-14 Times has no small-caps cut. Each word keeps its first letter
    at full size and drops the rest to 78%, which is what the reference's
    ``\\scshape`` produces optically.
    """
    smaller = round(size * SMALL_CAP_RATIO, 2)
    parts: list[str] = []
    for word in text.split(" "):
        if not word:
            continue
        head, tail = word[0], word[1:]
        chunk = escape(head.upper())
        if tail:
            chunk += f'<font size="{smaller}">{escape(tail.upper())}</font>'
        parts.append(chunk)
    return " ".join(parts)


class SectionRule(Flowable):
    """A small-caps section heading with a hairline running to the right edge.

    The label is laid out as a Paragraph, not as per-character ``drawString``
    calls. That distinction is not cosmetic: drawing "EDUCATION" one letter at a
    time emits nine separately-positioned text operations, and pypdf — like some
    ATS parsers — extracts them as nine lines. The section headings became
    invisible to any parser searching for the word "EDUCATION". A Paragraph
    emits one run and extracts as one word.
    """

    def __init__(self, label: str, width: float) -> None:
        super().__init__()
        self.label = label
        self.width = width
        self._style = ParagraphStyle(
            "SectionHeading",
            fontName=SERIF,
            fontSize=HEADING_SIZE,
            leading=HEADING_SIZE + 1.5,
            spaceBefore=0,
            spaceAfter=0,
        )
        self._para = Paragraph(small_caps(label, size=HEADING_SIZE), self._style)
        self.height = HEADING_SIZE + 4.5

    def wrap(self, available_width: float, available_height: float):
        self.width = available_width
        self._para.wrap(available_width, available_height)
        return available_width, self.height

    def draw(self) -> None:
        canvas = self.canv
        self._para.drawOn(canvas, 0, 3.0)

        # The rule starts after the rendered label. Measured from the real font
        # metrics, accounting for the smaller small-cap letters.
        text_width = 0.0
        smaller = HEADING_SIZE * SMALL_CAP_RATIO
        for index, char in enumerate(self.label.upper()):
            first = index == 0 or self.label[index - 1] == " "
            size = HEADING_SIZE if first else smaller
            text_width += canvas.stringWidth(char, SERIF, size)

        canvas.saveState()
        canvas.setLineWidth(0.5)
        canvas.setStrokeColor(colors.black)
        baseline = 4.6
        canvas.line(text_width + 5, baseline, self.width, baseline)
        canvas.restoreState()


# -- selective emphasis ------------------------------------------------------

#: Fragments the reference bolds: counts, percentages, ratios, measured spans.
#: Every pattern matches text that must ALREADY be present — this only wraps
#: substrings, it never computes or introduces a figure. A metric the Evidence
#: Bank did not state cannot appear here, because there is nothing to match.
_METRIC_PATTERNS = (
    r"\d[\d,]*\+?\s*(?:unit tests|users|records|endpoints|workflows|topics|"
    r"tables|fields|counties|lap records|defects|release cycles|"
    r"scenarios?|checkpoints?)",
    r"\d[\d,]*\s*[-–]\s*\d[\d,]*\s*(?:days|hours|users|records)",
    r"\d+(?:\.\d+)?%",
    r"\d+/\d+",
    r"\d+(?:\.\d+)?\s*(?:miles|hours|days|months)",
    r"\d+[-–]\w+(?:\s+\w+)?\s+(?:suite|backtest|strategy)",
    r"\b\d[\d,]{2,}\b",
    r"\b\d+\s*\+\b",
    r"\b\d+-(?:person|scenario|checkpoint|stop|hour|page)\b",
)
_METRIC_RE = re.compile("|".join(f"(?:{p})" for p in _METRIC_PATTERNS), re.IGNORECASE)

#: At most this many bolded runs per bullet. The reference emphasises a couple
#: of figures per line; bolding every number would flatten the emphasis back to
#: nothing and read as shouting.
MAX_EMPHASIS_PER_BULLET = 3


def emphasize(text: str) -> str:
    """Bold the measured figures already present in a bullet.

    Returns ReportLab markup with the source text escaped. Only verbatim
    substrings are wrapped; nothing is rewritten, rounded or added.
    """
    matches: list[tuple[int, int]] = []
    for match in _METRIC_RE.finditer(text):
        if len(matches) >= MAX_EMPHASIS_PER_BULLET:
            break
        start, end = match.span()
        if matches and start < matches[-1][1]:
            continue  # overlapping run
        matches.append((start, end))

    if not matches:
        return escape(text)

    out: list[str] = []
    cursor = 0
    for start, end in matches:
        out.append(escape(text[cursor:start]))
        out.append(f"<b>{escape(text[start:end])}</b>")
        cursor = end
    out.append(escape(text[cursor:]))
    return "".join(out)


# -- styles ------------------------------------------------------------------


@dataclass
class Styles:
    name: ParagraphStyle
    contact: ParagraphStyle
    summary: ParagraphStyle
    entry_left: ParagraphStyle
    entry_right: ParagraphStyle
    sub_left: ParagraphStyle
    sub_right: ParagraphStyle
    bullet: ParagraphStyle
    skill: ParagraphStyle


def build_styles() -> Styles:
    return Styles(
        name=ParagraphStyle(
            "Name",
            fontName=SERIF_BOLD,
            fontSize=NAME_SIZE,
            leading=NAME_SIZE + 2,
            alignment=TA_CENTER,
            spaceAfter=2,
        ),
        contact=ParagraphStyle(
            "Contact",
            fontName=SERIF,
            fontSize=BODY_SIZE,
            leading=BODY_SIZE + 2,
            alignment=TA_CENTER,
            spaceAfter=4,
        ),
        summary=ParagraphStyle(
            "Summary",
            fontName=SERIF,
            fontSize=BODY_SIZE,
            leading=BODY_LEADING,
            alignment=TA_JUSTIFY,
            spaceAfter=1,
        ),
        entry_left=ParagraphStyle(
            "EntryLeft", fontName=SERIF_BOLD, fontSize=BODY_SIZE, leading=BODY_LEADING
        ),
        entry_right=ParagraphStyle(
            "EntryRight",
            fontName=SERIF,
            fontSize=BODY_SIZE,
            leading=BODY_LEADING,
            alignment=2,  # right
        ),
        sub_left=ParagraphStyle(
            "SubLeft", fontName=SERIF_ITALIC, fontSize=BODY_SIZE, leading=BODY_LEADING
        ),
        sub_right=ParagraphStyle(
            "SubRight",
            fontName=SERIF_ITALIC,
            fontSize=BODY_SIZE,
            leading=BODY_LEADING,
            alignment=2,
        ),
        bullet=ParagraphStyle(
            "Bullet",
            fontName=SERIF,
            fontSize=BODY_SIZE,
            leading=BODY_LEADING,
            alignment=TA_JUSTIFY,
            leftIndent=13,
            bulletIndent=4,
            spaceAfter=0.6,
            # Without this the bullet is drawn in Helvetica — a different
            # family from the rest of the page, and it extracts as a
            # replacement character instead of a bullet.
            bulletFontName=SERIF,
            bulletFontSize=BODY_SIZE,
        ),
        skill=ParagraphStyle(
            "Skill",
            fontName=SERIF,
            fontSize=BODY_SIZE,
            leading=BODY_LEADING,
            leftIndent=13,
            bulletIndent=4,
            spaceAfter=0.6,
            bulletFontName=SERIF,
            bulletFontSize=BODY_SIZE,
        ),
    )


# -- composable rows ---------------------------------------------------------

_ROW_STYLE = TableStyle(
    [
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
)


def two_column_row(left: str, right: str, styles: tuple[ParagraphStyle, ParagraphStyle]) -> Table:
    """Left-aligned text with its date or location set against the right edge.

    A two-cell table rather than a tab stop: extraction yields the left cell
    then the right cell, which keeps a parser reading "OpenText" before
    "Sep 2023 - Mar 2024" instead of interleaving columns.
    """
    left_style, right_style = styles
    right_width = 2.0 * inch
    table = Table(
        [[Paragraph(left, left_style), Paragraph(right, right_style)]],
        colWidths=[CONTENT_WIDTH - right_width, right_width],
        hAlign="LEFT",
    )
    table.setStyle(_ROW_STYLE)
    return table


def bullet_paragraph(text: str, styles: Styles) -> Paragraph:
    return Paragraph(emphasize(text), styles.bullet, bulletText="•")


def skill_paragraph(label: str, skills: str, styles: Styles) -> Paragraph:
    return Paragraph(f"<b>{escape(label)}:</b> {escape(skills)}", styles.skill, bulletText="•")


@dataclass
class RenderedDoc:
    """Everything the PDF builder needs, already ordered."""

    story: list = field(default_factory=list)


class _ZeroPaddedDoc(SimpleDocTemplate):
    """A document whose declared margins are its actual margins.

    ReportLab's default frame adds 6pt of padding inside each margin. That
    silently pushed every right-aligned date 6pt past the right margin and made
    the real left margin 0.48in when the template declared 0.40in — so geometry
    measured from the reference PDF could never be reproduced exactly. Zeroing
    the padding makes the numbers in this module mean what they say.
    """

    def build(self, flowables, **kwargs):  # type: ignore[override]
        self._calc()
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
            id="body",
        )
        # SimpleDocTemplate's page handler switches to a template literally
        # named "Later" after page one, so both must exist even though a
        # one-page resume never reaches the second.
        self.addPageTemplates([PageTemplate("First", [frame]), PageTemplate("Later", [frame])])
        return BaseDocTemplate.build(self, flowables, **kwargs)


def make_document(path: str | Any, author: str) -> SimpleDocTemplate:
    """`path` may be a filename, a `Path`, or a writable buffer.

    ReportLab wants a `str` for a filename and rejects a `Path`, but takes a
    file object as-is — which is what lets the one-page fitting loop render and
    count without touching the filesystem (D-52).
    """
    target = str(path) if isinstance(path, (str, PathLike)) else path
    return _ZeroPaddedDoc(
        target,
        pagesize=PAGE_SIZE,
        leftMargin=LEFT_MARGIN,
        rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        # Metadata is the candidate's name only. No job id, no base name.
        title=f"{author} - Resume",
        author=author,
    )


__all__ = [
    "BODY_SIZE",
    "CONTENT_WIDTH",
    "HEADING_SIZE",
    "NAME_SIZE",
    "SERIF",
    "SERIF_BOLD",
    "SERIF_ITALIC",
    "KeepTogether",
    "RenderedDoc",
    "SectionRule",
    "Spacer",
    "Styles",
    "build_styles",
    "bullet_paragraph",
    "emphasize",
    "make_document",
    "skill_paragraph",
    "small_caps",
    "two_column_row",
]


# -- contact line, linked ----------------------------------------------------


def contact_paragraph(items: list[tuple[str, str | None]]) -> str:
    """The header contact line as ReportLab markup, with working hyperlinks.

    Displayed text stays the short form a human reads on paper
    (`linkedin.com/in/…`); the `href` underneath is fully qualified. Extraction
    returns the display text either way, so an ATS reads exactly what a person
    reads.

    The generous spacing between items is the reference resume's, and the
    trailing plain space after each non-breaking run is what lets a parser
    splitting on ASCII whitespace still separate them.
    """
    separator = "&nbsp;&nbsp;&nbsp; "
    parts: list[str] = []
    for text, href in items:
        if not text:
            continue
        label = escape(text)
        parts.append(f'<link href="{escape(href, {chr(34): "&quot;"})}">{label}</link>' if href else label)
    return separator.join(parts)


def add_docx_hyperlink(paragraph: Any, text: str, href: str) -> Any:
    """Append a real hyperlink run to a python-docx paragraph.

    python-docx has no public hyperlink API, so the relationship and the `w:hyperlink`
    element are built directly. Worth the twenty lines: a DOCX whose contact
    line is inert text loses the link the moment a recruiter opens it in Word,
    and this is the document some portals insist on.
    """
    from docx.oxml.ns import qn
    from docx.oxml.shared import OxmlElement

    part = paragraph.part
    r_id = part.relate_to(href, _HYPERLINK_RELATIONSHIP, is_external=True)

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    # Word's own Hyperlink character style may be absent in a blank document,
    # so the colour and underline are set explicitly rather than by reference.
    colour = OxmlElement("w:color")
    colour.set(qn("w:val"), "0563C1")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    properties.append(colour)
    properties.append(underline)
    run.append(properties)

    text_element = OxmlElement("w:t")
    text_element.text = text
    run.append(text_element)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)
    return hyperlink


_HYPERLINK_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
)
