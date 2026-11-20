"""Generate and validate evidence-backed mikroSDK 2.0 Click Board packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from pathlib import Path

from .click_corpus import write_summary, write_text
from .driver_contract import (
    decode_reply, expand_operation_traces, package_findings, plan_findings,
    separate_documentation_traces,
)
from .driver_evidence import BoardEvidence
from .driver_mimo import FileExchangeClient, MiMoClient, MiMoError, PendingCompletion
from .driver_prompts import IMPLEMENT, PLAN, SYSTEM, VERSION

QUERIES = (
    "mikroBUS pinout jumper supply voltage logic interface onboard settings electrical",
    "schematic pins jumper address ground interrupt device marking variant",
    "register map memory map register summary register address reset values",
    "register bit allocation status interrupt mask command configuration field encoding",
    "serial protocol I2C address repeated start SPI clock mode UART timing wait",
    "power up initialization reset default operating mode enable ready delay interrupt",
    "measurement conversion formula LSB resolution units voltage data encoding",
    "manufacturer restrictions reserved bits invalid error NVM programming endurance",
    "typical application mode example operation command sequence safe recommended",
)
CONTEXT_LIMIT = 92_000


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def digest(value) -> str:
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


def slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
        raise ValueError(f"Invalid board slug: {value!r}")
    return value


def target(evidence: BoardEvidence, stem_override: str | None) -> tuple[str, str]:
    name = evidence.board.removesuffix("-click")
    stem = stem_override or re.sub(r"[^a-z0-9]", "", name)
    if not re.fullmatch(r"[a-z][a-z0-9_]*", stem):
        stem = "click" + stem
    title = next((line[2:] for line in evidence.profile.splitlines() if line.startswith("# ")), name)
    title = re.sub(r"(?i)\bclick\b", "", title)
    words = re.findall(r"[A-Za-z0-9]+", title)
    alias = "Click." + ("".join(word[0].upper() + word[1:] for word in words) or stem)
    return stem, alias


def select_evidence(evidence: BoardEvidence, extra=()) -> list:
    selected = {}
    size = 0

    def add(passages):
        nonlocal size
        for passage in passages:
            cost = len(passage.context()) + 2
            if passage.id not in selected and size + cost <= CONTEXT_LIMIT:
                size += cost
                selected[passage.id] = passage

    add(evidence.retrieve("", roles=("board",), limit=2, max_chars=11000))
    add(evidence.retrieve("", roles=("schematic",), limit=2, max_chars=13000))
    for query in (*extra, *QUERIES):
        add(evidence.retrieve(query, limit=4, max_chars=14500))
    return list(selected.values())


def model_reply(client, messages, *, max_tokens: int, thinking: bool, journal: list[dict], phase: str) -> dict:
    print(f"  MiMo {phase} · {len(json_text(messages)):,} prompt characters", flush=True)
    reply = client.complete(messages, max_tokens=max_tokens, thinking=thinking)
    journal.append({"phase": phase, "usage": reply.usage, "finish_reason": reply.finish_reason})
    if reply.finish_reason not in {"stop", None}:
        raise ValueError(f"Incomplete model reply ({reply.finish_reason})")
    return decode_reply(reply.content)


def prompt(instruction: str, inputs: dict) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": instruction + "\n\nInput data\n" + json_text(inputs)}]


def plan_board(client, evidence: BoardEvidence, directory: Path, args, journal: list[dict]):
    base = select_evidence(evidence)
    identity = digest({
        "provider": args.provider, "model": args.model, "version": VERSION,
        "board": evidence.board, "thinking": args.thinking, "passages": [p.context() for p in base],
    })
    accepted = directory / "plan.json"
    if args.reviewed_plan:
        payload = decode_reply(args.reviewed_plan.read_text(encoding="utf-8"))
        reviewed = payload.get("plan", payload)
        if not isinstance(reviewed, dict):
            raise ValueError("A reviewed plan must be a JSON object.")
        by_id = {p.id: p for p in evidence.passages}
        findings = plan_findings(reviewed, evidence.board, {p.id: p.text for p in evidence.passages})
        if findings:
            raise ValueError("Reviewed plan failed validation: " + "; ".join(findings[:12]))
        selected = [by_id[ref] for ref in dict.fromkeys(row["evidence"] for row in reviewed["claims"])]
        record = {"fingerprint": identity, "plan": reviewed, "passage_ids": [p.id for p in selected],
                  "review": "Imported a locally reviewed hardware plan."}
        write_summary(accepted, record)
        return record, selected
    if accepted.is_file() and not args.force:
        record = json.loads(accepted.read_text(encoding="utf-8"))
        if record["fingerprint"] == identity:
            by_id = {p.id: p for p in evidence.passages}
            if all(ref in by_id for ref in record["passage_ids"]):
                selected = [by_id[ref] for ref in record["passage_ids"]]
                if not plan_findings(record["plan"], evidence.board, {p.id: p.text for p in selected}):
                    return record, selected
    feedback = []
    previous = None
    selected = base
    for attempt in range(args.attempts):
        data = {"board": evidence.board, "evidence": [p.context() for p in selected],
                "previous_findings": feedback, "previous_plan": previous}
        stage = f"plan-{attempt + 1}"
        try:
            candidate = model_reply(client, prompt(PLAN, data), max_tokens=16000 if args.thinking else 12000,
                                    thinking=args.thinking, journal=journal, phase=stage)
        except ValueError as exc:
            feedback = [str(exc)]
            print("  " + feedback[0], flush=True)
            continue
        claims = candidate.get("claims")
        for row in claims if isinstance(claims, list) else []:
            if isinstance(row, dict) and isinstance(row.get("evidence"), str):
                row["evidence"] = row["evidence"].strip().removeprefix("[").removesuffix("]")
        write_summary(directory / f"{stage}.json", candidate)
        feedback = plan_findings(candidate, evidence.board, {p.id: p.text for p in selected})
        unresolved = candidate.get("unresolved")
        blocking = [row for row in unresolved if isinstance(row, dict) and row.get("blocking") is True
                    and isinstance(row.get("query"), str)] if isinstance(unresolved, list) else []
        if (feedback or blocking) and attempt + 1 < args.attempts:
            print(f"  plan review: {len(feedback)} citation findings, {len(blocking)} open hardware questions",
                  flush=True)
            feedback.extend(str(row.get("issue")) for row in blocking)
            previous = candidate
            if blocking:
                selected = select_evidence(evidence, tuple(row["query"] for row in blocking[:5]))
            continue
        if not feedback:
            record = {"fingerprint": identity, "plan": candidate,
                      "passage_ids": [p.id for p in selected], "usage": journal}
            write_summary(accepted, record)
            return record, selected
    raise ValueError("Hardware plan failed validation: " + "; ".join(feedback[:12]))


def verify_artifacts(root: Path, hashes: dict) -> bool:
    return bool(hashes) and all(
        (root / path).is_file() and hashlib.sha256((root / path).read_bytes()).hexdigest() == expected
        for path, expected in hashes.items()
    )


def materialize(root: Path, files: dict[str, str]) -> dict:
    if root.resolve() != root.absolute():
        raise ValueError("Driver artifacts must stay in a regular package directory.")
    hashes = {}
    for name, content in files.items():
        path = root / name
        if path.resolve() != path.absolute():
            raise ValueError(f"Refusing to write through an artifact symlink: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, content)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def run_board(client, args, sdk, board: str) -> int:
    from .driver_sdk import (
        build_with_harness, compare_reference, complete_reference_declarations, core_api, exemplars, layout, package_manifests, reference_api,
        validate_driver,
    )

    directory = args.output / board
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Use a regular output directory for the board.")
    evidence = BoardEvidence(args.corpus, board)
    stem, alias = target(evidence, args.stem)
    paths = layout(stem, alias)
    journal = []
    print(f"{board} → {stem} · {len(evidence.passages)} candidate passages", flush=True)
    report_path = directory / "report.json"
    try:
        accepted, selected = plan_board(client, evidence, directory, args, journal)
        plan = accepted["plan"]
        print(f"  hardware plan · {len(plan['claims'])} cited claims", flush=True)
        used = {item["evidence"] for item in plan["claims"]}
        citations = {p.id: p.citation() for p in selected if p.id in used}
        write_summary(directory / "citations.json", citations)
        blocked = [item["issue"] for item in plan["unresolved"] if item["blocking"]]
        if blocked:
            write_summary(report_path, {"board": board, "status": "blocked", "findings": blocked, "usage": journal})
            print("  blocked: " + "; ".join(blocked), flush=True)
            return 1

        samples = exemplars(sdk, stem, plan["interface"], limit=2, exclude=(stem, board))
        referenced = [p.context() for p in selected if p.id in used]
        data = {"board": board, "stem": stem, "import_marker": alias, "required_paths": paths,
                "hardware_plan": plan, "claim_evidence": referenced, "sdk_exemplars": samples,
                "sdk_core_api": core_api(sdk, plan["interface"])}
        if args.reference_api:
            data["reference_api_contract"] = reference_api(sdk, stem)
        signature = digest({"version": VERSION, "model": args.model, "input": data,
                            "build": str(args.build_harness), "compare": args.compare_reference})
        driver_root = directory / "driver" / stem
        if report_path.is_file() and not args.force:
            record = json.loads(report_path.read_text(encoding="utf-8"))
            if record.get("status") == "verified" and record.get("fingerprint") == signature and verify_artifacts(
                driver_root, record.get("artifacts", {})
            ):
                print("  verified cache hit", flush=True)
                return 0

        findings = []
        validation = {}
        build = None
        previous_package = None
        for attempt in range(args.attempts):
            instruction = IMPLEMENT
            if args.reference_api:
                instruction += (
                    "\nMatch the public compatibility contract provided in the input. The declaration "
                    "map pairs each function name with its return type and parameters. Keep "
                    "documented hardware constraints, and flag any contract operation you "
                    "cannot safely implement. The original C implementation is withheld."
                )
            messages = prompt(instruction, {**data, "previous_findings": findings,
                                            "previous_package": previous_package})
            try:
                package = model_reply(client, messages, max_tokens=args.max_tokens,
                                      thinking=args.thinking, journal=journal, phase=f"package-{attempt + 1}")
            except ValueError as exc:
                findings = [str(exc)]
                continue
            if isinstance(package.get("files"), dict):
                package_manifests(package["files"], stem, alias, evidence.profile, plan["component"])
                if args.reference_api:
                    complete_reference_declarations(package["files"], stem, data["reference_api_contract"])
                expand_operation_traces(package, plan)
            unmapped_docs = separate_documentation_traces(package, plan)
            write_summary(directory / f"package-{attempt + 1}.json", package)
            previous_package = package
            findings = package_findings(package, paths, plan)
            if findings:
                print("  package findings: " + "; ".join(findings[:6]), flush=True)
                continue
            with tempfile.TemporaryDirectory(prefix=".candidate-", dir=directory) as temp:
                candidate = Path(temp) / stem
                materialize(candidate, package["files"])
                validation = validate_driver(candidate, stem, sdk, alias=alias)
                findings = list(validation.get("errors", []))
                if validation["ok"] and args.build_harness:
                    build = build_with_harness(
                        candidate, stem, sdk, args.build_harness, directory / "build", timeout=300
                    )
                    if not build["ok"]:
                        findings.extend(build.get("diagnostics") or build.get("errors")
                                        or [build.get("detail") or "SDK build rejected the candidate"])
                comparison = None
                if not findings and (args.compare_reference or args.reference_api):
                    comparison = compare_reference(candidate, sdk, stem)
                if not findings and args.reference_api:
                    missing = comparison.get("exact_declarations", {}).get("reference_only", [])
                    if missing:
                        findings.extend(f"Compatibility mode requires declaration {name}: {signature}"
                                        for name, signature in missing)
                if not findings:
                    artifacts = materialize(driver_root, package["files"])
                    write_summary(directory / "trace.json", {
                        "trace": package["trace"], "limitations": package["limitations"],
                        "unmapped_documentation_traces": unmapped_docs,
                    })
                    report = {"board": board, "stem": stem, "model": args.model, "status": "verified",
                              "fingerprint": signature, "artifacts": artifacts, "validation": validation,
                              "build": build, "comparison": comparison, "usage": journal,
                              "reference_api_supplied": args.reference_api}
                    write_summary(report_path, report)
                    print(f"  verified {len(artifacts)} artifacts", flush=True)
                    return 0
                print("  candidate findings: " + "; ".join(findings[:6]), flush=True)
        write_summary(report_path, {"board": board, "status": "failed", "findings": findings,
                                    "validation": validation, "build": build, "usage": journal})
        print("  validation failed: " + "; ".join(map(str, findings[:8])), flush=True)
        return 1
    except PendingCompletion as exc:
        print(f"  waiting for {exc.reply_path}", flush=True)
        return 2
    except ValueError as exc:
        write_summary(report_path, {"board": board, "status": "failed", "findings": [str(exc)], "usage": journal})
        print("  failed: " + str(exc), flush=True)
        return 1


def main(argv: list[str] | None = None) -> int:
    from .driver_sdk import resolve_sdk

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="Corpus directory or retrieval JSONL(.gz).")
    parser.add_argument("--sdk", type=Path, required=True, help="Parent of the Click and core mikroSDK checkouts.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--board", type=slug, action="append")
    selection.add_argument("--all", action="store_true", help="Run the local corpus board index sequentially.")
    parser.add_argument("--limit", type=int, help="Limit the number of selected boards.")
    parser.add_argument("--output", "-o", type=Path, default=Path("out-drivers"))
    parser.add_argument("--stem", help="Override the SDK stem for one board.")
    parser.add_argument("--provider", choices=("mimo", "exchange"), default="mimo")
    parser.add_argument("--model", default="mimo-v2.6-pro")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--attempts", type=int, default=3, help="Maximum attempts per stage.")
    parser.add_argument("--max-tokens", type=int, default=18000, help="Maximum tokens per package reply.")
    parser.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--force", action="store_true", help="Ignore saved generation checkpoints.")
    parser.add_argument("--compare-reference", action="store_true", help="Compare with the target only after validation.")
    parser.add_argument("--reviewed-plan", type=Path, help="Use a locally reviewed hardware plan for one board.")
    parser.add_argument("--reference-api", action="store_true",
                        help="Supply the reference's public declarations for explicit API compatibility.")
    parser.add_argument("--build-harness", type=Path, help="Optional trusted local firmware build harness.")
    args = parser.parse_args(argv)
    if args.attempts < 1 or args.max_tokens < 1 or (args.limit is not None and args.limit < 1):
        parser.error("Attempts, token cap and limit must be positive.")
    if args.stem and (not args.board or len(args.board) != 1 or not re.fullmatch(r"[a-z][a-z0-9_]*", args.stem)):
        parser.error("Use --stem with one board and a lowercase C identifier.")
    if args.reviewed_plan and (not args.board or len(args.board) != 1):
        parser.error("Use --reviewed-plan with one board.")
    if args.all and not args.corpus.is_dir():
        parser.error("--all requires a corpus directory with boards.jsonl.")
    try:
        boards = list(dict.fromkeys(args.board or (
            json.loads(line)["board"] for line in (args.corpus / "boards.jsonl").read_text().splitlines()
        )))
        sdk = resolve_sdk(args.sdk)
        args.output = args.output.expanduser().resolve()
        client = (MiMoClient.from_env(env_file=args.env_file, model=args.model, timeout=240)
                  if args.provider == "mimo" else FileExchangeClient(args.output / "exchange", model=args.model))
        failures = 0
        for board in boards[:args.limit]:
            result = run_board(client, args, sdk, slug(board))
            if result == 2:
                return result
            failures += result
        print(f"{len(boards[:args.limit]) - failures} verified · {failures} failed or blocked", flush=True)
        return int(bool(failures))
    except (MiMoError, OSError, ValueError) as exc:
        parser.exit(1, f"sheetforge-driver: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
