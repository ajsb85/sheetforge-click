"""Check exact board scoping and retrieval without external collections."""

import gzip
import json

import pytest

from sheetforge_click.driver_evidence import BoardEvidence


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_indexed_evidence_keeps_provenance_and_holds_out_example(tmp_path):
    write_jsonl(tmp_path / "boards.jsonl", [
        {"board": "sample", "title": "Sample Click", "path": "boards/sample.md"},
    ])
    (tmp_path / "boards").mkdir()
    (tmp_path / "boards/sample.md").write_text(
        "# Sample Click\n\n## Saved product documentation\n\n"
        "### Pinout diagram\n\n| ADR | GND |\n\n"
        "### <br> Software Support\n\nSample driver exposes target_driver_write().\n"
        "```c\ntarget_driver_reset();\n```\n"
        "## Saved product documentation\n\n### Electrical characteristics\n\n"
        "Logic level: 3.3 V.\n\n[Chip reference](https://example.com/datasheet.pdf)\n",
        encoding="utf-8",
    )
    write_jsonl(tmp_path / "documents.jsonl", [
        {"id": "chip", "title": "Sample IC", "status": "ok", "role": "reference",
         "sources": [{"board": "sample", "role": "datasheet"}],
         "markdown": "documents/chip/document.md", "chunks": "documents/chip/chunks.jsonl"},
        {"id": "other", "status": "ok", "role": "datasheet",
         "sources": [{"board": "sample-2", "role": "datasheet"}],
         "markdown": "outside.md", "chunks": "../outside.jsonl"},
    ])
    write_jsonl(tmp_path / "documents/chip/chunks.jsonl", [
        {"id": "chip:p5:1", "document_id": "chip", "title": "Sample IC", "page": 5, "part": 1,
         "text": "[Original PDF page 5](/private/path.pdf#page=5)\n\n"
                 "The I²C 7-bit address is 0x2E. Issue a repeated Start."},
    ])

    evidence = BoardEvidence(tmp_path, "sample")
    assert "target_driver" not in evidence.profile
    assert "Pinout diagram" in evidence.profile
    assert "3.3 V" in evidence.profile
    assert "example.com" not in evidence.profile
    assert len(evidence.documents) == 1

    passage, = evidence.retrieve("I2C repeated Start", roles=("datasheet",))
    assert "0x2E" in passage.context()
    assert "/private/path.pdf" not in passage.context()
    assert evidence.citations[passage.id] == {
        "title": "Sample IC", "role": "datasheet", "artifact": "documents/chip/document.md",
        "record_id": "chip:p5:1", "page": 5, "document_id": "chip",
    }
    assert evidence.context("I2C", roles=("datasheet",), max_chars=30) == ""
    with pytest.raises(ValueError, match="No board profile"):
        BoardEvidence(tmp_path, "sam")


def test_packed_evidence_loads_board_and_exact_pages(tmp_path):
    path = tmp_path / "retrieval.jsonl.gz"
    records = [
        {"id": "board:sample:1", "kind": "board", "board_slugs": ["sample"], "title": "Sample Click",
         "text": "# Sample\n\nInput voltage 3.3 V.", "artifact": "boards/sample.md"},
        {"id": "chip:p9:1", "kind": "pdf_page", "board_slugs": ["sample"], "title": "Sample IC",
         "text": "Register CMD is 0x08.", "artifact": "documents/chip/document.md",
         "role": "datasheet", "document_id": "chip", "page": 9, "part": 1},
        {"id": "chip:layout:p9:1", "kind": "pdf_layout", "board_slugs": ["sample"], "title": "Sample IC",
         "text": "    Write    CMD    0x08", "artifact": "documents/chip/layout.md",
         "role": "datasheet", "document_id": "chip", "page": 9, "part": 1},
        {"id": "other:p7:1", "kind": "pdf_page", "board_slugs": ["sample-2"], "title": "Unrelated",
         "text": "Register CMD is 0xAA.", "artifact": "outside.md",
         "role": "datasheet", "document_id": "other", "page": 7, "part": 1},
    ]
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")

    evidence = BoardEvidence(path, "sample")
    selected = evidence.retrieve("register CMD write 0x08", roles=("datasheet",))
    assert {passage.page for passage in selected} == {9}
    assert {passage.kind for passage in selected} == {"pdf_page", "pdf_layout"}
    assert len({passage.id for passage in selected}) == 2
    assert len(evidence.documents) == 1
    assert "0xAA" not in evidence.context("CMD")
    assert "3.3 V" in evidence.profile
