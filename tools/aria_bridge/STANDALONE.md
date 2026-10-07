# Dedicated Aria repository

The standalone migration targets
<https://github.com/kevinwaynekelly/aria-gpt-bridge>.
The separate repository retains `tools/aria_bridge/` so existing installer,
test and recovery paths remain consistent. It contains the bridge application
and its tests, rather than the Discord cogs.

## Migrate an existing installation

Publish the dedicated repository before running the migration. From the clean
original `kevin-cogs` checkout on Aria, run:

```bash
cd /mnt/user/appdata/aria-gpt-bridge/source
git pull --ff-only
bash tools/aria_bridge/migrate-repository.sh
```

The script clones only `kevinwaynekelly/aria-gpt-bridge` branch `main` into a
temporary sibling directory. It validates the clone, reserves the installer
and management queue, and refuses queued, running or uncertain jobs. The clone
becomes `/mnt/user/appdata/aria-gpt-bridge/source-standalone`. Any directory
already at that destination is preserved and causes migration to stop.

The bridge briefly disconnects while the host service and container switch to
the dedicated source. The existing upgrade procedure preserves the actual
tunnel ID, credential files, Red endpoint, mounted status directory and
resource limits. Appdata, management settings, jobs and request receipts stay
in the same location. The boot launcher is updated to the new source path.

The original source checkout is left intact with its original Git remote and
history. Do not change its remote to the standalone repository and attempt a
pull across the two unrelated histories. After migration, future manual updates
use:

```bash
cd /mnt/user/appdata/aria-gpt-bridge/source-standalone
git pull --ff-only
bash tools/aria_bridge/upgrade-unraid.sh
```

For an existing installation with a custom appdata directory, set
`ARIA_APPDATA_ROOT` to that same directory. By default the script discovers the
appdata parent from the installed container's `/status` bind mount.

## Recovery and retries

If cloning, validation or the idle check fails, the running bridge is unchanged.
If the new upgrade fails after host shutdown, its installer attempts container
rollback and the migration restores the original host-service and boot paths.
Both source checkouts are retained for inspection after an attempted upgrade.
Recovery is reported as incomplete if either host-service recovery command
fails; a retained source checkout is not proof that the old container was
restored successfully. Inspect the installer output and container state.

To retry a failed migration, inspect the retained `source-standalone` directory
and rename it to a recovery name before running the migration again. Do not
delete it while a host service or boot launcher still refers to it.

The existing source can restart the previous host implementation manually:

```bash
cd /mnt/user/appdata/aria-gpt-bridge/source
bash tools/aria_bridge/host-service.sh restart
bash tools/aria_bridge/host-service.sh install-boot
```

Changing host-service paths alone does not change the container image. The
upgrade script retains an original container during replacement and reports
its rollback outcome. If further container recovery is necessary, inspect the
retained `aria-gpt-bridge-rollback-*` container before removing anything.

## Update trust

The remote updater accepts only the two fixed repositories owned by
`kevinwaynekelly`, `aria-gpt-bridge` and transitional `kevin-cogs`, using the
documented HTTPS or GitHub SSH forms. It still requires a clean `main` checkout
and a fast-forward. No client can select a different repository or branch.

Scheduled updates record the revision whose checks passed. The detached
updater fetches `main` again and verifies that its SHA still equals that checked
revision before changing the checkout. A moved branch fails safely and waits
for a later check rather than installing a different, unchecked revision.
