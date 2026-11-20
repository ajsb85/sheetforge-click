"""Retrieve board-specific engineering evidence from a Sheetforge Click corpus."""

from __future__ import annotations

import gzip
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

ENGINEERING_ROLES = frozenset({"board", "datasheet", "schematic", "manual", "reference", "application_note"})
SKIP_BOARD_SECTIONS = re.compile(
    r"^#{2,6}\s+(?:<br\s*/?>\s*)?(?:pdf documents|local board assets|archive contents|"
    r"software(?: support| demos)?|example(?: code)?|library(?: description)?|"
    r"m[ií]krosdk(?:\b.*)?|resources|downloads|source notes)\s*:?\s*$",
    re.I,
)
HEADING = re.compile(r"^(#{1,6})\s+")
# Keep reference labels in the model context, with file paths in citations.
LINK = re.compile(r"!?\[([^]\n]*)\]\((?:<[^>\n]*>|[^)\n]*)\)")
RAW_URL = re.compile(r"(?:https?://|file://)\S+")
LAYOUT_PAGE = re.compile(r"(?m)^## Page (\d+)\s*$")
TOKENS = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
STOP_WORDS = frozenset(
    "a an and are as at be by for from in is it its of on or page that the "
    "this to was were will with source pdf datasheet original transcript section".split()
)
PART_LIMIT = 5800


@dataclass(frozen=True)
class Passage:
    id: str
    text: str
    role: str
    title: str
    page: int | None
    artifact: str
    record_id: str
    document_id: str | None = None
    kind: str = "pdf_page"

    def citation(self) -> dict:
        """Return the exact local evidence locator, without sending paths to MiMo."""
        result = {
            "title": self.title,
            "role": self.role,
            "artifact": self.artifact,
            "record_id": self.record_id,
        }
        if self.page is not None:
            result["page"] = self.page
        if self.document_id:
            result["document_id"] = self.document_id
        return result

    def context(self) -> str:
        locator = f" · page {self.page}" if self.page is not None else ""
        return f"[{self.id}] {self.role} · {self.title}{locator}\n{self.text}"


def _tokens(value: str) -> list[str]:
    normal = unicodedata.normalize("NFKC", value).casefold()
    return [token for token in TOKENS.findall(normal) if token not in STOP_WORDS]


def _prompt_text(value: str) -> str:
    def link_label(match):
        label = match[1]
        if label.startswith(("Original PDF page ", "Embedded image ")):
            return ""
        return label

    return RAW_URL.sub("", LINK.sub(link_label, value)).strip()


def _board_text(profile: str) -> str:
    """Keep hardware sections and withhold the target's example and API guide."""
    selected = []
    skipped_level = None
    fenced = None
    for line in profile.splitlines():
        marker = line.lstrip()
        fence = re.match(r"^(`{3,}|~{3,})", marker)
        if fence:
            delimiter = fence[1]
            if fenced is None:
                fenced = delimiter
            elif delimiter[0] == fenced[0] and len(delimiter) >= len(fenced):
                fenced = None
            continue
        if fenced:
            continue
        heading = HEADING.match(line)
        if heading:
            level = len(heading[1])
            if skipped_level is not None and level <= skipped_level:
                skipped_level = None
            if skipped_level is None and SKIP_BOARD_SECTIONS.fullmatch(line):
                skipped_level = level
                continue
        if skipped_level is not None:
            continue
        if marker.startswith(("Source captures ", "Product page ")):
            continue
        if re.fullmatch(r"\*{1,2}(?:Library|Example) Description:?\*{1,2}:?", marker, re.I):
            skipped_level = 6
            continue
        selected.append(line)
    return _prompt_text("\n".join(selected))


def _parts(text: str, limit: int = PART_LIMIT) -> Iterator[str]:
    """Split on line breaks while retaining exact source lines."""
    pending = []
    length = 0
    for line in text.splitlines(keepends=True):
        if pending and length + len(line) > limit:
            yield "".join(pending).strip("\n")
            pending, length = [], 0
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut + 1 if cut > 0 else limit
            yield line[:cut]
            line = line[cut:]
        pending.append(line)
        length += len(line)
    if pending:
        yield "".join(pending).strip("\n")


def _records(path: Path) -> Iterator[dict]:
    read = gzip.open if path.suffix == ".gz" else Path.open
    with read(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _artifact(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Corpus artifact is outside its root: {relative}")
    return target


class BoardEvidence:
    """Scope retrieval to the exact board and index its evidence in memory.

    A corpus directory uses each selected document's own JSONL and page-layout
    Markdown. A packed retrieval JSONL (plain or gzip) can be used directly.
    """

    def __init__(self, corpus: Path, board: str):
        self.root = Path(corpus).resolve()
        self.board = board
        self.documents = []
        self.profile = ""
        if self.root.is_file():
            raw = self._stream_passages()
        else:
            raw = self._indexed_passages()
        self.passages = tuple(raw)
        if not self.passages:
            raise ValueError(f"No retrievable evidence for board {board!r}")
        self.citations = {passage.id: passage.citation() for passage in self.passages}
        self._terms = [Counter(_tokens(passage.text)) for passage in self.passages]
        self._lengths = [sum(terms.values()) for terms in self._terms]
        self._average_length = max(1.0, sum(self._lengths) / len(self._lengths))
        self._frequency = Counter(term for terms in self._terms for term in terms)

    def _indexed_passages(self) -> Iterator[Passage]:
        board = next((row for row in _records(self.root / "boards.jsonl")
                      if row["board"] == self.board), None)
        if board is None:
            raise ValueError(f"No board profile for {self.board!r}")
        profile = _artifact(self.root, board["path"]).read_text(encoding="utf-8")
        self.profile = _board_text(profile)
        yield from self._board_passages(self.profile, board["title"], board["path"])

        documents = [row for row in _records(self.root / "documents.jsonl")
                     if row.get("status") in {"ok", "partial"}
                     and any(source["board"] == self.board for source in row.get("sources", ()))]

        def board_role(row):
            roles = {source["role"] for source in row["sources"] if source["board"] == self.board}
            return next((role for role in ("datasheet", "schematic", "manual", "application_note", "reference")
                         if role in roles), min(roles))

        for row in sorted(documents, key=lambda row: row["id"]):
            role = board_role(row)
            if role not in ENGINEERING_ROLES:
                continue
            self.documents.append({**row, "role": role})
            number = len(self.documents)
            for record in _records(_artifact(self.root, row["chunks"])):
                yield from self._pdf_passages(number, record, row["markdown"], role)
            if "layout" in row:
                yield from self._layout_passages(number, row, role)

    def _stream_passages(self) -> Iterator[Passage]:
        board_records = []
        document_numbers = {}
        for record in _records(self.root):
            if self.board not in record.get("board_slugs", ()):
                continue
            kind = record.get("kind")
            if kind == "board":
                board_records.append(record)
                continue
            role = record.get("role", "reference")
            if role not in ENGINEERING_ROLES:
                continue
            document_id = record["document_id"]
            if document_id not in document_numbers:
                document_numbers[document_id] = len(document_numbers) + 1
                self.documents.append({
                    "id": document_id, "role": role, "title": record["title"],
                    "markdown": record["artifact"],
                })
            yield from self._pdf_passages(document_numbers[document_id], record, record["artifact"], role)
        if board_records:
            first = board_records[0]
            self.profile = _board_text("\n\n".join(row["text"] for row in board_records))
            yield from self._board_passages(self.profile, first["title"], first["artifact"])

    def _board_passages(self, profile: str, title: str, artifact: str) -> Iterator[Passage]:
        for part, text in enumerate(_parts(profile), 1):
            yield Passage(f"B{part:02d}", text, "board", title, None, artifact,
                          f"board:{self.board}:hardware:{part}", kind="board")

    def _pdf_passages(self, number: int, row: dict, artifact: str, role: str) -> Iterator[Passage]:
        kind = row.get("kind", "pdf_page")
        page = row["page"]
        form = "L" if kind == "pdf_layout" else "P"
        label = f"D{number:02d}{form}{page:03d}C{row.get('part', 1):02d}"
        for part, text in enumerate(_parts(_prompt_text(row["text"])), 1):
            if text.strip():
                identifier = label if part == 1 else f"{label}S{part}"
                yield Passage(identifier, text, role, row["title"], page,
                              artifact, row["id"], row["document_id"], kind)

    def _layout_passages(self, number: int, doc: dict, role: str) -> Iterator[Passage]:
        markdown = _artifact(self.root, doc["layout"]).read_text(encoding="utf-8")
        contents = LAYOUT_PAGE.split(markdown)
        for page, text in zip(contents[1::2], contents[2::2]):
            page_number = int(page)
            if not any(line.startswith("    ") and line.strip() for line in text.splitlines()):
                continue
            yield from self._pdf_passages(number, {
                "kind": "pdf_layout", "page": page_number, "title": doc["title"],
                "document_id": doc["id"], "part": 1, "text": text,
                "id": f"{doc['id']}:layout:p{page_number}",
            }, doc["layout"], role)

    def _score(self, index: int, keywords: set[str]) -> float:
        passage = self.passages[index]
        terms = self._terms[index]
        length_factor = 0.3 + 0.7 * self._lengths[index] / self._average_length
        score = sum(
            math.log1p((len(self.passages) - self._frequency[token] + 0.5)
                       / (self._frequency[token] + 0.5))
            * (2.2 * terms[token] / (terms[token] + 1.2 * length_factor))
            for token in keywords & terms.keys()
        )
        # Prefer the normal transcription when both renderings match equally.
        return score * (0.87 if passage.kind == "pdf_layout" else 1.0)

    def retrieve(self, query: str, *, limit: int = 12, max_chars: int = 45_000,
                 roles: Iterable[str] = ()) -> list[Passage]:
        """Return whole, scored passages within an inclusive prompt-context budget."""
        if limit < 1 or max_chars < 1:
            return []
        roles = set(roles) or ENGINEERING_ROLES
        keywords = set(_tokens(query))
        scores = [(self._score(index, keywords), index) for index, passage in enumerate(self.passages)
                  if passage.role in roles]
        scores.sort(key=lambda item: (-item[0], item[1]))

        selected = []
        remaining = max_chars
        used = Counter()
        seen = set()
        for score, index in scores:
            if keywords and not score:
                continue
            passage = self.passages[index]
            key = (passage.document_id, passage.page)
            if used[key] >= 2 or passage.text in seen:
                continue
            cost = len(passage.context()) + (2 if selected else 0)
            if cost > remaining:
                continue
            selected.append(passage)
            seen.add(passage.text)
            used[key] += 1
            remaining -= cost
            if len(selected) == limit:
                break
        return selected

    def context(self, query: str, **options) -> str:
        return "\n\n".join(passage.context() for passage in self.retrieve(query, **options))
