"""A small, dependency-free PDF writer for printable maintenance service records.

It lays out a title block and a wrapped table with the PDF standard Helvetica
fonts (WinAnsi encoding), so no fonts or libraries need to be installed in
Home Assistant. Text that can't be encoded in Windows-1252 becomes "?".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

PAGE_W, PAGE_H = 612.0, 792.0  # US Letter, points
MARGIN = 48.0

# Helvetica advance widths (1/1000 em) for ASCII 32..126.
_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)


def text_width(text: str, size: float, bold: bool = False) -> float:
    total = sum(_WIDTHS[ord(ch) - 32] if 32 <= ord(ch) <= 126 else 556 for ch in text)
    return total * size / 1000 * (1.06 if bold else 1.0)


def wrap(text: str, width: float, size: float, bold: bool = False) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text or "").replace("\r", "").split("\n"):
        words = paragraph.split(" ")
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip() if line else word
            if text_width(candidate, size, bold) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            # Break words longer than the column.
            while text_width(word, size, bold) > width and len(word) > 1:
                cut = len(word)
                while cut > 1 and text_width(word[:cut], size, bold) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines or [""]


def _escape(text: str) -> bytes:
    cleaned = "".join(ch if ch >= " " else " " for ch in text)
    raw = cleaned.encode("cp1252", "replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


@dataclass
class Column:
    title: str
    width: float
    align: str = "left"


class _Page:
    def __init__(self) -> None:
        self.ops: list[bytes] = []

    def text(self, x: float, y: float, text: str, size: float, bold: bool = False, gray: float = 0.0) -> None:
        font = b"/F2" if bold else b"/F1"
        self.ops.append(b"BT %.2f g %s %.1f Tf %.2f %.2f Td (" % (gray, font, size, x, y) + _escape(text) + b") Tj ET")

    def line(self, x1: float, y1: float, x2: float, y2: float, gray: float = 0.8, width: float = 0.5) -> None:
        self.ops.append(b"%.2f G %.2f w %.2f %.2f m %.2f %.2f l S" % (gray, width, x1, y1, x2, y2))

    def rect(self, x: float, y: float, w: float, h: float, gray: float) -> None:
        self.ops.append(b"%.2f g %.2f %.2f %.2f %.2f re f" % (gray, x, y, w, h))


def render_table_document(
    *,
    title: str,
    subtitle_lines: Sequence[str],
    columns: Sequence[Column],
    rows: Sequence[Sequence[str]],
    footer_line: str,
    generated_line: str,
) -> bytes:
    """Lay out a titled, paginated table and return the PDF bytes."""
    size, leading, head_size = 8.5, 11.0, 8.5
    pages: list[_Page] = []
    page = _Page()
    y = PAGE_H - MARGIN

    def table_header(target: _Page, top: float) -> float:
        target.rect(MARGIN, top - 4, PAGE_W - 2 * MARGIN, leading + 4, 0.93)
        x = MARGIN + 4
        for column in columns:
            target.text(x, top, column.title, head_size, bold=True, gray=0.15)
            x += column.width
        return top - leading - 6

    page.text(MARGIN, y - 18, title, 18, bold=True)
    y -= 34
    for line in subtitle_lines:
        page.text(MARGIN, y, line, 10, gray=0.3)
        y -= 14
    y -= 8
    y = table_header(page, y)
    for row in rows:
        cells = [wrap(value, column.width - 8, size) for value, column in zip(row, columns)]
        height = max(len(cell) for cell in cells) * leading + 4
        if y - height < MARGIN + 30:
            pages.append(page)
            page = _Page()
            y = table_header(page, PAGE_H - MARGIN - 10)
        x = MARGIN + 4
        for cell, column in zip(cells, columns):
            line_y = y
            for text in cell:
                offset = column.width - 8 - text_width(text, size) if column.align == "right" else 0
                page.text(x + offset, line_y, text, size)
                line_y -= leading
            x += column.width
        y -= height
        page.line(MARGIN, y + leading - 2, PAGE_W - MARGIN, y + leading - 2)
    if y < MARGIN + 40:
        pages.append(page)
        page = _Page()
        y = PAGE_H - MARGIN - 10
    page.text(MARGIN, y - 6, footer_line, 10, bold=True)
    pages.append(page)

    for number, target in enumerate(pages, start=1):
        target.text(MARGIN, MARGIN - 20, generated_line, 7.5, gray=0.45)
        label = f"Page {number} of {len(pages)}"
        target.text(PAGE_W - MARGIN - text_width(label, 7.5), MARGIN - 20, label, 7.5, gray=0.45)
    return _assemble(pages, title)


def _assemble(pages: Sequence[_Page], title: str) -> bytes:
    objects: list[bytes] = []
    page_ids = [5 + i * 2 for i in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % pid for pid in page_ids) + b"] /Count %d >>" % len(pages))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    for page, page_id in zip(pages, page_ids):
        stream = b"\n".join(page.ops)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
            % (int(PAGE_W), int(PAGE_H), page_id + 1)
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Title (" + _escape(title) + b") /Producer (TaskDragon) >>")
    info_id = len(objects)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, info_id, xref)
    return bytes(out)
