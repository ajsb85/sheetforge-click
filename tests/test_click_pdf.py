"""Regression coverage for page evidence in the Click board corpus."""

import pytest

pymupdf = pytest.importorskip("pymupdf")

from sheetforge_click.click_pdf import (  # noqa: E402
    SECTION_LIMIT,
    chunks_of,
    clean,
    clean,
    markdown_table,
    page_content,
    source_link,
    tables_on,
    text_blocks,
)


def test_source_link_escapes_path_and_adds_page_fragment():
    assert source_link("../Spec (rev #2) α.pdf", "Original PDF", 3) == (
        "[Original PDF](../Spec%20%28rev%20%232%29%20%CE%B1.pdf#page=3)"
    )


def test_unpaired_pdf_surrogates_are_replaced_without_losing_valid_symbols():
    assert clean(" V\ud835 and \ud835\udd2a, 3.3 µA ") == "V� and 𝔪, 3.3 µA"


def test_invalid_pdf_unicode_preserves_valid_characters_and_replaces_lone_surrogate():
    assert clean("µA \ud835\udc01 \ud835 CLK_MSR\x00") == "µA 𝐁 � CLK_MSR"


def test_ruled_specification_table_and_page_text(tmp_path):
    rows = [
        ["Parameter", "Condition", "Value"],
        ["Supply voltage", "Recommended", "3.3 V"],
        ["Supply current", "Standby", "15 µA"],
    ]
    with pymupdf.open() as document:
        page = document.new_page(width=500, height=350)
        page.insert_text((40, 45), "Electrical characteristics", fontsize=16)
        for x in (40, 180, 320, 460):
            page.draw_line((x, 65), (x, 155))
        for y in (65, 95, 125, 155):
            page.draw_line((40, y), (460, y))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                page.insert_text((46 + 140 * column_index, 85 + 30 * row_index), value)
        page.insert_text((40, 200), "Figure 1. Application circuit")

        tables, _ = tables_on(page, text_blocks(page))
        assert [table["rows"] for table in tables] == [rows]
        sections, details = page_content(
            page, "datasheet", "source.pdf", tmp_path, None, "Electrical characteristics"
        )

    assert "[Original PDF page 1](source.pdf#page=1)" in sections
    assert "Electrical characteristics" in sections
    assert any("3.3 V" in section for section in sections)
    assert details["native_tokens_missing_from_semantic"] > 0
    assert details["figure_labels"] == ["Figure 1. Application circuit"]


def test_image_only_page_routes_to_ocr(tmp_path, monkeypatch):
    def transcribe(page, language, sparse_diagram):
        assert page.number == 0
        assert language == "eng"
        assert sparse_diagram is False
        return "Maximum input voltage 5.5 V"

    monkeypatch.setattr("sheetforge_click.click_pdf.ocr_pdf_page", transcribe)
    with pymupdf.open() as document:
        page = document.new_page(width=500, height=350)
        image = pymupdf.Pixmap(pymupdf.csGRAY, 2, 2, bytes(range(4)), False)
        page.insert_image(page.rect, pixmap=image)
        sections, details = page_content(page, "datasheet", "scan.pdf", tmp_path, "eng")

    assert "Maximum input voltage 5.5 V" in "\n".join(sections)
    assert details["native_chars"] == 0
    assert (tmp_path / details["preview"]).exists()


def test_long_prose_and_tables_preserve_text_across_chunks():
    prose = "\n".join(f"Evidence line {i:04d}. " + "source text " * 8 for i in range(90))
    table = {
        "bbox": [40, 65, 460, 155],
        "columns": 2,
        "rows": [[f"Parameter {i:04d}", "Case A | Case B\n" + "nominal " * 12]
                 for i in range(110)],
    }
    passages = chunks_of([prose, *markdown_table(table, 1)])

    assert len(passages) > 3
    assert max(map(len, passages)) <= SECTION_LIMIT
    joined = "\n".join(passages)
    for i in range(90):
        assert joined.count(f"Evidence line {i:04d}.") == 1
    for i in range(110):
        assert joined.count(f"Parameter {i:04d}") == 1
    assert r"Case A \| Case B<br>" in joined
    table_passages = [chunk for chunk in passages if "#### Detected table" in chunk]
    assert len(table_passages) > 1
    assert all("| Column 1 | Column 2 |\n| --- | --- |" in chunk for chunk in table_passages)


def test_schematic_preserves_labels_and_preview(tmp_path):
    labels = ("VIN", "SCL", "SDA", "ADDR SEL", "0x2E", "0x2F")
    with pymupdf.open() as document:
        page = document.new_page()
        for i, label in enumerate(labels):
            page.insert_text((45, 45 + i * 20), label)
        sections, details = page_content(page, "schematic", "schematic.pdf", tmp_path, None)

    text = "\n".join(sections)
    assert all(label in text for label in labels)
    assert details["tables"] == []
    assert (tmp_path / details["preview"]).stat().st_size > 0
