"""Render technical content and local source inventory for Click boards."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin, urlsplit
from zipfile import BadZipFile

from lxml import etree, html

from .click_inventory import archive_members

if TYPE_CHECKING:
    from .click_inventory import BoardSource


_IMAGE_EXTENSIONS = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".tiff", ".webp"}
_HIDDEN = {"script", "style", "noscript", "form", "button", "iframe"}
_WHITESPACE = re.compile(r"\s+")
_URL_SAFE = ":/%?&=+#@!$'()*;,~.-_[]"
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = _HEADINGS | {
    "address", "article", "div", "figure", "figcaption", "section", "blockquote", "dl",
    "p", "pre", "ul", "ol", "table",
}
_PINOUT_HEADER = (
    "Left notes", "Left board pin", "Left socket pin #", "Left mikroBUS signal",
    "Right mikroBUS signal", "Right socket pin #", "Right board pin", "Right notes",
)


@dataclass(frozen=True)
class ProductPage:
    title: str
    url: str
    body: str


def _clean(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _escape(value: str) -> str:
    value = value.replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*\[\]])", r"\\\1", value)


def _link(label: str, target: str) -> str:
    return f"[{label}](<{quote(target, safe=_URL_SAFE)}>)"


def _web_link(base: str, target: str) -> str:
    try:
        url = urljoin(base, target.strip())
        parsed = urlsplit(url)
    except ValueError:
        return ""
    return url if target.strip() and parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _file_link(label: str, path: Path, relative_to: Path) -> str:
    target = quote(os.path.relpath(path, relative_to), safe="/")
    return f"[{_escape(label)}](<{target}>)"


def _fence(text: str) -> str:
    width = max((len(match) for match in re.findall(r"`+", text)), default=0) + 1
    return "`" * width


def _raw_text(node: html.HtmlElement) -> str:
    parts = [node.text or ""]
    for child in node:
        if child.tag == "br":
            parts.append("\n")
        elif isinstance(child.tag, str):
            parts.append(_raw_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _inline_children(node: html.HtmlElement, base: str) -> str:
    parts = [_escape(node.text or "")]
    for child in node:
        parts.append(_inline(child, base))
        parts.append(_escape(child.tail or ""))
    return "".join(parts)


def _inline(node: html.HtmlElement, base: str) -> str:
    tag = node.tag
    if not isinstance(tag, str) or tag in _HIDDEN:
        return ""
    if tag == "br":
        return "<br>"
    if tag == "img":
        target = _web_link(base, node.get("data-src") or node.get("src", ""))
        alt = _escape(_clean(node.get("alt") or node.get("title") or "Product image"))
        return _link(alt, target) if target else alt
    if tag == "code":
        text = _clean(_raw_text(node))
        if not text:
            return ""
        fence = _fence(text)
        padding = " " if text.startswith("`") or text.endswith("`") else ""
        return f"{fence}{padding}{text}{padding}{fence}"
    content = _inline_children(node, base)
    if not content.strip():
        return content
    text = content.strip()
    if tag == "a":
        target = _web_link(base, node.get("href", ""))
        # An image can already contribute a link, which Markdown cannot nest.
        if not target or node.xpath(".//img"):
            return content
        result = _link(_clean(text), target)
    elif tag in {"sub", "sup"}:
        result = f"<{tag}>{_clean(text)}</{tag}>"
    elif tag in {"b", "strong", "em", "i"}:
        delimiter = "**" if tag in {"b", "strong"} else "*"
        result = f"{delimiter}{_clean(text)}{delimiter}"
    else:
        return content
    return content[:len(content) - len(content.lstrip())] + result + content[len(content.rstrip()):]


def _span(cell: html.HtmlElement, attribute: str) -> int:
    try:
        return max(1, min(100, int(cell.get(attribute, "1"))))
    except ValueError:
        return 1


def _table(node: html.HtmlElement, base: str) -> str:
    rows = node.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
    if not rows:
        return ""
    values: list[list[str]] = []
    spans: dict[tuple[int, int], str] = {}
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row.xpath("./th | ./td"):
            while (row_index, column) in spans:
                column += 1
            text = _clean(_inline_children(cell, base)).replace("|", "\\|")
            for row_offset in range(min(_span(cell, "rowspan"), len(rows) - row_index)):
                for column_offset in range(_span(cell, "colspan")):
                    spans.setdefault((row_index + row_offset, column + column_offset), text)
            column += _span(cell, "colspan")
        width = max((index + 1 for r, index in spans if r == row_index), default=0)
        values.append([spans.get((row_index, index), "") for index in range(width)])

    width = max(map(len, values))
    if not width:
        return ""
    classes = node.get("class", "").lower()
    if "pinout-diagram" in classes and width == len(_PINOUT_HEADER):
        headers = _PINOUT_HEADER
        values = values[1:] if rows[0].find("th") is not None else values
    elif rows[0].find("th") is not None or rows[0].getparent().tag == "thead":
        headers = tuple(values.pop(0))
    elif "specification-table" in classes and width == 2:
        headers = ("Property", "Value")
    else:
        headers = tuple(f"Column {i + 1}" for i in range(width))

    def table_row(items: Iterable[str]) -> str:
        cells = list(items)
        return "| " + " | ".join(cells + [""] * (width - len(cells))) + " |"

    rendered = "\n".join([table_row(headers), table_row(["---"] * width), *map(table_row, values)])
    captions = [_clean(_inline_children(caption, base)) for caption in node.xpath("./caption")]
    return "\n\n".join([*filter(None, captions), rendered])


def _list(node: html.HtmlElement, base: str) -> str:
    items = []
    try:
        start = int(node.get("start", "1"))
    except ValueError:
        start = 1
    for offset, child in enumerate(node.xpath("./li")):
        text = "\n\n".join(_blocks(child, base))
        if not text:
            continue
        marker = f"{start + offset}. " if node.tag == "ol" else "- "
        first, *rest = text.splitlines()
        items.extend([marker + first, *(" " * len(marker) + line if line else "" for line in rest)])
    return "\n".join(items)


def _blocks(node: html.HtmlElement, base: str) -> Iterable[str]:
    pending = [_escape(node.text or "")]
    for child in node:
        tag = child.tag
        if tag in _BLOCK_TAGS:
            prose = _clean("".join(pending))
            if prose:
                yield prose
            pending.clear()
            if tag in {"ul", "ol"}:
                rendered = _list(child, base)
            elif tag == "pre":
                code = _raw_text(child).strip("\n\r")
                language = child.xpath("string(./code/@class)")
                match = re.search(r"(?:^|\s)(?:lang|language)-([\w+-]+)", language)
                fence = "`" * max(3, len(_fence(code)))
                rendered = f"{fence}{match[1] if match else ''}\n{code}\n{fence}" if code.strip() else ""
            elif tag == "table":
                rendered = _table(child, base)
            elif tag in _HEADINGS:
                title = _clean(_inline_children(child, base))
                level = min(6, max(3, int(tag[1])))
                rendered = f"{'#' * level} {title}" if title else ""
            else:
                rendered = "\n\n".join(_blocks(child, base))
                if rendered and tag == "blockquote":
                    rendered = "\n".join("> " + line for line in rendered.splitlines())
            if rendered:
                yield rendered
        else:
            pending.append(_inline(child, base))
        pending.append(_escape(child.tail or ""))
    prose = _clean("".join(pending))
    if prose:
        yield prose


def _first_content(document: etree._ElementTree, base: str, *selectors: str) -> str:
    for selector in selectors:
        for node in document.xpath(selector):
            content = "\n\n".join(_blocks(node, base))
            if content:
                return content
    return ""


def _product_metadata(document: etree._ElementTree) -> dict:
    for value in document.xpath("//script[@type='application/ld+json']/text()"):
        try:
            data = json.loads(value, strict=False)
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
    return {}


def _page(path: Path, board: str) -> ProductPage:
    document = html.parse(path, parser=html.HTMLParser(encoding="utf-8", no_network=True))
    default_url = f"https://www.mikroe.com/{quote(board)}"
    product = _product_metadata(document)
    canonical = document.xpath("string(//link[@rel='canonical'][1]/@href)")
    url = _web_link(default_url, canonical) or default_url
    title = _clean(document.xpath("string((//*[@id='center_column']//h1 | //main//h1)[1])"))
    title = title or _clean(document.xpath("string(//h1[1])")) or board

    metadata = []
    sku = document.xpath("string(//*[@id='product_reference']//*[@content][1]/@content)")
    weight = document.xpath("string(//*[@id='weight_desc']//span[1])")
    for label, value in (
        ("Product ID", sku or product.get("sku") or product.get("mpn")),
        ("GTIN", product.get("gtin13") or product.get("gtin")),
        ("Weight", weight),
    ):
        if value:
            metadata.append(f"- {label} — {_escape(_clean(str(value)))}")
    categories = [
        _clean(node.text_content())
        for node in document.xpath("//*[@id='breadcrumb']//a[contains(@href, '/click/')]")
    ]
    if categories:
        metadata.append("- Category — " + " / ".join(map(_escape, categories)))

    overview = _first_content(
        document, url, "//*[@id='short_description_content']",
        "//*[@id='board-overview']//*[contains(concat(' ', normalize-space(@class), ' '), ' desc ')]",
    )
    if not overview:
        description = _clean(document.xpath("string(//meta[@name='description'][1]/@content)"))
        if description:
            overview = _escape(description)
    technical = _first_content(
        document, url, "//*[@id='info-content']", "//*[@id='info-description']", "//*[@id='idTab1']",
    )
    extra_downloads = [
        _clean(_inline(link, url))
        for link in document.xpath(
            "//*[@id='center_column']//a[contains(concat(' ', normalize-space(@class), ' '), ' btn-download ')"
            " and not(ancestor::*[@id='info-content' or @id='info-description' or @id='idTab1'])]"
        )
    ]
    downloads = "\n".join(f"- {link}" for link in dict.fromkeys(extra_downloads) if link and link not in technical)
    body = "\n\n".join(filter(None, [
        "\n".join(metadata),
        f"### Overview\n\n{overview}" if overview else "",
        technical,
        f"### Other downloads\n\n{downloads}" if downloads else "",
    ]))
    return ProductPage(title, url, body)


def _assets(sources: Iterable[BoardSource], relative_to: Path, warnings: list[str]) -> list[str]:
    files: dict[str, dict[str, Path]] = defaultdict(dict)
    for source in sources:
        for name in source.assets:
            path = source.path / name
            if path != source.page:
                files[name][source.collection] = path
    if not files:
        return []
    lines = [
        "## Local board assets",
        "",
        "| Asset | Collections |",
        "| --- | --- |",
    ]
    archives: dict[tuple[str, tuple[tuple[str, int], ...]], list[str]] = defaultdict(list)
    for name, variants in sorted(files.items()):
        local_links = [
            _file_link(collection, path, relative_to)
            for collection, path in variants.items()
        ]
        name_text = _escape(_clean(name)).replace("|", "\\|")
        lines.append(f"| {name_text} | {', '.join(local_links)} |")
        for collection, path in variants.items():
            if path.suffix.lower() in _IMAGE_EXTENSIONS or not path.is_file():
                continue
            try:
                members = archive_members(path)
                if members:
                    archives[(name, members)].append(collection)
            except (OSError, ValueError, BadZipFile) as exc:
                warnings.append(f"{collection}/{name} archive could not be indexed ({type(exc).__name__})")
    if archives:
        lines.extend(["", "### Archive contents"])
    for (name, members), collections in archives.items():
        lines.extend([
            "",
            f"#### {_escape(_clean(name))}",
            "",
            ", ".join(map(_escape, collections)),
            "",
            "| Member | Uncompressed bytes |",
            "| --- | --- |",
        ])
        for member, size in members:
            member_text = _escape(_clean(member)).replace("|", "\\|")
            lines.append(f"| {member_text} | {size} |")
    return lines


def write_boards(
    output: Path,
    sources: Mapping[str, Iterable[BoardSource]],
    board_documents: Mapping[str, Iterable[tuple[str, str, str]]],
) -> list[dict]:
    """Write one profile per board using catalog-relative (name, role, path) references."""
    profiles = output / "boards"
    profiles.mkdir(parents=True, exist_ok=True)
    index = []
    for board, board_sources in sorted(sources.items()):
        references = list(board_sources)
        pages: dict[ProductPage, list[tuple[str, Path]]] = defaultdict(list)
        warnings = []
        for source in references:
            if source.page is None:
                continue
            try:
                pages[_page(source.page, board)].append((source.collection, source.page))
            except (OSError, etree.LxmlError, ValueError) as exc:
                warnings.append(f"{source.collection} page could not be parsed ({type(exc).__name__})")
        title = next(iter(pages)).title if pages else board
        path = profiles / f"{board}.md"
        lines = [
            f"# {_escape(title)}",
            "",
            f"Board slug — {_escape(board)}",
        ]
        if pages:
            lines.append(f"\nProduct page — {_link('MIKROE', next(iter(pages)).url)}")
        source_index = [
            {
                "collection": source.collection,
                "path": str(source.path),
                "page": str(source.page) if source.page else None,
            }
            for source in references
        ]
        documents = sorted(set(board_documents.get(board, ())))
        lines.extend(["", "## PDF documents", ""])
        if documents:
            for name, role, target in documents:
                reference = _file_link(name, output / target, profiles)
                lines.append(f"- {reference} — {_escape(role.replace('_', ' '))}")
        else:
            lines.append("The catalog has no processed PDF linked to this board.")
        page_versions = []
        for page, captures in pages.items():
            labels = ", ".join(_file_link(label, capture, profiles) for label, capture in captures)
            lines.extend(["", "## Saved product documentation", "", f"Source captures — {labels}"])
            if page.body:
                lines.extend(["", page.body])
            else:
                warnings.append(f"No technical product text found in {', '.join(label for label, _ in captures)}")
            page_versions.append({"url": page.url, "sources": [label for label, _ in captures]})
        if not pages:
            warnings.append("No saved product page could be read.")
        assets = _assets(references, profiles, warnings)
        if assets:
            lines.extend(["", *assets])
        if warnings:
            lines.extend(["", "## Source notes", "", *("- " + _escape(warning) for warning in warnings)])
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=profiles, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write("\n".join(lines) + "\n")
                stream.flush()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        index.append({
            "board": board,
            "title": title,
            "path": path.relative_to(output).as_posix(),
            "page_versions": page_versions,
            "sources": source_index,
            "documents": [{"filename": name, "role": role, "path": target} for name, role, target in documents],
            "warnings": warnings,
        })
    return index
