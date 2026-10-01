"""Protect the public command surface and existing Config namespace/defaults."""

import importlib
import inspect
import json
from pathlib import Path

import pytest

BASELINE = json.loads(Path(__file__).with_name("compatibility.json").read_text())
# Intentional public renames. Keep the original snapshot for all other compatibility checks.
RENAMED_ROOTS = {"com": "community", "logplus": "log", "owoplus": "owo"}


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
        command = roots[RENAMED_ROOTS.get(root, root)]
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
    defaults = cog.config.defaults
    if package == "audioplus":
        # The opt-in watchdog adds one Config section. All legacy defaults stay exact.
        assert "watchdog" in defaults["GLOBAL"]
        defaults["GLOBAL"].pop("watchdog")
        from audioplus.features import DEFAULTS_GUILD

        assert defaults.pop("GUILD") == DEFAULTS_GUILD
    if package == "levelplus":
        from levelplus.features import FEATURE_DEFAULTS_GUILD

        for key, value in FEATURE_DEFAULTS_GUILD.items():
            assert defaults["GUILD"].pop(key) == value
    if package == "owoplus":
        from owoplus.features import FEATURE_DEFAULTS

        assert defaults["GUILD"].pop("features") == FEATURE_DEFAULTS
    if package == "communityplus":
        from communityplus.features import FEATURE_DEFAULTS, PARTICIPATION_DEFAULTS

        assert defaults["GUILD"].pop("features") == FEATURE_DEFAULTS
        assert defaults["MEMBER"].pop("participation") == PARTICIPATION_DEFAULTS
    assert defaults == BASELINE[package]["defaults"]
