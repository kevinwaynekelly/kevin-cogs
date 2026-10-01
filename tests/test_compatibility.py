"""Protect the public command surface and existing Config namespace/defaults."""

import importlib
import inspect
import json
from pathlib import Path

import pytest

BASELINE = json.loads(Path(__file__).with_name("compatibility.json").read_text())


@pytest.mark.parametrize("package", list(BASELINE))
def test_existing_commands_and_permissions_remain_compatible(package, bot):
    cls = getattr(importlib.import_module(package), BASELINE[package]["class"])
    cog = cls(bot)
    roots = {}
    for command in cog.get_commands():
        for name in [command.name, *command.aliases]:
            roots[name] = command
    for saved in BASELINE[package]["commands"]:
        root, *rest = saved["name"].split()
        command = roots[root]
        for part in rest:
            command = command.get_command(part)
            assert command is not None, saved["name"]
        assert len(command.checks) == saved["checks"], saved["name"]
        actual = [
            (p.name, str(p.annotation))
            for p in list(inspect.signature(command.callback).parameters.values())[2:]
        ]
        assert actual == [tuple(p) for p in saved["parameters"]], saved["name"]
        for alias in saved["aliases"]:
            parent = command.parent
            if parent:
                assert parent.get_command(alias) is command
            else:
                assert roots[alias] is command


@pytest.mark.parametrize("package", list(BASELINE))
async def test_saved_configuration_namespace_and_defaults(package, bot):
    cls = getattr(importlib.import_module(package), BASELINE[package]["class"])
    cog = cls(bot)
    assert int(cog.config.unique_identifier.split(":")[0]) == BASELINE[package]["identifier"]
    assert cog.config.defaults == BASELINE[package]["defaults"]
