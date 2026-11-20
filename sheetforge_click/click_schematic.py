"""Read connectivity explicitly exported in Altium schematic PDF bookmarks."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

_ALTIUM_ROOT = re.compile(r"^Schematic\s*\(", re.IGNORECASE)
_SECTION_NAMES = {"components", "nets"}


@dataclass
class _Bookmark:
    level: int
    label: str
    page: int | None
    number: int
    children: list[_Bookmark] = field(default_factory=list)

    def record(self) -> dict:
        return {"label": self.label, "page": self.page, "bookmark": self.number}

    def subtree(self) -> dict:
        result = self.record()
        if self.children:
            result["children"] = [child.subtree() for child in self.children]
        return result


def _outline_tree(outline: Sequence[Sequence]) -> list[_Bookmark]:
    roots: list[_Bookmark] = []
    stack: list[_Bookmark] = []
    for number, (level, label, page, *_) in enumerate(outline, 1):
        while stack and stack[-1].level >= level:
            stack.pop()
        node = _Bookmark(level, label, page if page > 0 else None, number)
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)
    return roots


def _pin_bookmarks(component: _Bookmark) -> Iterator[_Bookmark]:
    for child in component.children:
        if child.label.casefold().strip() == "pins":
            yield from child.children
        else:
            yield child


def _schematic_sheets(node: _Bookmark, ancestors: list[dict]) -> Iterator[dict]:
    groups = [child for child in node.children if child.label.casefold().strip() in _SECTION_NAMES]
    if groups:
        components = []
        nets = []
        pin_owners: dict[str, list[int]] = defaultdict(list)
        for group in groups:
            if group.label.casefold().strip() != "components":
                continue
            for component in group.children:
                pins = []
                for pin in _pin_bookmarks(component):
                    pins.append(pin.record())
                    owners = pin_owners[pin.label]
                    if component.number not in owners:
                        owners.append(component.number)
                components.append({**component.record(), "pins": pins})
        for group in groups:
            if group.label.casefold().strip() != "nets":
                continue
            for net in group.children:
                pins = []
                objects = []
                for connection in net.children:
                    if connection.label.casefold().strip() != "pins":
                        objects.append(connection.subtree())
                        continue
                    for pin in connection.children:
                        pins.append({
                            **pin.record(),
                            "matching_component_bookmarks": pin_owners.get(pin.label, []),
                        })
                nets.append({**net.record(), "pins": pins, "objects": objects})
        if components or nets:
            yield {
                **node.record(), "ancestors": ancestors,
                "components": components, "nets": nets,
            }
    for child in node.children:
        if child.label.casefold().strip() not in _SECTION_NAMES:
            yield from _schematic_sheets(child, [*ancestors, node.record()])


def _find_altium_sheets(node: _Bookmark, ancestors: list[dict]) -> Iterator[dict]:
    if _ALTIUM_ROOT.match(node.label.strip()):
        yield from _schematic_sheets(node, ancestors)
    else:
        for child in node.children:
            yield from _find_altium_sheets(child, [*ancestors, node.record()])


def extract_schematic(outline: Sequence[Sequence]) -> dict | None:
    """Return direct bookmark evidence, with one-based indices into the PDF outline."""
    if not any(_ALTIUM_ROOT.match(row[1].strip()) for row in outline):
        return None
    sheets = [
        sheet for root in _outline_tree(outline) for sheet in _find_altium_sheets(root, [])
    ]
    return {"source": "altium_pdf_bookmarks", "sheets": sheets} if sheets else None


def _code(label: str) -> str:
    text = " ".join(label.split())
    ticks = max((len(part) for part in re.findall(r"`+", text)), default=0) + 1
    fence = "`" * ticks
    padding = " " if "`" in text else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _net_line(net: dict) -> str:
    pins = ", ".join(_code(pin["label"]) for pin in net["pins"])
    line = f"- {_code(net['label'])}"
    line += f" connects {pins}." if pins else " has no pin bookmarks."
    for group in net["objects"]:
        if labels := ", ".join(_code(item["label"]) for item in group.get("children", [])):
            line += f" {_code(group['label'])} {labels}."
        else:
            line += f" {_code(group['label'])}."
    return line


def schematic_sections(schematic: dict | None, page: int) -> list[str]:
    """Render compact page-scoped passages from the extracted bookmark evidence."""
    if schematic is None:
        return []
    sections = []
    for sheet in schematic["sheets"]:
        sheet_page = sheet["page"] or next(
            (item["page"] for item in reversed(sheet["ancestors"]) if item["page"]), None,
        )
        label = _code(sheet["label"])
        components = [
            item for item in sheet["components"]
            if page == (item["page"] or sheet_page) or any(pin["page"] == page for pin in item["pins"])
        ]
        nets = [
            item for item in sheet["nets"]
            if page == (item["page"] or sheet_page) or any(pin["page"] == page for pin in item["pins"])
        ]
        if components:
            lines = [f"### Schematic components from PDF bookmarks for {label}", ""]
            for component in components:
                name = _code(component["label"])
                pins = ", ".join(_code(pin["label"]) for pin in component["pins"])
                lines.append(f"- {name} lists pins {pins}." if pins else f"- {name}")
            sections.append("\n".join(lines))
        if nets:
            heading = f"### Schematic connectivity from PDF bookmarks for {label}"
            sections.append("\n".join((heading, "", *(_net_line(net) for net in nets))))
    return sections
