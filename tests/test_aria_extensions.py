"""Ensure the public schema and privileged validator enforce the same boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.aria_bridge import management, server

ROOT = Path(__file__).resolve().parents[1]
PHP = shutil.which("php")


@pytest.mark.parametrize(
    "schema,value,accepted",
    [
        ({"type": "integer", "minimum": 1, "maximum": 5}, True, False),
        ({"type": "integer", "minimum": 1, "maximum": 5}, 3, True),
        ({"type": "integer", "minimum": 1, "maximum": 5}, 3.5, False),
        ({"type": "string", "maxLength": 4, "maxBytes": 4}, "ééé", False),
        ({"type": "string", "maxLength": 4}, "ééé", True),
        ({"type": "string", "pattern": "^[a-z]+$"}, "abc\n", False),
        ({"type": "string", "enum": ["start", "stop"]}, "remove", False),
        ({"type": "boolean"}, "true", False),
        ({"type": "array", "items": {"type": "string"}, "maxItems": 2}, ["a", "b"], True),
        ({"type": "array", "items": {"type": "string"}, "maxItems": 2}, ["a", "b", "c"], False),
        ({"type": "array", "items": {"type": "string"}, "uniqueItems": True}, ["a", "a"], False),
        ({"type": "object", "properties": {"x": {"type": "boolean"}}}, {"x": False}, True),
        ({"type": "object", "properties": {"x": {"type": "boolean"}}}, {"extra": 1}, False),
        ({"type": "object", "properties": {}, "required": ["x"]}, {}, False),
        ({"type": "object", "additionalProperties": {"type": "integer"}}, {"port": "22"}, False),
        (
            {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
            [{"name": "app"}],
            True,
        ),
    ],
)
def test_schema_boundary_matches_php(schema, value, accepted):
    try:
        management.validate_value(value, schema)
        python_ok = True
    except management.ArgumentError:
        python_ok = False
    assert python_ok is accepted
    if PHP is None:
        pytest.skip("PHP is unavailable")
    result = subprocess.run(
        [
            PHP,
            "-r",
            "require $argv[1]; $a=json_decode(stream_get_contents(STDIN),true); try { ariaSchemaValue($a['value'],$a['schema']); echo 'ok'; } catch(Throwable $e) { echo 'invalid'; }",
            str(ROOT / "tools/aria_bridge/host-agent.php"),
        ],
        input=json.dumps({"value": value, "schema": schema}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert (result.stdout == "ok") is accepted, result.stderr


def test_extension_inventory_and_mutation_requirements():
    specs = management.extension_specs()
    assert {spec.action.split("_", 1)[0] for spec in specs} == {
        "operations",
        "diagnostics",
        "applications",
        "automation",
        "deployment",
    }
    assert len({spec.action for spec in specs}) == len(specs)
    assert len({spec.name for spec in specs}) == len(specs)
    definitions = {tool["name"]: tool for tool in server.tool_definitions()}
    for spec in specs:
        assert spec.name in definitions
        if not spec.read_only:
            assert "request_id" in spec.required
            with pytest.raises(management.ArgumentError):
                management.validate_arguments(spec, {"request_id": "../escape"})


def test_frontend_docker_build_includes_every_manifest():
    directory = ROOT / "tools/aria_bridge"
    dockerfile = (directory / "Dockerfile").read_text()
    assert "*-tools.json" in dockerfile
    assert "!*-tools.json" in (directory / ".dockerignore").read_text()
