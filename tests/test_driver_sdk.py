"""Synthetic mikroSDK packages for the generation harness checks."""

import json
from pathlib import Path

import pytest

from sheetforge_click.driver_sdk import compare_reference, exemplars, layout, resolve_sdk, validate_driver


def make_driver(clicks: Path, stem: str, *, address: str = "0x2e", alias: str | None = None) -> Path:
    alias = alias or f"Click.{stem}"
    root = clicks / stem
    lib = f"lib_{stem}"
    files = {
        f"{lib}/include/{stem}.h": f"""#include "drv_i2c_master.h"
#define {stem.upper()}_ADDRESS {address}
#define {stem.upper()}_MAP_MIKROBUS(cfg, socket)
void {stem}_cfg_setup(void);
int {stem}_init(void);
""",
        f"{lib}/src/{stem}.c": f"""#include "{stem}.h"
void {stem}_cfg_setup(void) {{ }}
int {stem}_init(void) {{ return 0; }}
""",
        f"{lib}/CMakeLists.txt": f"add_library({lib} STATIC src/{stem}.c)\nadd_library({alias} ALIAS {lib})\n",
        f"{lib}/include/{alias}": f'#include "{stem}.h"\n',
        "example/main.c": f"""#include "{stem}.h"
void application_init(void) {{ {stem}_cfg_setup(); }}
void application_task(void) {{ }}
int main(void) {{ application_init(); application_task(); return 0; }}
""",
        "example/manifest.exm": json.dumps({"name": stem, "toolchain": ["gcc-arm"], "hw": ["Click"]}),
        "doc/package/manifest.json": json.dumps({
            "type": "click", "name": f"mikroe.click.{stem}",
            "contents": {"libraries": [{"alias": alias, "subdir_name": lib}]},
        }),
    }
    for filename in layout(stem, alias):
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(files.get(filename, "Fixture.\n"), encoding="utf-8")
    return root


def test_sdk_selection_excludes_target_and_same_family(tmp_path):
    clicks = tmp_path / "mikrosdk_click_v2/clicks"
    make_driver(clicks, "example")
    make_driver(clicks, "example2")
    make_driver(clicks, "alternate", alias="Click.Example")
    make_driver(clicks, "unrelated")
    sdk = resolve_sdk(tmp_path)
    assert sdk.clicks == clicks
    assert [row["stem"] for row in exemplars(sdk, "example", "I²C")] == ["unrelated"]


def test_package_validation_and_heldout_comparison(tmp_path):
    reference = make_driver(tmp_path / "sdk/clicks", "sample", address="056")
    generated = make_driver(tmp_path / "candidates", "sample", address="0X2E")
    sdk = resolve_sdk(reference.parent)
    result = validate_driver(generated, "sample", sdk)
    assert result["ok"]
    assert result["checks"]["readable_artifacts"] == len(layout("sample"))
    comparison = compare_reference(generated, sdk, "sample")
    assert comparison["functions"]["reference_recall"] == 1
    assert comparison["numeric_macros"]["reference_recall"] == 1
    assert comparison["same_name_numeric_mismatches"] == {}


def test_validation_rejects_dynamic_cmake_and_escaping_include(tmp_path):
    generated = make_driver(tmp_path, "sample")
    source = generated / "lib_sample/src/sample.c"
    source.write_text(source.read_text() + '#include "../local.h"\n', encoding="utf-8")
    (generated / "CMakeLists.txt").write_text('file(DOWNLOAD "https://example.org/a" "a")\n', encoding="utf-8")
    result = validate_driver(generated, "sample")
    assert not result["ok"]
    assert any("unsupported #include" in error for error in result["errors"])
    assert any("external operations" in error for error in result["errors"])
    with pytest.raises(ValueError):
        layout("../../sample")


def test_compatibility_header_completion_requires_matching_definition(tmp_path):
    from sheetforge_click.driver_sdk import complete_reference_declarations, reference_api

    make_driver(tmp_path / "sdk/clicks", "sample")
    sdk = resolve_sdk(tmp_path / "sdk")
    contract = reference_api(sdk, "sample")
    files = {
        "lib_sample/include/sample.h": "#ifndef SAMPLE_H\n#define SAMPLE_H\n#endif\n",
        "lib_sample/src/sample.c": "void sample_cfg_setup(void) { }\n",
    }
    complete_reference_declarations(files, "sample", contract)
    assert "sample_cfg_setup" in files["lib_sample/include/sample.h"]
    assert "sample_init" not in files["lib_sample/include/sample.h"]
    assert files["lib_sample/include/sample.h"].endswith("#endif\n")
