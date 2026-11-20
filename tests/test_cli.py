"""Exercise the installed CLI with generated product captures and PDFs."""

import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf


def make_pdf(lines, bookmarks=None):
    with pymupdf.open() as document:
        page = document.new_page()
        for offset, line in enumerate(lines):
            page.insert_text((54, 72 + 23 * offset), line, fontsize=12)
        if bookmarks:
            document.set_toc(bookmarks)
        return document.tobytes()


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run_corpus(primary, mirror, output):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sheetforge_click.click_corpus",
            "--collection",
            f"primary={primary}",
            "--collection",
            f"mirror={mirror}",
            "--output",
            str(output),
            "--jobs",
            "1",
            "--ocr",
            "off",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((output / "catalog.json").read_text(encoding="utf-8"))


def test_cli_builds_and_reuses_a_portable_corpus(tmp_path: Path):
    primary = tmp_path / "primary products"
    mirror = tmp_path / "mirror products"
    power = primary / "voltage-click"
    panel = mirror / "readout-click"
    power.mkdir(parents=True)
    panel.mkdir(parents=True)
    (power / "index.html").write_text(
        """<!doctype html>
        <title>Voltage Click</title>
        <main><h1>Voltage Click</h1>
        <div id="info-content">
          <h2>Electrical characteristics</h2>
          <table><tr><th>Supply</th><th>Typical</th></tr>
          <tr><td>VDD</td><td>3.3 V</td></tr></table>
        </div></main>""",
        encoding="utf-8",
    )
    (panel / "index.html").write_text(
        "<main><h1>Readout Click</h1></main>",
        encoding="utf-8",
    )

    datasheet = make_pdf(
        ["Voltage reference data sheet", "Recommended supply voltage is 3.3 V."]
    )
    (power / "reference datasheet.pdf").write_bytes(datasheet)
    (panel / "vendor datasheet.pdf").write_bytes(datasheet)

    schematic = make_pdf(
        ["Voltage Click schematic", "Power input VBUS", "Component U1 pin 1"],
        bookmarks=[
            [1, "Schematic (All Documents)", 1],
            [2, "Power.SchDoc(Power)", 1],
            [3, "Components", 1],
            [4, "U1", 1],
            [5, "U1-1", 1],
            [3, "Nets", 1],
            [4, "VBUS", 1],
            [5, "Pins", 1],
            [6, "U1-1", 1],
        ],
    )
    member = "drawings/assembly schematic.pdf"
    archive = power / "board assets.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        bundle.writestr(member, schematic)
        bundle.writestr("cad/example.step", "Synthetic CAD placeholder")

    output = tmp_path / "generated corpus"
    summary = run_corpus(primary, mirror, output)
    assert summary["boards"] == 2
    assert summary["unique_pdfs"] == 2
    assert summary["source_occurrences"] == 3
    assert summary["pages"] == 2
    assert summary["failed_documents"] == summary["page_failures"] == 0
    assert summary["cached"] == 0

    documents = {
        record["id"]: record for record in read_jsonl(output / "documents.jsonl")
    }
    assert all(record["status"] == "ok" for record in documents.values())
    datasheet_id = hashlib.sha256(datasheet).hexdigest()
    schematic_id = hashlib.sha256(schematic).hexdigest()
    shared = documents[datasheet_id]
    assert {item["collection"] for item in shared["sources"]} == {"primary", "mirror"}
    assert "3.3 V" in (output / shared["markdown"]).read_text(encoding="utf-8")

    archived = documents[schematic_id]
    source = archived["sources"][0]
    assert source["path"] == str(archive)
    assert source["member_chain"] == [member]
    document_path = output / archived["markdown"]
    assert (document_path.parent / "source.pdf").read_bytes() == schematic
    assert "source.pdf#page=1" in document_path.read_text(encoding="utf-8")
    model = json.loads((output / archived["schematic"]).read_text(encoding="utf-8"))
    net = model["sheets"][0]["nets"][0]
    assert net["label"] == "VBUS"
    assert net["pins"][0]["label"] == "U1-1"

    board = (output / "boards" / "voltage-click.md").read_text(encoding="utf-8")
    assert "| VDD | 3.3 V |" in board
    assert member in board
    assert "cad/example.step" in board

    retrieval = output / "retrieval.jsonl.gz"
    with gzip.open(retrieval, "rt", encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    assert len(records) == summary["retrieval_records"]
    assert len({item["id"] for item in records}) == len(records)
    assert all((output / item["artifact"]).is_file() for item in records)
    pdf_pages = [item for item in records if item["kind"] == "pdf_page"]
    assert {item["document_id"] for item in pdf_pages} == {datasheet_id, schematic_id}
    assert all(item["page"] == 1 for item in pdf_pages)
    assert all(
        item["board_slugs"] == ["readout-click", "voltage-click"]
        for item in pdf_pages
        if item["document_id"] == datasheet_id
    )

    retrieval_bytes = retrieval.read_bytes()
    second = run_corpus(primary, mirror, output)
    assert second["cached"] == second["unique_pdfs"] == 2
    assert retrieval.read_bytes() == retrieval_bytes
