"""Build a resumable, source-linked corpus from local MikroE Click collections."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import gzip
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from importlib.metadata import version
from pathlib import Path

import liteparse
import pymupdf

from .click_board import write_boards
from .click_inventory import Document, discover
from .click_pdf import EXTRACT_VERSION, chunks_of, clean, page_content, scan_with_text_overlay, source_link
from .click_schematic import extract_schematic, schematic_sections

FORMAT_VERSION = 1
DEFAULT_OUTPUT = Path("out-click")
BATCH_LOG_INTERVAL = 100
PIPE_TABLE = re.compile(r"(?m)^\s*\|\s*:?-{2,}")
PDFIUM_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(img_p(\d+)_(\d+)\.png\)")
LAYOUT_LINK = "[Independent column-aligned text from Poppler](layout.md)"
_PARSER = None


def json_line(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def write_records(path: Path, records) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            for record in records:
                f.write(json_line(record))
            f.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def write_summary(path: Path, value: dict) -> None:
    write_records(path, [value])


def write_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(text)
            f.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def title_for(name: str) -> str:
    return clean(re.sub(r"(?i)\.pdf$", "", name)) or "PDF document"


def corpus_path(doc_id: str) -> Path:
    return Path("documents", doc_id[:2], doc_id)


def semantic_pages(input_file) -> tuple[dict[int, str], str | None]:
    global _PARSER
    try:
        if _PARSER is None:
            _PARSER = liteparse.LiteParse(
                output_format="markdown", max_pages=100000, ocr_enabled=False,
                keep_headers_footers=True, preserve_very_small_text=True,
                continue_on_page_error=True, num_workers=1, quiet=True,
            )
        result = _PARSER.parse(input_file)
        return {p.page_num: clean(p.markdown or "") for p in result.pages}, None
    except Exception as exc:
        return {}, f"LiteParse: {type(exc).__name__}: {exc}"


def poppler_pages(input_file) -> list[str]:
    target = "-" if isinstance(input_file, bytes) else str(input_file)
    result = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", target, "-"],
        input=input_file if isinstance(input_file, bytes) else None,
        capture_output=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[-800:])
    pages = result.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return [page.strip("\n") for page in pages]


def open_pdf(input_file):
    if isinstance(input_file, bytes):
        doc = pymupdf.open(stream=input_file, filetype="pdf")
    else:
        doc = pymupdf.open(input_file)
    if not doc.is_pdf or doc.needs_pass or doc.page_count < 1:
        doc.close()
        raise ValueError("PDF type, password, or page count could not be verified.")
    return doc


def cached_record(destination: Path, signature: dict, document: Document) -> dict | None:
    try:
        record = json.loads((destination / "metadata.json").read_text(encoding="utf-8"))
        if record["signature"] != signature or record["page_failures"]:
            return None
        artifacts = record["artifacts"]
        if record["role"] == "schematic" and "schematic.json" not in artifacts:
            if extract_schematic(record["outline"]):
                return None
        if not all((destination / name).stat().st_size == size for name, size in artifacts.items()):
            return None
        if signature["ocr"] and needs_scan_upgrade(record, document):
            return None
        return record
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def needs_scan_upgrade(record: dict, document: Document) -> bool:
    """Revisit older cached documents only when an improved OCR route applies."""
    old_pages = [page for page in record["pages"]
                 if "scan_candidate" not in page and "ocr_chars" not in page]
    if record["role"] == "schematic" and any(35 <= page["native_chars"] < 180 for page in old_pages):
        return True
    candidates = [page["page"] for page in old_pages
                  if page.get("image_resources") and 35 <= page["native_chars"] < 200]
    if not candidates:
        return False
    input_file = document.read_bytes() if document.source.member else document.source.path
    try:
        with open_pdf(input_file) as pdf:
            return any(scan_with_text_overlay(pdf[number - 1]) for number in candidates)
    except (RuntimeError, ValueError):
        return False


def pdf_provenance(source) -> dict:
    return source.to_dict()


def ocr_setting(requested: str) -> str | None:
    if requested == "off":
        return None
    try:
        proc = subprocess.run(["tesseract", "--list-langs"], text=True, capture_output=True,
                              check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Tesseract is required for OCR. Install it or use --ocr off.") from exc
    available = set(proc.stdout.splitlines()[1:])
    missing = set(requested.split("+")) - available
    if missing:
        raise ValueError(f"Tesseract language data missing: {', '.join(sorted(missing))}")
    return requested


def add_layout_evidence(destination: Path, record: dict, input_file, href: str) -> None:
    """Keep an independent rendition of the native columns, including merged headers."""
    if record["role"] == "schematic" or record.get("layout_status") == "ok":
        return
    try:
        pages = poppler_pages(input_file)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        record["layout_status"] = "unavailable"
        record["layout_note"] = clean(f"{type(exc).__name__}: {exc}")
    else:
        sections = [
            f"# Column-aligned source text · {record['title']}",
            "The spaces reflect the extracted source positions. Confirm cell boundaries on the PDF.",
        ]
        for number, page in enumerate(pages, 1):
            body = "\n".join("    " + line for line in page.splitlines())
            sections.extend((f"## Page {number}", source_link(href, f"Original PDF page {number}", number), body))
        layout = destination / "layout.md"
        write_text(layout, "\n\n".join(sections) + "\n")
        document = destination / "document.md"
        heading, _, rest = document.read_text(encoding="utf-8").partition("\n\n")
        if not rest.startswith(LAYOUT_LINK):
            write_text(document, f"{heading}\n\n{LAYOUT_LINK}\n\n{rest}")
        record["artifacts"].update({"layout.md": layout.stat().st_size,
                                    "document.md": document.stat().st_size})
        record["layout_status"] = "ok"
        record["layout_pages"] = len(pages)
    write_summary(destination / "metadata.json", record)


def link_source_images(markdown: str, href: str) -> str:
    return PDFIUM_IMAGE.sub(
        lambda match: source_link(
            href, match[1] or f"Embedded image {match[3]} on PDF page {match[2]}", int(match[2])
        ),
        markdown,
    )


def relink_cached_images(destination: Path, record: dict, href: str) -> None:
    """Replace transient converter image names with original PDF page links."""
    if record.get("source_images_linked"):
        return
    markdown = destination / "document.md"
    text = markdown.read_text(encoding="utf-8")
    linked = link_source_images(text, href)
    if linked != text:
        chunks = destination / "chunks.jsonl"
        with chunks.open(encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        for row in rows:
            row["text"] = link_source_images(row["text"], href)
        write_records(chunks, rows)
        write_text(markdown, linked)
        record["artifacts"].update({"document.md": markdown.stat().st_size,
                                    "chunks.jsonl": chunks.stat().st_size})
    record["source_images_linked"] = True
    write_summary(destination / "metadata.json", record)


def write_pdf(output: Path, document: Document, ocr: str | None) -> tuple[dict, bool]:
    pymupdf.TOOLS.mupdf_display_errors(False)
    source = document.source
    doc_id = document.sha256
    target = output / corpus_path(doc_id)
    name = source.filename
    signature = {
        "version": EXTRACT_VERSION,
        "format": FORMAT_VERSION,
        "pymupdf": pymupdf.VersionBind,
        "liteparse": version("liteparse"),
        "ocr": ocr,
        "source": pdf_provenance(source),
    }
    href = "source.pdf" if source.member else os.path.relpath(source.path, target)
    if result := cached_record(target, signature, document):
        if result["role"] != "schematic" and result.get("layout_status") != "ok":
            input_file = target / "source.pdf" if source.member else source.path
            add_layout_evidence(target, result, input_file, href)
        relink_cached_images(target, result, href)
        return result, True

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=target.parent) as tmp:
        stage = Path(tmp)
        input_file = document.read_bytes() if source.member else source.path
        if source.member:
            (stage / "source.pdf").write_bytes(input_file)
        result = extract_pdf(stage, doc_id, input_file, source, name, href, signature, ocr)
        add_layout_evidence(stage, result, input_file, href)
        target.mkdir(exist_ok=True)
        # Metadata is the completion marker, published after the payload.
        (target / "metadata.json").unlink(missing_ok=True)
        for file in stage.iterdir():
            if file.name != "metadata.json":
                file.replace(target / file.name)
        (stage / "metadata.json").replace(target / "metadata.json")
        return result, False


def extract_pdf(
    stage: Path, doc_id: str, input_file, source, name: str, href: str,
    signature: dict, ocr: str | None,
) -> dict:
    warnings = []
    outline = []
    metadata = {}
    semantic = {}
    if source.role != "schematic":
        semantic, warning = semantic_pages(input_file)
        semantic = {page: link_source_images(text, href) for page, text in semantic.items()}
        if warning:
            warnings.append(warning)
    try:
        doc = open_pdf(input_file)
    except Exception as exc:
        warnings.append(f"PyMuPDF: {type(exc).__name__}: {exc}")
        try:
            backup = poppler_pages(input_file)
        except Exception as err:
            warnings.append(f"Poppler: {type(err).__name__}: {err}")
            backup = []
        if not any(backup) and not any(semantic.values()):
            raise RuntimeError(" | ".join(warnings)) from exc
        doc = None
        page_count = max(len(backup), max(semantic, default=0))
    else:
        backup = []
        page_count = doc.page_count
        try:
            outline = [[level, clean(label), number] for level, label, number in doc.get_toc()]
            metadata = {k: clean(v) for k, v in doc.metadata.items() if v}
        except Exception as exc:
            warnings.append(f"Outline or metadata: {type(exc).__name__}: {exc}")

    title = title_for(name)
    pages = []
    failed_pages = []
    chunk_count = 0
    header = [
        f"# {title}", "",
        f"- Document role: {source.role.replace('_', ' ')}",
        f"- Source board: `{source.board}` ({source.collection})",
        f"- Original: {source_link(href, 'source PDF')}",
        f"- SHA-256: `{doc_id}`",
        f"- Pages: {page_count}", "",
    ]
    if source.member:
        header.extend((f"Archive member: `{source.member}`", ""))
    if metadata:
        header.extend(("## PDF metadata", ""))
        for key, value in sorted(metadata.items()):
            if key not in {"format", "encryption"}:
                header.append(f"- {key}: {clean(str(value)).replace(chr(10), ' ')}")
        header.append("")
    if outline:
        header.extend(("## PDF bookmarks", ""))
        for level, label, number in outline:
            text = clean(label).replace("[", r"\[").replace("]", r"\]").replace("\n", " ")
            jump = f"[{text}](#page-{number})" if 1 <= number <= page_count else text
            header.append(f"{'  ' * min(max(0, level - 1), 12)}- {jump}")
        header.append("")
    if warnings:
        header.extend(("## Document extraction notes", "", *[f"- {clean(note)}" for note in warnings], ""))

    schematic = extract_schematic(outline) if source.role == "schematic" else None
    if schematic:
        header.extend((source_link("schematic.json", "Schematic connectivity from PDF bookmarks"), ""))
        write_summary(stage / "schematic.json", schematic)
    with contextlib.ExitStack() as stack:
        if doc is not None:
            stack.enter_context(doc)
        md = stack.enter_context((stage / "document.md").open("w", encoding="utf-8"))
        index = stack.enter_context((stage / "chunks.jsonl").open("w", encoding="utf-8"))
        md.write("\n".join(header) + "\n")
        for number in range(1, page_count + 1):
            transcription = semantic.get(number, "")
            if doc is not None:
                try:
                    sections, details = page_content(doc[number - 1], source.role, href, stage, ocr, transcription)
                except Exception as exc:
                    if not backup:
                        try:
                            backup = poppler_pages(input_file)
                        except Exception:
                            backup = [""] * page_count
                    note = f"Page extraction: {type(exc).__name__}: {exc}"
                    sections, details = fallback_page(number, href, transcription, backup, note)
            else:
                sections, details = fallback_page(number, href, transcription, backup, "Fallback PDF engine")
            if details.get("failed"):
                failed_pages.append(number)
            sections.extend(schematic_sections(schematic, number))
            md.write(f"## Page {number}\n\n")
            for section in sections:
                if section:
                    md.write(section + "\n\n")
            passages = chunks_of(sections)
            details["chunks"] = len(passages)
            pages.append(details)
            for part, passage in enumerate(passages, 1):
                index.write(json_line({
                    "id": f"{doc_id}:p{number}:{part}",
                    "document_id": doc_id,
                    "title": title,
                    "role": source.role,
                    "source": str(source.path),
                    "archive_member": source.member,
                    "page": number,
                    "part": part,
                    "parts_on_page": len(passages),
                    "preview": details.get("preview"),
                    "text": f"# {title}\n\n## Page {number}\n\n{passage}",
                }))
                chunk_count += 1

    artifacts = {p.name: p.stat().st_size for p in stage.iterdir()}
    record = {
        "id": doc_id, "title": title, "role": source.role, "signature": signature,
        "source": pdf_provenance(source), "page_count": page_count, "chunk_count": chunk_count,
        "metadata": metadata, "outline": outline, "warnings": warnings, "pages": pages,
        "page_failures": failed_pages, "artifacts": artifacts, "source_images_linked": True,
    }
    write_summary(stage / "metadata.json", record)
    return record


def fallback_page(
    number: int, href: str, transcription: str, backup: list[str], note: str,
) -> tuple[list[str], dict]:
    native = backup[number - 1] if number <= len(backup) else ""
    sections = [source_link(href, f"Original PDF page {number}", number)]
    if transcription:
        sections.extend(("### Page transcription", transcription))
    if native:
        sections.extend(("### Poppler layout text", "\n".join("    " + line for line in native.splitlines())))
    sections.extend(("### Extraction notes", note))
    return sections, {
        "page": number, "native_chars": len(native), "semantic_chars": len(transcription),
        "warnings": [note], "tables": [], "failed": not (native or transcription),
    }


def parse_collection(value: str) -> tuple[str, Path]:
    label, sep, path = value.partition("=")
    if not sep or not re.fullmatch(r"[\w.-]+", label) or not path:
        raise argparse.ArgumentTypeError("Use LABEL=/path/to/products.")
    return label, Path(path).expanduser().resolve()


def pack_retrieval(output: Path, catalog: dict, boards: list[dict]) -> int:
    """Create a single streaming JSONL corpus alongside the browsable files."""
    with tempfile.NamedTemporaryFile(dir=output, delete=False) as staging_file:
        staging = Path(staging_file.name)
        count = 0
        try:
            with gzip.GzipFile(fileobj=staging_file, mode="wb", filename="", mtime=0, compresslevel=3) as f:
                for board in sorted(boards, key=lambda record: record["board"]):
                    slug = board["board"]
                    markdown = (output / board["path"]).read_text(encoding="utf-8")
                    for part, text in enumerate(chunks_of(markdown.split("\n\n")), 1):
                        item = {
                            "id": f"board:{slug}:{part}", "kind": "board", "board_slugs": [slug],
                            "title": board["title"], "artifact": board["path"], "text": text,
                        }
                        f.write(json_line(item).encode("utf-8"))
                        count += 1
                for key in sorted(catalog):
                    record = catalog[key]
                    if "chunks" not in record:
                        continue
                    slugs = sorted({source["board"] for source in record["sources"]})
                    item_context = {"kind": "pdf_page", "board_slugs": slugs,
                                    "artifact": record["markdown"], "metadata": record["metadata"]}
                    structured_pages = set()
                    with (output / record["chunks"]).open(encoding="utf-8") as chunks:
                        for line in chunks:
                            item = {**json.loads(line), **item_context}
                            if PIPE_TABLE.search(item["text"]):
                                structured_pages.add(item["page"])
                            f.write(json_line(item).encode("utf-8"))
                            count += 1
                    if structured_pages and "layout" in record:
                        for item in layout_chunks(output, record, item_context, structured_pages):
                            f.write(json_line(item).encode("utf-8"))
                            count += 1
            staging_file.close()
            staging.replace(output / "retrieval.jsonl.gz")
        finally:
            staging.unlink(missing_ok=True)
    return count


def layout_chunks(output: Path, record: dict, item_context: dict, formatted_pages: set[int]):
    metadata = json.loads((output / record["metadata"]).read_text(encoding="utf-8"))
    pages = {page["page"] for page in metadata["pages"] if not page["tables"]} & formatted_pages
    if not pages:
        return
    markdown = (output / record["layout"]).read_text(encoding="utf-8")
    contents = re.split(r"(?m)^## Page (\d+)$", markdown)
    for page, body in zip(contents[1::2], contents[2::2]):
        number = int(page)
        if number not in pages or len(body.splitlines()) < 6:
            continue
        for part, text in enumerate(chunks_of([body.strip("\n")]), 1):
            yield {
                **item_context, "kind": "pdf_layout", "artifact": record["layout"],
                "document_id": record["id"], "role": record["role"], "title": record["title"],
                "source": metadata["source"]["path"], "archive_member": metadata["source"]["member"],
                "id": f"{record['id']}:layout:p{number}:{part}", "page": number, "part": part,
                "text": f"# {record['title']}\n\n## Page {number} · independent column-aligned text\n\n{text}",
            }


def write_landing(output: Path, summary: dict) -> None:
    example = " See [BATT Boost Click](boards/batt-boost-click.md) for an example." if (
        output / "boards/batt-boost-click.md"
    ).is_file() else ""
    write_text(output / "README.md", f"""# MikroE Click board corpus

This export contains {summary['boards']:,} board profiles and
{summary['unique_pdfs']:,} distinct source PDFs.

- Find your board in the [board index](boards.jsonl).{example}
- Pass [retrieval.jsonl.gz](retrieval.jsonl.gz) to a JSONL ingestion pipeline
  after decompressing it. Each PDF record includes the source page number.
- Use the [document index](documents.jsonl) for exact SHA-256 source matching,
  original copies and archive members, and Markdown and metadata paths.
- See [catalog.json](catalog.json) for coverage and [issues.jsonl](issues.jsonl)
  for discovery errors.

Open original PDF pages for visual geometry and to confirm ambiguous
OCR characters or table cells.
""")


def build(args: argparse.Namespace, output: Path, ocr: str | None) -> int:
    collections = args.collection
    print("Inventorying local collections and archive members", flush=True)
    inventory = discover(collections)
    boards = inventory.boards
    if args.board:
        missing = set(args.board) - boards.keys()
        if missing:
            raise ValueError(f"Unknown board slugs: {', '.join(sorted(missing))}")
        boards = {slug: boards[slug] for slug in args.board}
    documents = [doc for doc in inventory.documents.values()
                 if any(source.board in boards for source in doc.sources)]
    documents.sort(key=lambda doc: (-doc.size, doc.sha256))
    output.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(boards):,} boards and {len(documents):,} unique PDFs", flush=True)
    catalog = {}
    counts = Counter()
    # PDFium and MuPDF state stays local to each worker process.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=args.jobs, mp_context=context) as pool:
        tasks = {pool.submit(write_pdf, output, document, ocr): document for document in documents}
        for index, task in enumerate(as_completed(tasks), 1):
            document = tasks[task]
            counts["processed"] += 1
            prefix = corpus_path(document.sha256)
            entry = {
                "id": document.sha256,
                "bytes": document.size,
                "sources": [pdf_provenance(source) for source in document.sources],
            }
            try:
                record, cached = task.result()
            except Exception as exc:
                entry.update(status="error", error=f"{type(exc).__name__}: {exc}")
                counts["failed_documents"] += 1
                if counts["failed_documents"] <= 12:
                    print(f"PDF failed: {document.source.path}: {entry['error']}", file=sys.stderr, flush=True)
            else:
                entry.update(
                    title=record["title"], role=record["role"],
                    status="partial" if record["page_failures"] else "ok",
                    markdown=str(prefix / "document.md"),
                    chunks=str(prefix / "chunks.jsonl"), metadata=str(prefix / "metadata.json"),
                    pages=record["page_count"], chunk_count=record["chunk_count"],
                    page_failures=record["page_failures"],
                )
                if "schematic.json" in record["artifacts"]:
                    entry["schematic"] = str(prefix / "schematic.json")
                if "layout.md" in record["artifacts"]:
                    entry["layout"] = str(prefix / "layout.md")
                counts.update({
                    "cached": int(cached),
                    "pages": record["page_count"],
                    "chunks": record["chunk_count"],
                    "tables": sum(len(page["tables"]) for page in record["pages"]),
                    "ocr_pages": sum(bool(page.get("ocr_chars")) for page in record["pages"]),
                    "preview_pages": sum(bool(page.get("preview")) for page in record["pages"]),
                    "pages_with_notes": sum(bool(page.get("warnings")) for page in record["pages"]),
                    "partial_documents": int(bool(record["page_failures"])),
                    "page_failures": len(record["page_failures"]),
                    "schematic_models": int("schematic" in entry),
                    "layout_documents": int("layout" in entry),
                    "source_images_linked": int(record.get("source_images_linked", False)),
                })
            catalog[document.sha256] = entry
            if index % BATCH_LOG_INTERVAL == 0 or index == len(documents):
                print(f"{index:,}/{len(documents):,} PDFs · {counts['pages']:,} pages · "
                      f"{counts['cached']:,} cache hits · {counts['failed_documents']:,} failed", flush=True)

    board_documents = defaultdict(set)
    for record in catalog.values():
        for source in record["sources"]:
            if "markdown" in record and source["board"] in boards:
                raw_name = source["member_chain"][-1] if source["member_chain"] else source["relative_path"]
                name = Path(raw_name.replace("\\", "/")).name
                board_documents[source["board"]].add((name, source["role"], record["markdown"]))
    print("Writing board profiles and corpus indexes", flush=True)
    board_records = write_boards(output, boards, board_documents)
    print("Packing the retrieval corpus", flush=True)
    retrieval_records = pack_retrieval(output, catalog, board_records)
    issue_records = [issue if isinstance(issue, dict) else vars(issue) for issue in inventory.issues]
    write_records(output / "documents.jsonl", (catalog[k] for k in sorted(catalog)))
    write_records(output / "boards.jsonl", board_records)
    write_records(output / "issues.jsonl", issue_records)
    summary = {
        "format": FORMAT_VERSION,
        "collections": {label: str(path) for label, path in collections},
        "selected_boards": sorted(set(args.board or [])),
        "boards": len(boards),
        "unique_pdfs": len(documents),
        "source_occurrences": sum(len(entry["sources"]) for entry in catalog.values()),
        "by_role": dict(Counter(entry.get("role", "error") for entry in catalog.values())),
        "inventory_issues": len(issue_records),
        "retrieval": "retrieval.jsonl.gz",
        "retrieval_records": retrieval_records,
        **{key: counts[key] for key in (
            "processed", "pages", "chunks", "tables", "ocr_pages", "preview_pages",
            "pages_with_notes", "failed_documents", "partial_documents", "page_failures", "cached",
            "schematic_models", "layout_documents", "source_images_linked",
        )},
    }
    write_landing(output, summary)
    write_summary(output / "catalog.json", summary)
    print(json.dumps(summary, indent=2), flush=True)
    return int(bool(counts["failed_documents"] or counts["page_failures"] or issue_records))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--collection", type=parse_collection, action="append", required=True,
                        help="Source collection as LABEL=/path/to/products. Repeat to combine roots.")
    parser.add_argument("--board", action="append", help="Limit export to this board slug. Repeat to select more.")
    parser.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--ocr", default="eng", help="Tesseract language(s), such as eng+deu, or off.")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive.")
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    pymupdf.TOOLS.mupdf_display_errors(False)
    output = args.output.expanduser().resolve()
    try:
        ocr = ocr_setting(args.ocr)
        output.mkdir(parents=True, exist_ok=True)
        with (output / ".build.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            catalog_path = output / "catalog.json"
            if args.board and catalog_path.is_file():
                existing = json.loads(catalog_path.read_text(encoding="utf-8"))
                if existing.get("selected_boards") != sorted(set(args.board)):
                    raise ValueError("Use a separate output directory for this board selection.")
            for stage in (output / "documents").glob("[0-9a-f][0-9a-f]/.build-*"):
                if stage.is_dir() and not stage.is_symlink():
                    shutil.rmtree(stage)
            return build(args, output, ocr)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"sheetforge-click: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
