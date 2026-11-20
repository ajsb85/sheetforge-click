"""Discover Click board PDFs and keep every source of each distinct document."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote

_PDF_HEADER = re.compile(rb"%PDF-\d\.\d(?:[\r\n\x00 ]|$)")
_ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z"}
_ARCHIVE_HEADERS = (b"PK\x03\x04", b"PK\x05\x06", b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c")


def document_role(name: str) -> str:
    """Classify filename evidence, keeping ambiguous names as references."""
    words = re.sub(r"[^a-z0-9]+", " ", unquote(name).casefold())
    if re.search(r"\b(?:schem(?:atic)?|shematic|sch)\b", words):
        return "schematic"
    if re.search(r"\b(?:annex iii|declaration of conformity|reach|rohs)\b", words):
        return "compliance"
    if re.search(r"\b(?:3d print|pdf3d|mechanical|dimensions|pcb|footprint)\b", words):
        return "mechanical_drawing"
    if re.search(r"\b(?:datasheets?|data sheets?)\b", words):
        return "datasheet"
    if re.search(r"\b(?:app(?:lication)? notes?|appnote|errata)\b", words):
        return "application_note"
    if re.search(r"\b(?:manual|user ?guide|reference guide|at commands?|getting started)\b", words):
        return "manual"
    if re.search(r"\bcatalo(?:g|gue)\b", words):
        return "catalog"
    return "reference"


@dataclass(frozen=True)
class _ArchiveMember:
    name: str
    size: int
    index: int | None  # ZIP directory position, including repeated member names


@dataclass(frozen=True)
class DocumentSource:
    collection: str
    board: str
    relative_path: str
    path: Path
    role: str
    symlink_target: str | None = None
    _members: tuple[_ArchiveMember, ...] = field(default=(), repr=False)

    @property
    def member(self) -> str | None:
        return "!".join(member.name for member in self._members) or None

    @property
    def filename(self) -> str:
        return _basename(self._members[-1].name) if self._members else self.path.name

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        if self._members:
            with _open_member_chain(self.path, self._members) as stream:
                yield stream
        else:
            with self.path.open("rb") as stream:
                yield stream

    def to_dict(self) -> dict:
        return {
            "collection": self.collection,
            "board": self.board,
            "relative_path": self.relative_path,
            "path": str(self.path),
            "member": self.member,
            "member_chain": [member.name for member in self._members],
            "member_indices": [member.index for member in self._members],
            "role": self.role,
            "symlink_target": self.symlink_target,
        }


@dataclass
class Document:
    sha256: str
    size: int
    sources: list[DocumentSource] = field(default_factory=list)

    @property
    def source(self) -> DocumentSource:
        # Prefer an ordinary file over archive expansion or a symlink.
        return min(self.sources, key=lambda source: (bool(source._members), source.symlink_target is not None))

    @property
    def path(self) -> Path:
        return self.source.path

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        with self.source.open() as stream:
            yield stream

    def read_bytes(self) -> bytes:
        with self.open() as stream:
            return stream.read()


@dataclass(frozen=True)
class BoardSource:
    collection: str
    board: str
    path: Path
    assets: tuple[str, ...]
    page: Path | None


@dataclass(frozen=True)
class InventoryIssue:
    collection: str
    board: str
    path: str
    reason: str
    member: str | None = None


@dataclass
class Inventory:
    collections: dict[str, Path]
    boards: dict[str, list[BoardSource]] = field(default_factory=dict)
    documents: dict[str, Document] = field(default_factory=dict)
    issues: list[InventoryIssue] = field(default_factory=list)


def _basename(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def _suffix(name: str) -> str:
    return Path(_basename(unquote(name))).suffix.casefold()


def _is_archive(name: str, prefix: bytes) -> bool:
    return prefix.startswith(_ARCHIVE_HEADERS) or _suffix(name) in _ARCHIVE_SUFFIXES


def _has_pdf_header(prefix: bytes) -> bool:
    return not prefix.startswith(_ARCHIVE_HEADERS) and _PDF_HEADER.search(prefix) is not None


def _digest(stream: BinaryIO, prefix: bytes) -> tuple[str, int]:
    sha256 = hashlib.sha256(prefix)
    size = len(prefix)
    while block := stream.read(1024 * 1024):
        sha256.update(block)
        size += len(block)
    return sha256.hexdigest(), size


def _sevenzip(args: list[str]) -> bytes:
    executable = shutil.which("7z") or shutil.which("7zz")
    if executable is None:
        raise OSError("7z or 7zz is required to inspect this archive")
    try:
        result = subprocess.run(
            [executable, *args], stdin=subprocess.DEVNULL, capture_output=True,
            check=True, timeout=90,
        )
    except subprocess.CalledProcessError as exc:
        error = exc.stderr.decode("utf-8", "replace").strip()
        raise OSError(error or f"7z exited with code {exc.returncode}") from exc
    except subprocess.TimeoutExpired as exc:
        raise OSError("7z timed out") from exc
    return result.stdout


@contextmanager
def _local_archive(container: Path | io.BytesIO) -> Iterator[Path]:
    if isinstance(container, Path):
        yield container
    else:
        with tempfile.TemporaryDirectory(prefix="sheetforge-click-") as directory:
            archive = Path(directory) / "archive"
            archive.write_bytes(container.getbuffer())
            yield archive


def _archive_entries(container: Path | io.BytesIO) -> list[_ArchiveMember]:
    if zipfile.is_zipfile(container):
        with zipfile.ZipFile(container) as archive:
            return [
                _ArchiveMember(item.filename, item.file_size, index)
                for index, item in enumerate(archive.infolist()) if not item.is_dir()
            ]
    with _local_archive(container) as archive:
        output = _sevenzip(["l", "-slt", "-ba", "-sccUTF-8", "--", str(archive)])
    entries = []
    for record in re.split(r"\n\s*\n", output.decode("utf-8", "replace")):
        fields = dict(line.split(" = ", 1) for line in record.splitlines() if " = " in line)
        if fields.get("Folder") != "+" and fields.get("Path") and fields.get("Size") is not None:
            entries.append(_ArchiveMember(fields["Path"], int(fields["Size"]), None))
    return entries


def archive_members(path: Path) -> tuple[tuple[str, int], ...]:
    """List top-level member paths and sizes, or return empty for other files."""
    with path.open("rb") as stream:
        prefix = stream.read(1024)
    if _has_pdf_header(prefix) or not _is_archive(path.name, prefix):
        return ()
    return tuple((entry.name, entry.size) for entry in _archive_entries(path))


@contextmanager
def _open_member(container: Path | io.BytesIO, member: _ArchiveMember) -> Iterator[BinaryIO]:
    if member.index is not None:
        with zipfile.ZipFile(container) as archive:
            with archive.open(archive.infolist()[member.index]) as stream:
                yield stream
    else:
        with _local_archive(container) as archive:
            data = _sevenzip(["x", "-so", "-spd", "-p", "--", str(archive), member.name])
        with io.BytesIO(data) as stream:
            yield stream


@contextmanager
def _open_member_chain(
    container: Path | io.BytesIO, members: tuple[_ArchiveMember, ...],
) -> Iterator[BinaryIO]:
    with _open_member(container, members[0]) as stream:
        if len(members) == 1:
            yield stream
        else:
            with io.BytesIO(stream.read()) as inner:
                with _open_member_chain(inner, members[1:]) as pdf:
                    yield pdf


_ARCHIVE_ERRORS = (OSError, EOFError, ValueError, RuntimeError, zipfile.BadZipFile)


def _archive_documents(
    container: Path | io.BytesIO,
    report_issue: Callable[[str, tuple[_ArchiveMember, ...]], None],
    parents: tuple[_ArchiveMember, ...] = (),
) -> Iterator[tuple[str, int, tuple[_ArchiveMember, ...]]]:
    try:
        entries = _archive_entries(container)
    except _ARCHIVE_ERRORS as error:
        report_issue(str(error), parents)
        return
    for member in entries:
        name = member.name
        suffix = _suffix(name)
        if suffix and suffix not in _ARCHIVE_SUFFIXES and suffix != ".pdf":
            continue
        chain = (*parents, member)
        try:
            with _open_member(container, member) as stream:
                prefix = stream.read(1024)
                if _has_pdf_header(prefix):
                    digest, size = _digest(stream, prefix)
                    yield digest, size, chain
                elif _is_archive(name, prefix):
                    with io.BytesIO(prefix + stream.read()) as inner:
                        yield from _archive_documents(inner, report_issue, chain)
                elif suffix == ".pdf":
                    report_issue("Missing PDF header", chain)
        except _ARCHIVE_ERRORS as error:
            report_issue(str(error), chain)


def _record_document(
    inventory: Inventory,
    board: BoardSource,
    path: Path,
    digest: str,
    size: int,
    members: tuple[_ArchiveMember, ...] = (),
) -> None:
    document = inventory.documents.get(digest)
    if document is None:
        document = inventory.documents[digest] = Document(digest, size)
    name = _basename(members[-1].name) if members else path.name
    document.sources.append(DocumentSource(
        collection=board.collection,
        board=board.board,
        relative_path=path.relative_to(inventory.collections[board.collection]).as_posix(),
        path=path,
        role=document_role(name),
        symlink_target=os.readlink(path) if path.is_symlink() else None,
        _members=members,
    ))


def _scan_file(
    inventory: Inventory, board: BoardSource, path: Path,
    digests: dict[tuple[int, int, int, int], tuple[str, int]],
) -> None:
    def report_issue(reason: str, members: tuple[_ArchiveMember, ...] = ()) -> None:
        inventory.issues.append(InventoryIssue(
            board.collection, board.board, str(path), reason,
            "!".join(member.name for member in members) or None,
        ))

    try:
        status = path.stat()
        if not stat.S_ISREG(status.st_mode):
            return
        identity = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
        known = digests.get(identity)
        if known is not None:
            _record_document(inventory, board, path, *known)
            return
        with path.open("rb") as stream:
            prefix = stream.read(1024)
            if _has_pdf_header(prefix):
                digest, size = digests[identity] = _digest(stream, prefix)
                _record_document(inventory, board, path, digest, size)
                return
        if _is_archive(path.name, prefix):
            for digest, size, members in _archive_documents(path, report_issue):
                _record_document(inventory, board, path, digest, size, members)
        elif _suffix(path.name) == ".pdf":
            report_issue("Missing PDF header")
    except OSError as error:
        report_issue(str(error))


def discover(collections: Iterable[tuple[str, Path]]) -> Inventory:
    """Scan both sources, preserving duplicate board and PDF provenance."""
    roots: dict[str, Path] = {}
    for label, path in collections:
        if label in roots:
            raise ValueError(f"Duplicate collection label {label!r}")
        root = Path(path).absolute()
        if not root.is_dir():
            raise FileNotFoundError(f"Collection {label!r} does not exist at {root}")
        roots[label] = root
    inventory = Inventory(roots)
    digests: dict[tuple[int, int, int, int], tuple[str, int]] = {}
    for label, root in roots.items():
        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue

            def report_walk_error(error: OSError) -> None:
                inventory.issues.append(InventoryIssue(label, path.name, error.filename or str(path), str(error)))

            assets = sorted(
                (Path(directory) / name).relative_to(path).as_posix()
                for directory, _, names in os.walk(path, onerror=report_walk_error)
                for name in names
            )
            page = path / "index.html"
            board = BoardSource(label, path.name, path, tuple(assets), page if page.is_file() else None)
            inventory.boards.setdefault(path.name, []).append(board)
            for asset in assets:
                _scan_file(inventory, board, path / asset, digests)
    return inventory
