from sheetforge_click.driver_contract import decode_reply, package_findings, plan_findings


def plan():
    return {
        "board": "example-click", "component": "EX01", "interface": "I2C",
        "claims": [{
            "name": "read_address", "value": "0x2E", "evidence": "D01P1",
            "quote": "The read address is 0x2E.",
        }],
        "operations": [{
            "name": "read", "description": "Read status.", "claims": ["read_address"],
        }],
        "unresolved": [],
    }


def test_plan_requires_literal_source_and_valid_claims():
    evidence = {"D01P1": "Protocol: The read\n address is 0x2E."}
    assert not plan_findings(plan(), "example-click", evidence)
    assert plan_findings(plan(), "example-click", {"D01P1": "Read address 0x2F."})
    broken = plan()
    broken["operations"][0]["claims"] = ["unverified"]
    assert plan_findings(broken, "example-click", evidence)


def test_package_rejects_extra_paths_and_unbound_symbols():
    package = decode_reply('''```json
{
  "files": {"driver.c":"#define ADDRESS 0x2E"},
  "trace": [{"claim":"read_address","path":"driver.c","symbol":"ADDRESS"}],
  "limitations": []
}
```''')
    assert not package_findings(package, ("driver.c",), plan())
    package["files"]["../.env"] = "overwritten"
    package["trace"][0]["symbol"] = "UNBOUND"
    errors = package_findings(package, ("driver.c",), plan())
    assert any("Unexpected file path" in error for error in errors)
    assert any("Invalid trace" in error for error in errors)


def test_operation_trace_expands_only_to_approved_claims():
    from sheetforge_click.driver_contract import expand_operation_traces

    package = {
        "files": {"driver.c": "#define ADDRESS 0x2E"},
        "trace": [{"claim": "read", "path": "driver.c", "symbol": "ADDRESS"}],
        "limitations": [],
    }
    expand_operation_traces(package, plan())
    assert [row["claim"] for row in package["trace"]] == ["read_address"]
    assert not package_findings(package, ("driver.c",), plan())


def test_unmapped_prose_is_reported_without_bypassing_code_checks():
    from sheetforge_click.driver_contract import separate_documentation_traces

    package = {
        "files": {"driver.c": "#define ADDRESS 0x2E", "README.md": "I2C device"},
        "trace": [
            {"claim": "read_address", "path": "README.md", "symbol": "Missing heading"},
            {"claim": "read_address", "path": "driver.c", "symbol": "STILL_MISSING"},
        ],
        "limitations": [],
    }
    notes = separate_documentation_traces(package, plan())
    assert [row["path"] for row in notes] == ["README.md"]
    issues = package_findings(package, ("driver.c", "README.md"), plan())
    assert issues == ["Invalid trace: read_address -> driver.c::STILL_MISSING"]
