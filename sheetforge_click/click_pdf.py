"""Preserve page-level evidence for the local Click board corpus."""

from __future__ import annotations

import math
import re
import statistics
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import quote

import pymupdf as pdf

EXTRACT_VERSION = 1
TEXT_FLAGS = pdf.TEXTFLAGS_DICT & ~pdf.TEXT_PRESERVE_IMAGES & ~pdf.TEXT_CID_FOR_UNKNOWN_UNICODE
FIGURE_LABEL = re.compile(r"^\s*(?:fig(?:ure)?[.\s]+|graph\s+|diagram\s+)\d", re.I)
SECTION_LIMIT = 6500


def source_link(source: str, label: str, page: int | None = None) -> str:
    fragment = f"#page={page}" if page is not None else ""
    return f"[{label}]({quote(source, safe='/.:')}{fragment})"


def clean(text: str) -> str:
    # Keep engineering symbols, spacing, and the original language.
    if re.search(r"[\ud800-\udfff]", text):
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()


def usable_count(text: str) -> int:
    return sum(c.isalnum() for c in text)


def encoding_suspect(text: str) -> bool:
    invalid = sum(c == "\ufffd" or unicodedata.category(c) == "Co" for c in text)
    return invalid >= 3 and invalid > len(text) * 0.015


def box(coordinates) -> list[float]:
    return [round(x, 1) for x in coordinates]


def text_blocks(page: pdf.Page) -> list[dict]:
    blocks = []
    for raw in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]:
        lines = []
        for line in raw["lines"]:
            spans = line["spans"]
            if not spans:
                continue
            text = clean("".join(span["text"] for span in spans))
            if text:
                lines.append({
                    "text": text,
                    "bbox": box(line["bbox"]),
                    "size": max(span["size"] for span in spans),
                    "bold": all(span["flags"] & pdf.TEXT_FONT_BOLD for span in spans),
                })
        if lines:
            blocks.append({"bbox": box(raw["bbox"]), "lines": lines})
    return blocks


def reading_order(blocks: list[dict], width: float) -> list[dict]:
    """Group adjacent text columns inside bands separated by spanning blocks."""
    ordered = sorted(blocks, key=lambda block: (block["bbox"][1], block["bbox"][0]))
    if len(ordered) < 4:
        return ordered
    for center in (width / 2, width * 0.45, width * 0.55):
        left = [b for b in ordered if b["bbox"][2] <= center + 2]
        right = [b for b in ordered if b["bbox"][0] >= center - 2]
        if len(left) < 2 or len(right) < 2:
            continue
        vertical_overlap = (
            min(max(b["bbox"][3] for b in left), max(b["bbox"][3] for b in right))
            - max(min(b["bbox"][1] for b in left), min(b["bbox"][1] for b in right))
        )
        if vertical_overlap < 35:
            continue
        left_ids = {id(b) for b in left}
        right_ids = {id(b) for b in right}
        result = []
        band = []
        for block in ordered:
            if id(block) in left_ids or id(block) in right_ids:
                band.append(block)
            else:
                result.extend(sorted(band, key=lambda b: id(b) not in left_ids))
                result.append(block)
                band.clear()
        result.extend(sorted(band, key=lambda b: id(b) not in left_ids))
        return result
    return ordered


def markdown_text(blocks: list[dict]) -> list[str]:
    body_sizes = [line["size"] for block in blocks for line in block["lines"]
                  for _ in range(max(1, len(line["text"]) // 15))]
    median = statistics.median(body_sizes) if body_sizes else 0
    result = []
    for block in blocks:
        lines = block["lines"]
        for line in lines:
            # Source strings remain literal even if they contain Markdown syntax.
            line["text"] = re.sub(r"(?m)^(?=[#>]|[-+*]\s|\d+[.)]\s)", r"\\", line["text"])
        if len(lines) == 1 and len(lines[0]["text"]) <= 115 and (
            lines[0]["size"] >= median * 1.22 or
            (lines[0]["bold"] and lines[0]["size"] >= median * 1.04)
        ):
            result.append("#### " + lines[0]["text"])
        else:
            result.append("  \n".join(line["text"] for line in lines))
    return result


def tables_on(page: pdf.Page, blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Prefer actual rules and use alignment only for unusually tabular pages."""
    drawings = page.get_drawings()
    # A diagram-heavy or malformed page can contain thousands of tiny edges.
    paths = [path for path in drawings if len(path["items"]) <= 1500
             and path.get("type") in {"s", "fs", "f"}]
    found = []
    if paths and sum(len(path["items"]) for path in paths) <= 420:
        found = page.find_tables(strategy="lines_strict", paths=paths).tables
    short_lines = sum(len(line["text"]) <= 30 for b in blocks for line in b["lines"])
    if not found and len(drawings) < 90 and 24 <= short_lines <= 190:
        # Require strong column repetition before trying the text strategy.
        starts = {}
        for b in blocks:
            for line in b["lines"]:
                if len(line["text"]) <= 30:
                    key = round(line["bbox"][0] / 3)
                    starts.setdefault(key, set()).add(round(line["bbox"][1] / 3))
        if sum(len(ys) >= 4 for ys in starts.values()) >= 3:
            found = page.find_tables(strategy="text", min_words_vertical=4).tables

    tables = []
    for candidate in found:
        if candidate.row_count < 2 or not 2 <= candidate.col_count <= 32:
            continue
        rows = [[clean(cell) if cell is not None else None for cell in row]
                for row in candidate.extract()]
        filled_rows = [row for row in rows if sum(bool(cell) for cell in row) >= 2]
        if len(filled_rows) < 2:
            continue
        if sum(bool(cell) for row in rows for cell in row) < 0.28 * sum(map(len, rows)):
            continue
        # Consecutive borderless text lines often generate empty spacer rows.
        tables.append({
            "bbox": box(candidate.bbox),
            "columns": candidate.col_count,
            "rows": [row for row in rows if any(row)],
            "row_bounds": [box(row.bbox) for row, cells in zip(candidate.rows, rows) if any(cells)],
            "merged_or_missing_cells": any(cell is None for row in rows for cell in row),
        })
    return tables, drawings


def markdown_table(table: dict, number: int) -> list[str]:
    def row_md(row):
        return "| " + " | ".join(
            (cell or "").replace("|", r"\|").replace("\n", "<br>") for cell in row
        ) + " |"

    heading = (
        f"#### Detected table {number} · page bounds "
        f"`{', '.join(map(str, table['bbox']))}` pt"
    )
    header = row_md([f"Column {i}" for i in range(1, table["columns"] + 1)])
    divider = "| " + " | ".join(["---"] * table["columns"]) + " |"
    segments = []
    body = [heading, "", header, divider]
    size = sum(map(len, body))
    for row in table["rows"]:
        line = row_md(row)
        if size + len(line) > SECTION_LIMIT and len(body) > 4:
            segments.append("\n".join(body))
            body = [heading + " (continued)", "", header, divider]
            size = sum(map(len, body))
        body.append(line)
        size += len(line) + 1
    return segments + ["\n".join(body)]


def raster(page: pdf.Page, dpi: int, max_side: int = 3200) -> pdf.Pixmap:
    scale = min(dpi / 72, max_side / max(page.rect.width, page.rect.height))
    return page.get_pixmap(matrix=pdf.Matrix(scale, scale), colorspace=pdf.csGRAY, alpha=False)


def scan_with_text_overlay(page: pdf.Page) -> bool:
    area = page.rect.get_area()
    return bool(area and any(
        pdf.Rect(info["bbox"]).get_area() > area * 0.45 for info in page.get_image_info()
    ))


def ocr_pdf_page(page: pdf.Page, language: str, sparse_diagram: bool) -> str:
    image = raster(page, 240, max_side=4600)
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", language, "--psm",
         "11" if sparse_diagram else "3"],
        input=image.tobytes("png"), capture_output=True, timeout=40,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[-300:])
    return clean(result.stdout.decode("utf-8", "replace"))


def page_content(
    page: pdf.Page, role: str, source: str, destination: Path, ocr: str | None,
    semantic: str = "",
) -> tuple[list[str], dict]:
    number = page.number + 1
    diagram = role == "schematic"
    blocks = reading_order(text_blocks(page), page.rect.width)
    raw_text = "\n".join(line["text"] for block in blocks for line in block["lines"])
    native_count = usable_count(raw_text)
    suspect = encoding_suspect(raw_text)
    images = len(page.get_images())
    scan = bool(images and len(raw_text) < 200 and scan_with_text_overlay(page))
    details = {
        "page": number,
        "width_pt": round(page.rect.width, 1),
        "height_pt": round(page.rect.height, 1),
        "rotation": page.rotation,
        "native_chars": len(raw_text),
        "image_resources": images,
        "scan_candidate": scan,
        "tables": [],
        "warnings": [],
    }
    sections = [source_link(source, f"Original PDF page {number}", number)]
    if diagram:
        sections.append("Schematic labels appear below. Wire connectivity and symbol geometry are in the page drawing.")
    elif images:
        sections.append(f"Embedded image resources on this page: {images}. The original page preserves their visual content.")

    tables = []
    if native_count >= 35 and not diagram:
        try:
            tables, drawings = tables_on(page, blocks)
            details["vector_paths"] = len(drawings)
        except Exception as exc:
            details["warnings"].append(f"Table analysis: {type(exc).__name__}: {exc}")
    if suspect:
        details["warnings"].append("Native text contains undecoded glyphs.")
    needs_ocr = native_count < (100 if diagram else 35) or suspect or scan
    if needs_ocr:
        if ocr and (native_count or images or page.get_cdrawings()):
            try:
                details["ocr_text"] = ocr_pdf_page(page, ocr, diagram)
            except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
                details["warnings"].append(f"OCR: {type(exc).__name__}: {exc}")
        if native_count < 35 and usable_count(details.get("ocr_text", "")) < 20:
            details["warnings"].append("Sparse text. Use the original page or preview to inspect visual evidence.")
    if diagram or needs_ocr:
        try:
            filename = f"page-{number:04d}.jpg"
            raster(page, 160, 2400).save(destination / filename, jpg_quality=82)
            details["preview"] = filename
            sections.append(f"![Visual evidence for PDF page {number}]({filename})")
        except (RuntimeError, ValueError) as exc:
            details["warnings"].append(f"Preview: {type(exc).__name__}: {exc}")

    if semantic:
        sections.extend(("### Page transcription", semantic))
        details["semantic_chars"] = len(semantic)
    # Keep an independently extracted transcript whenever the semantic parser
    # may have omitted text. Check both technical tokens and numeric values.
    native_tokens = set(re.findall(r"[\wµΩ]+", raw_text.casefold()))
    semantic_tokens = set(re.findall(r"[\wµΩ]+", semantic.casefold()))
    missing = native_tokens - semantic_tokens
    details["native_tokens_missing_from_semantic"] = len(missing) if semantic else None
    if raw_text and (not semantic or len(missing) >= 3 or any(token.isdigit() for token in missing)):
        sections.append("### Native page text" if not semantic else "### Independent native text · additional source evidence")
        if diagram:
            sections.append("  \n".join(line for line in raw_text.splitlines()))
        else:
            sections.extend(markdown_text(blocks))
    if details.get("ocr_text"):
        sections.extend(("### OCR transcription · verify ambiguous symbols against the page",
                         details["ocr_text"]))
        details["ocr_chars"] = len(details["ocr_text"])
    if tables:
        sections.append("### Detected table structure · compare with page text and source")
        for index, table in enumerate(tables, 1):
            sections.extend(markdown_table(table, index))
    details["tables"] = [
        {**{key: value for key, value in table.items() if key != "rows"},
         "row_count": len(table["rows"])} for table in tables
    ]
    details["figure_labels"] = [line.strip() for line in raw_text.splitlines()
                                if FIGURE_LABEL.match(line)]
    try:
        links = [link["uri"] for link in page.get_links() if link.get("uri")]
        if links:
            details["links"] = list(dict.fromkeys(clean(link) for link in links))
            sections.append("### PDF hyperlinks\n\n" + "\n".join(
                f"- `{clean(link).replace(chr(96), chr(39))}`" for link in details["links"]
            ))
        annotations = [clean(annot.info.get("content", "")) for annot in page.annots() or ()]
        annotations = [annotation for annotation in annotations if annotation]
        if annotations:
            sections.extend(["### PDF annotations", *annotations])
    except Exception as exc:
        details["warnings"].append(f"Annotation analysis: {type(exc).__name__}: {exc}")
    if details["warnings"]:
        sections.append("### Extraction notes\n\n" + "\n".join(
            f"- {clean(message)}" for message in details["warnings"]
        ))
    details.pop("ocr_text", None)
    return sections, details


def split_oversized(section: str) -> list[str]:
    pieces = []
    while len(section) > SECTION_LIMIT:
        stop = section.rfind("\n", 0, SECTION_LIMIT)
        if stop < SECTION_LIMIT // 3:
            stop = section.rfind(" ", 0, SECTION_LIMIT)
        if stop < SECTION_LIMIT // 3:
            stop = SECTION_LIMIT
        pieces.append(section[:stop].rstrip())
        section = section[stop:].lstrip("\n ")
    return pieces + [section]


def chunks_of(sections: list[str]) -> list[str]:
    """Pack blocks and table segments into bounded, page-addressable passages."""
    chunks = []
    pending = []
    size = 0
    for section in sections:
        if not section:
            continue
        pieces = [section] if section.startswith("#### Detected table") else split_oversized(section)
        for piece in pieces:
            if pending and size + len(piece) + 2 > SECTION_LIMIT:
                chunks.append("\n\n".join(pending))
                pending, size = [], 0
            pending.append(piece)
            size += len(piece) + 2
    if pending:
        chunks.append("\n\n".join(pending))
    return chunks
