"""Validate model replies and preserve auditable source associations."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import PurePosixPath

MAX_FILE_BYTES = 160_000


def decode_reply(raw: str) -> dict:
    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    result = json.loads(fence[1] if fence else text)
    if not isinstance(result, dict):
        raise ValueError("The reply must be a JSON object.")
    return result


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def plan_findings(plan: dict, board: str, evidence: dict[str, str]) -> list[str]:
    issues = []
    if plan.get("board") != board:
        issues.append(f"board must be {board}")
    for field in ("component", "interface"):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            issues.append(f"{field} must be a nonempty string")

    named = set()
    claims = plan.get("claims")
    if not isinstance(claims, list) or not claims:
        issues.append("claims must be a nonempty array")
        claims = []
    for claim in claims:
        if not isinstance(claim, dict) or not all(
            isinstance(claim.get(key), str) and claim[key].strip()
            for key in ("name", "value", "evidence", "quote")
        ):
            issues.append("Each claim needs nonempty name, value, evidence and quote strings")
            continue
        name = claim["name"]
        if name in named:
            issues.append(f"Duplicate claim name {name}")
        named.add(name)
        source = evidence.get(claim["evidence"], "")
        if len(normalize(claim["quote"])) < 8 or normalize(claim["quote"]) not in normalize(source):
            issues.append(f"{name}: quote not found verbatim in passage {claim['evidence']}")

    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        issues.append("operations must be a nonempty array")
        operations = []
    for item in operations:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(
            item.get("description"), str
        ):
            issues.append("Each operation needs a name and description")
            continue
        refs = item.get("claims")
        if not isinstance(refs, list) or not refs or any(
            not isinstance(ref, str) or ref not in named for ref in refs
        ):
            issues.append(f"{item['name']}: operation needs valid claim names")

    unresolved = plan.get("unresolved")
    if not isinstance(unresolved, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("blocking"), bool)
        or not isinstance(item.get("issue"), str) or not isinstance(item.get("query"), str)
        for item in unresolved
    ):
        issues.append("unresolved must be an array of issue, blocking and query objects")
    return issues


def package_findings(package: dict, paths: tuple[str, ...], plan: dict) -> list[str]:
    files = package.get("files")
    if not isinstance(files, dict):
        return ["files must be an object mapping paths to complete contents"]
    issues = []
    required = set(paths)
    for path in sorted(required.difference(files)):
        issues.append(f"Missing required artifact: {path}")
    for path, text in files.items():
        relative = PurePosixPath(path)
        if path not in required or relative.is_absolute() or ".." in relative.parts:
            issues.append(f"Unexpected file path: {path}")
        if not isinstance(text, str) or "\x00" in text:
            issues.append(f"{path}: file content must be UTF-8 text")
        elif len(text.encode("utf-8", "replace")) > MAX_FILE_BYTES:
            issues.append(f"{path}: generated artifact exceeds the size cap")
        elif not text.strip() and not relative.name.startswith("Click."):
            issues.append(f"{path}: empty file")

    traces = package.get("trace")
    names = {claim["name"] for claim in plan["claims"]}
    if not isinstance(traces, list) or not traces:
        return [*issues, "trace must cite approved plan claims and literal code symbols"]
    for row in traces:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k]
                                               for k in ("path", "claim", "symbol")):
            issues.append("Each trace needs path, claim and symbol strings")
            continue
        text = files.get(row["path"])
        if row["claim"] not in names or not isinstance(text, str) or row["symbol"] not in text:
            issues.append(f"Invalid trace: {row['claim']} -> {row['path']}::{row['symbol']}")
    limits = package.get("limitations")
    if not isinstance(limits, list) or not all(isinstance(item, str) for item in limits):
        issues.append("limitations must be an array of strings")
    return issues


def expand_operation_traces(package: dict, plan: dict) -> None:
    """Expand a trace to an operation into that operation's approved claim IDs."""
    rows = package.get("trace")
    if not isinstance(rows, list):
        return
    operations = {operation["name"]: operation["claims"] for operation in plan["operations"]}
    expanded = []
    for row in rows:
        name = row.get("claim") if isinstance(row, dict) else None
        if isinstance(name, str) and name in operations:
            expanded.extend({**row, "claim": claim} for claim in operations[name])
        else:
            expanded.append(row)
    package["trace"] = expanded


def separate_documentation_traces(package: dict, plan: dict) -> list[dict]:
    """Keep code anchors in the gate and log unresolved prose references separately."""
    rows = package.get("trace")
    files = package.get("files")
    if not isinstance(rows, list) or not isinstance(files, dict):
        return []
    names = {claim["name"] for claim in plan["claims"]}
    kept = []
    unresolved = []
    for row in rows:
        if isinstance(row, dict) and all(isinstance(row.get(key), str) for key in ("path", "claim", "symbol")):
            path = row["path"]
            text = files.get(path)
            if (row["claim"] in names and isinstance(text, str) and not path.endswith((".c", ".h"))
                    and row["symbol"] not in text):
                unresolved.append(row)
                continue
        kept.append(row)
    package["trace"] = kept
    return unresolved
