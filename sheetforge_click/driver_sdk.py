"""Read mikroSDK examples and check generated Click driver packages locally."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


_STEM = re.compile(r"[a-z0-9_]+")
_ALIAS = re.compile(r"Click\.[A-Za-z0-9_]+")
_SDK_BUS_HEADERS = {
    "i2c": "drv_i2c_master.h",
    "spi": "drv_spi_master.h",
    "uart": "drv_uart.h",
    "analog": "drv_analog_in.h",
    "gpio": "drv_digital_in.h",
    "pwm": "drv_pwm.h",
    "onewire": "drv_one_wire.h",
}
_COMMON_EXEMPLARS = {
    "i2c": ("ambient22",),
    "spi": ("pwrmeter2",),
    "uart": ("uartmux",),
    "gpio": ("relay", "reed"),
}
_INCLUDE = re.compile(r"(?m)^\s*#\s*(include(?:_next)?|import|embed)\b([^\n]*)")
_FUNCTION = re.compile(
    r"(?m)^\s*((?:[a-zA-Z_]\w*\s+){1,8}\**\s*)"
    r"([a-zA-Z_]\w*)\s*\(([^;{}]*)\)\s*([;{])"
)
_INTEGER_DEFINE = re.compile(
    r"(?m)^\s*#\s*define\s+(\w+)\s+\(*\s*"
    r"([+-]?(?:0[xX][0-9a-fA-F]+|0[bB][01]+|[0-9]+))[uUlL]*\s*\)*\s*$"
)
_C_COMMENT = re.compile(r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|/\*.*?\*/|//[^\n]*""", re.S)


@dataclass(frozen=True)
class SDK:
    clicks: Path
    core: Path | None


def resolve_sdk(root: Path) -> SDK:
    """Resolve a Click library checkout, its clicks directory, or their parent."""
    root = root.expanduser().resolve()
    clicks = None
    for candidate in (
        root / "mikrosdk_click_v2" / "clicks",
        root / "mikrosdk-click-v2" / "clicks",
        root / "clicks",
        root,
    ):
        if candidate.is_dir() and (
            candidate.name == "clicks"
            or any(directory.is_dir() and (directory / f"lib_{directory.name}").is_dir()
                   for directory in candidate.iterdir())
        ):
            clicks = candidate
            break
    if clicks is None:
        raise ValueError(f"No mikroSDK Click library checkout found at {root}")
    core = next((candidate for base in (root, root.parent, clicks.parent.parent)
                 for candidate in (base / "mikrosdk_v2", base / "mikrosdk-v2", base)
                 if (candidate / "drv/lib/include").is_dir() and (candidate / "hal").is_dir()), None)
    return SDK(clicks=clicks, core=core)


def layout(stem: str, alias: str | None = None) -> tuple[str, ...]:
    """Return the portable artifact paths of a conventional Click package."""
    alias = alias or f"Click.{stem}"
    if not _STEM.fullmatch(stem) or not _ALIAS.fullmatch(alias):
        raise ValueError(f"Invalid Click C stem or import alias: {stem!r}, {alias!r}")
    lib = f"lib_{stem}"
    return (
        "README.md",
        "CMakeLists.txt",
        f"{lib}/CMakeLists.txt",
        f"{lib}/include/{alias}",
        f"{lib}/include/{stem}.h",
        f"{lib}/src/{stem}.c",
        "example/CMakeLists.txt",
        "example/main.c",
        "example/manifest.exm",
        "doc/package/manifest.json",
        "doc/doxy/Doxyfile.doxy",
    )


def _identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"(?i)^(?:click\.)|(?:[\s_-]+click)$", "", value).lower())


def _family(value: str) -> str:
    return re.sub(r"\d+$", "", _identifier(value)) or _identifier(value)


def _interfaces(interface: str | Iterable[str] | None) -> set[str]:
    if interface is None:
        return set()
    if not isinstance(interface, str):
        return {bus for item in interface for bus in _interfaces(item)}
    value = re.sub(r"(?i)i[²2]c", "i2c", interface.lower())
    names = set(re.findall(r"[a-z0-9]+", value))
    names.update({"analog"} if "adc" in names else ())
    names.update({"uart"} if names & {"usart", "serial"} else ())
    names.update({"gpio"} if names & {"digital", "gpio"} else ())
    if "1wire" in names or "one-wire" in value or "one wire" in value:
        names.add("onewire")
    return names & _SDK_BUS_HEADERS.keys()


def _without_comments(source: str) -> str:
    return _C_COMMENT.sub(lambda match: match[1] or (" " + "\n" * match[0].count("\n")), source)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _exemplar_snippets(directory: Path, stem: str, header: str) -> dict[str, str]:
    lib = f"lib_{stem}"
    snippets = {f"{lib}/include/{stem}.h": header[:3800]}
    for relative, max_chars in (
        (f"{lib}/src/{stem}.c", 4000),
        ("example/main.c", 3000),
        (f"{lib}/CMakeLists.txt", 1500),
    ):
        source = directory / relative
        if source.is_file():
            text = _read(source)
            if source.suffix == ".c":
                text = _without_comments(text).strip()
            snippets[relative] = text[:max_chars]
    return snippets


def exemplars(
    sdk: SDK, stem: str, interface: str | Iterable[str] | None,
    *, limit: int = 2, exclude: Iterable[str] = (),
) -> list[dict]:
    """Return short same-interface examples from different Click families."""
    if limit < 1:
        return []
    requested = _interfaces(interface)
    excluded = {_family(value) for value in (stem, *exclude)}
    preferred = tuple(dict.fromkeys(
        name for bus in sorted(requested or {"i2c", "spi", "uart", "gpio"})
        for name in _COMMON_EXEMPLARS.get(bus, ())
    ))
    candidates = [sdk.clicks / name for name in preferred]
    candidates.extend(sorted(sdk.clicks.iterdir()))
    checked = set()
    selected = []
    clicks_root = sdk.clicks.resolve()
    for candidate in candidates:
        real = candidate.resolve()
        if (
            real in checked or not real.is_relative_to(clicks_root)
            or _family(candidate.name) in excluded or _family(real.name) in excluded
        ):
            continue
        checked.add(real)
        header_dir = real / f"lib_{real.name}/include"
        header_path = header_dir / f"{real.name}.h"
        if not header_path.is_file():
            continue
        # Some repos alias library directories or umbrella headers.
        if not header_path.resolve().is_relative_to(real):
            continue
        alias_names = {path.name for path in header_dir.glob("Click.*")}
        if any(_family(value) in excluded for value in alias_names):
            continue
        source_path = real / f"lib_{real.name}/src/{real.name}.c"
        if not source_path.is_file() or not source_path.resolve().is_relative_to(real):
            continue
        header = _without_comments(_read(header_path)).strip()
        buses = {bus for bus, filename in _SDK_BUS_HEADERS.items()
                 if re.search(r'[#]\s*include\s+["<]' + re.escape(filename) + r'[">]', header)}
        if requested and not requested & buses:
            if not ("gpio" in requested and "drv_digital_out.h" in header):
                continue
        if re.search(r"\b" + re.escape(stem) + r"_\w+", _without_comments(_read(source_path)), re.I):
            continue
        selected.append({
            "stem": real.name,
            "interfaces": sorted(buses),
            "files": _exemplar_snippets(real, real.name, header),
        })
        excluded.add(_family(real.name))
        if len(selected) == limit:
            break
    return selected


def _functions(text: str, stem: str, marker: str, *, public: bool = False) -> dict[str, str]:
    return {
        name: " ".join((return_type + " " + parameters).split())
        for return_type, name, parameters, end in _FUNCTION.findall(_without_comments(text))
        if end == marker and name.startswith(stem + "_") and
        (not public or "static" not in return_type.split())
    }


def _defines(text: str, stem: str) -> dict[str, int]:
    values = {}
    for name, value in _INTEGER_DEFINE.findall(_without_comments(text)):
        if name.startswith(stem.upper() + "_"):
            digits = value.lstrip("+-")
            base = 8 if re.fullmatch(r"0[0-7]+", digits) else (
                0 if digits[:2].lower() in {"0x", "0b"} else 10
            )
            values[name] = int(value, base)
    return values


def _file_contents(path: Path, errors: list[str], relative: str) -> str | None:
    if path.is_symlink() or not path.is_file():
        errors.append(f"Missing regular artifact {relative}.")
        return None
    text = path.read_text(encoding="utf-8")
    if not text.strip() or "\0" in text:
        errors.append(f"Invalid text artifact {relative}.")
        return None
    return text


def _check_includes(body: str, relative: str, errors: list[str]) -> None:
    code = re.sub(r"\\\r?\n", "", _without_comments(body))
    for directive, value in _INCLUDE.findall(code):
        target = re.fullmatch(r'\s*(?:<([A-Za-z0-9_./+\-]+)>|"([A-Za-z0-9_./+\-]+)")\s*', value)
        path = Path((target[1] or target[2]) if target else "/")
        if directive != "include" or path.is_absolute() or ".." in path.parts:
            errors.append(f"{relative} has unsupported #{directive} {value.strip()}.")


def validate_driver(
    root: Path, stem: str, sdk: SDK | None = None, *, alias: str | None = None,
) -> dict:
    """Check package paths, C interfaces and NECTO metadata without running CMake."""
    alias = alias or f"Click.{stem}"
    paths = layout(stem, alias)
    root = root.resolve()
    errors = []
    warnings = []
    files = {}
    for name in paths:
        path = root / name
        try:
            if not path.resolve().is_relative_to(root):
                errors.append(f"Artifact leaves the package directory: {name}.")
            elif (content := _file_contents(path, errors, name)) is not None:
                files[name] = content
        except (OSError, UnicodeError) as exc:
            errors.append(f"Cannot read artifact {name}: {exc}.")
    for name, body in files.items():
        if name.endswith((".c", ".h")) or name.endswith("/" + alias):
            _check_includes(body, name, errors)
        if name.endswith("CMakeLists.txt") and re.search(
            r"(?i)\b(?:(?:execute_process|add_custom_command|add_custom_target|fetchcontent_declare|"
            r"externalproject_add)\s*\(|file\s*\(\s*download\b)", body
        ):
            errors.append(f"{name} defines external operations outside the SDK package layout.")
    lib = f"lib_{stem}"
    header = files.get(f"{lib}/include/{stem}.h", "")
    source = files.get(f"{lib}/src/{stem}.c", "")
    declared = _functions(header, stem, ";")
    defined = _functions(source, stem, "{", public=True)
    if header and source:
        for function in sorted(declared.keys() - defined.keys()):
            errors.append(f"Header declares {function}, which has no public definition.")
        for function in sorted(defined.keys() - declared.keys()):
            warnings.append(f"Public definition {function} has no header declaration.")
        for suffix in ("_cfg_setup", "_init"):
            if stem + suffix not in declared:
                errors.append(f"Missing public entry point {stem}{suffix}.")
        if f"{stem.upper()}_MAP_MIKROBUS" not in header:
            errors.append(f"Missing {stem.upper()}_MAP_MIKROBUS socket mapping.")
    if import_header := files.get(f"{lib}/include/{alias}"):
        if f'"{stem}.h"' not in import_header and f"<{stem}.h>" not in import_header:
            errors.append(f"Import marker {alias} needs to include {stem}.h.")
    if library_cmake := files.get(f"{lib}/CMakeLists.txt"):
        if alias not in library_cmake or f"src/{stem}.c" not in library_cmake:
            errors.append(f"Library CMake needs to define {alias} and compile src/{stem}.c.")
    if example := files.get("example/main.c"):
        for function in ("application_init", "application_task", "main"):
            if not re.search(rf"\b{function}\s*\([^;]*?\)\s*\{{", _without_comments(example)):
                errors.append(f"Example needs a definition of {function}.")
    manifests = {}
    for filename in ("example/manifest.exm", "doc/package/manifest.json"):
        if body := files.get(filename):
            try:
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                manifests[filename] = data
            except ValueError as exc:
                errors.append(f"Invalid JSON in {filename}: {exc}.")
    if example_metadata := manifests.get("example/manifest.exm"):
        for key in ("name", "toolchain", "hw"):
            if not example_metadata.get(key):
                errors.append(f"Example manifest requires {key}.")
    if metadata := manifests.get("doc/package/manifest.json"):
        if metadata.get("type") != "click" or metadata.get("name") != f"mikroe.click.{stem}":
            errors.append(f"Package manifest needs type 'click' and name 'mikroe.click.{stem}'.")
        contents = metadata.get("contents")
        libraries = contents.get("libraries", []) if isinstance(contents, dict) else []
        if not isinstance(libraries, list) or not any(
            isinstance(library, dict)
            and library.get("alias") == alias and library.get("subdir_name") == lib
            for library in libraries
        ):
            errors.append(f"Package manifest needs library {alias} with subdir_name {lib}.")
    if sdk and sdk.core:
        for header_name in sorted(set(re.findall(r'["<](drv_[A-Za-z0-9_]+\.h)[">]', header + source))):
            if not (sdk.core / "drv/lib/include" / header_name).is_file():
                warnings.append(f"Driver header {header_name} is absent from the selected core SDK.")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": {
            "required_artifacts": len(paths),
            "readable_artifacts": len(files),
            "public_declarations": sorted(declared),
            "public_definitions": sorted(defined),
        },
    }


def _overlap(generated: set, reference: set) -> dict:
    shared = sorted(generated & reference)
    return {
        "matched": shared,
        "generated_count": len(generated),
        "reference_count": len(reference),
        "reference_recall": round(len(shared) / len(reference), 4) if reference else None,
        "generated_precision": round(len(shared) / len(generated), 4) if generated else None,
        "reference_only": sorted(reference - generated),
        "generated_only": sorted(generated - reference),
    }


def _reference_alias(directory: Path, stem: str) -> str:
    aliases = sorted((directory / f"lib_{stem}/include").glob("Click.*"))
    return aliases[0].name if len(aliases) == 1 else f"Click.{stem}"


def reference_api(sdk: SDK, stem: str) -> dict:
    """Read only the optional public interface contract, never the target source."""
    layout(stem)
    header = sdk.clicks / stem / f"lib_{stem}" / "include" / f"{stem}.h"
    if not header.is_file():
        raise ValueError(f"No reference header exists for {stem}.")
    text = _without_comments(_read(header))
    declarations = _functions(text, stem, ";")
    if not declarations:
        raise ValueError(f"No public SDK declarations were found for {stem}.")
    types = re.findall(r"typedef\s+struct(?:\s+\w+)?\s*\{[^{}]*\}\s*\w+\s*;", text)
    prototypes = {
        name: f"{return_type.strip()} {name} ( {parameters.strip()} );"
        for return_type, name, parameters, end in _FUNCTION.findall(text)
        if end == ";" and name in declarations
    }
    return {"declarations": declarations, "types": types, "prototypes": prototypes}


def compare_reference(generated: Path, sdk: SDK, stem: str) -> dict:
    """Benchmark the public C symbols and numeric defines against a held-out package."""
    layout(stem)
    reference = sdk.clicks / stem
    relative_header = f"lib_{stem}/include/{stem}.h"
    relative_source = f"lib_{stem}/src/{stem}.c"
    if not (reference / relative_header).is_file() or not (reference / relative_source).is_file():
        return {"available": False, "reason": f"No reference SDK package for {stem}."}
    if not (generated / relative_header).is_file() or not (generated / relative_source).is_file():
        return {"available": False, "reason": "The generated driver source or header is missing."}
    alias = _reference_alias(reference, stem)
    gen_alias = _reference_alias(generated, stem)
    ref_header = _read(reference / relative_header)
    gen_header = _read(generated / relative_header)
    ref_functions = _functions(ref_header, stem, ";")
    gen_functions = _functions(gen_header, stem, ";")
    ref_defines = _defines(ref_header, stem)
    gen_defines = _defines(gen_header, stem)
    def code_tokens(path: Path) -> list[str]:
        return re.findall(r"\w+|[^\s\w]", _without_comments(_read(path)))

    token_similarity = SequenceMatcher(
        None, code_tokens(reference / relative_source), code_tokens(generated / relative_source)
    ).ratio()
    return {
        "available": True,
        "reference_path": str(reference),
        "reference_c_sha256": hashlib.sha256((reference / relative_source).read_bytes()).hexdigest(),
        "reference_alias": alias,
        "generated_alias": gen_alias,
        "package_paths": _overlap(
            {path for path in layout(stem, gen_alias) if (generated / path).is_file()},
            {path for path in layout(stem, alias) if (reference / path).is_file()},
        ),
        "functions": _overlap(set(gen_functions), set(ref_functions)),
        "exact_declarations": _overlap(set(gen_functions.items()), set(ref_functions.items())),
        "numeric_macros": _overlap(set(gen_defines.items()), set(ref_defines.items())),
        "same_name_numeric_mismatches": {
            name: {"generated": gen_defines[name], "reference": ref_defines[name]}
            for name in sorted(gen_defines.keys() & ref_defines.keys())
            if gen_defines[name] != ref_defines[name]
        },
        "numeric_value_overlap": _overlap(set(gen_defines.values()), set(ref_defines.values())),
        "normalized_c_token_similarity": round(token_similarity, 4),
    }


def build_with_harness(
    root: Path, stem: str, sdk: SDK, builder: Path, output: Path, *, timeout: int = 180,
) -> dict:
    """Use an explicitly selected local ARM build harness after static checks."""
    root = root.resolve()
    if root.name != stem:
        return {"ok": False, "detail": f"Build input directory must be named {stem}."}
    if sdk.core is None:
        return {"ok": False, "detail": "No core SDK checkout was found."}
    if (output / stem).resolve() == root:
        return {"ok": False, "detail": "Build reports need a directory separate from the driver package."}
    validation = validate_driver(root, stem, sdk, alias=_reference_alias(root, stem))
    if not validation["ok"]:
        return {"ok": False, "detail": "Static checks failed.", "errors": validation["errors"]}
    try:
        command = [
            sys.executable, str(builder.resolve(strict=True)), "--corpus", str(root.parent),
            "--sdk", str(sdk.core), "--case", stem, "--out", str(output.resolve()),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": str(exc)}
    driver_report = output / stem / "build.json"
    report = json.loads(_read(driver_report)) if driver_report.is_file() else {}
    log = output / stem / "build.log"
    diagnostics = []
    if log.is_file():
        lines = [line for line in _read(log).splitlines()
                 if re.search(r":\s*(?:error|warning|note|fatal error):|undefined reference", line)]
        lines.sort(key=lambda line: (
            not re.search(r":\s*(?:fatal )?error:|undefined reference", line), not line.startswith(str(root)),
        ))
        diagnostics = [line.replace(str(root) + "/", "")[:320]
                       for line in dict.fromkeys(lines)]
    return {
        "ok": result.returncode == 0 and report.get("stages", {}).get("artifacts") == "passed",
        "returncode": result.returncode,
        "stages": report.get("stages", {}),
        "diagnostics": diagnostics[:30],
        "detail": report.get("detail") or result.stderr.strip()[-1000:],
        "report": str(driver_report) if driver_report.is_file() else None,
    }


def package_manifests(files: dict, stem: str, alias: str, profile: str, component: str) -> None:
    """Fill the NECTO packaging contract from local identity and SDK conventions."""
    layout(stem, alias)
    display_name = next((value[2:].strip() for value in profile.splitlines()
                         if value.startswith("# ")), stem)
    product_id = re.search(r"\bMIKROE-\d+\b", profile)
    library = f"lib_{stem}"
    example = {
        "name": display_name,
        "toolchain": ["gcc-arm"],
        "hw": ["Click", component],
    }
    manifest = {
        "display_name": display_name,
        "name": f"mikroe.click.{stem}",
        "type": "click",
        "version": "0.1.0",
        "package_changed": True,
        "contents": {
            "examples": [{"path": f"{display_name}/examples/example"}],
            "libraries": [{"alias": alias, "subdir_name": library,
                           "path": f"{display_name}/data/{library}"}],
        },
    }
    if product_id:
        manifest["pid"] = product_id[0]
    files["example/manifest.exm"] = json.dumps(example, ensure_ascii=False, indent=2) + "\n"
    files["doc/package/manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def core_api(sdk: SDK, interface: str) -> dict[str, str]:
    """Expose the selected SDK's actual declarations, constants, and types."""
    if sdk.core is None:
        return {}
    headers = {_SDK_BUS_HEADERS[bus] for bus in _interfaces(interface)}
    headers.update(("drv_digital_in.h", "drv_digital_out.h"))
    include = sdk.core / "drv/lib/include"
    return {
        name: re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n",
                     _without_comments(_read(include / name))).strip()
        for name in sorted(headers) if (include / name).is_file()
    }


def complete_reference_declarations(files: dict, stem: str, contract: dict) -> None:
    """Insert a withheld header declaration only when its matching definition exists."""
    lib = f"lib_{stem}"
    path = f"{lib}/include/{stem}.h"
    header = files.get(path)
    source = files.get(f"{lib}/src/{stem}.c")
    if not isinstance(header, str) or not isinstance(source, str):
        return
    declared = _functions(header, stem, ";")
    defined = _functions(source, stem, "{", public=True)
    additions = [
        prototype for name, prototype in contract["prototypes"].items()
        if name not in declared and defined.get(name) == contract["declarations"][name]
    ]
    if additions:
        closing = list(re.finditer(r"(?m)^#\s*ifdef\s+__cplusplus\s*\n\s*\}", header))
        position = closing[-1].start() if closing else header.rfind("#endif")
        if position >= 0:
            files[path] = header[:position] + "\n".join(additions) + "\n\n" + header[position:]
