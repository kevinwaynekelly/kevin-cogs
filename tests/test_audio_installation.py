"""Exercise Red's update gate and file copy with native libraries already installed."""

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from redbot.cogs.downloader.downloader import Downloader
from redbot.cogs.downloader.installable import Installable, InstalledModule


@pytest.mark.parametrize("media_install_succeeds", [True, False])
async def test_downloader_updates_without_reinstalling_native_voice(
    monkeypatch, tmp_path, media_install_succeeds
):
    source = Path(__file__).parents[1] / "audioplus"
    repo_source = tmp_path / "repos" / "kevin" / "audioplus"
    shutil.copytree(source, repo_source, ignore=shutil.ignore_patterns("__pycache__"))
    installed_path = tmp_path / "installed"
    old_cog = installed_path / "audioplus"
    old_cog.mkdir(parents=True)
    (old_cog / "cog.py").write_text("# Older installed command implementation\n")

    async def install_requirements(requirements, target):
        # Reproduce a working bot whose Downloader cannot reinstall native wheels.
        assert target == tmp_path / "Downloader" / "lib"
        native = {"pynacl", "davey"}
        names = {canonicalize_name(Requirement(req).name) for req in requirements}
        return media_install_succeeds and not names.intersection(native)

    repo = SimpleNamespace(
        commit="new-revision",
        checkout=AsyncMock(),
        install_raw_requirements=AsyncMock(side_effect=install_requirements),
    )
    cog = Installable(repo_source, repo=repo, commit=repo.commit)
    current = InstalledModule.from_installable(cog)
    monkeypatch.setattr(
        "redbot.cogs.downloader.downloader.cog_data_path", lambda _: tmp_path / "Downloader"
    )
    bot = SimpleNamespace(list_enabled_app_commands=AsyncMock(return_value={}))
    downloader = Downloader(bot)
    downloader._repo_manager._repos["kevin"] = repo
    monkeypatch.setattr(downloader, "cog_install_path", AsyncMock(return_value=installed_path))
    # Shared libraries are unrelated to this cog; preserve the real requirement/cog flow.
    monkeypatch.setattr(downloader, "_reinstall_libraries", AsyncMock(return_value=((), ())))

    updated, message = await downloader._update_cogs_and_libs(
        SimpleNamespace(clean_prefix="!", prefix="!"), [cog], [], [current]
    )

    attempted = {
        canonicalize_name(Requirement(req).name)
        for call in repo.install_raw_requirements.await_args_list
        for req in call.args[0]
    }
    assert attempted == {"yt-dlp"}
    if media_install_succeeds:
        assert updated == {"audioplus"}
        assert "completed successfully" in message
        assert (old_cog / "cog.py").read_bytes() == (source / "cog.py").read_bytes()
        assert (await downloader.config.installed_cogs())["kevin"]["audioplus"][
            "commit"
        ] == repo.commit
        assert repo.checkout.await_count == 2
    else:
        assert not updated
        assert "Failed to install" in message
        assert (old_cog / "cog.py").read_text().startswith("# Older installed")
        assert not await downloader.config.installed_cogs()
        repo.checkout.assert_not_awaited()
