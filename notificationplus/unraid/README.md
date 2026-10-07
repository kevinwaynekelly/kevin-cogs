# Unraid notification delivery

Run the included [script](script) on the **Unraid host**, once per minute. It reads
NotificationPlus's persistent outbox and uses Unraid's existing Alert recipients.
Redbot needs no SMTP credentials or Docker socket.

## Set up Redbot

Use your existing repository name in place of `kevin` if different:

```text
!cog install kevin notificationplus
!load notificationplus
!notifications enable
!notifications
```

Copy the exact **Container outbox** path from the status message. Keep Red's data
directory on persistent Docker storage. Update/reload the other suite cogs to
include their notification failure reporting hooks.

## Install through Aria's scripts repository

The [Aria scripts repository](https://github.com/kevinwaynekelly/unraid-userscripts)
includes the self-contained **1_cog_failure_alerts** job. In the Unraid terminal:

```sh
bash /boot/config/plugins/user.scripts/scripts/3_pull_github_repo/script
```

Refresh User Scripts and schedule **1_cog_failure_alerts** with `* * * * *`.
Run it once manually. Future bridge updates arrive through that same updater;
no `docker cp`, separate bridge file or wrapper is needed for this installation.
New jobs start unscheduled and existing schedules/settings are preserved.

The default discovers one NotificationPlus outbox in the persistent `red-discordbot:/data`
mount, directly under `cogs/NotificationPlus/alerts/unraid.json` or one/two
instance directories below it. Existing `maintenance.conf` files need no new
settings for that layout. No Config contents or container environment are read.
For another container/data root set `COG_ALERTS_CONTAINER`/`COG_ALERTS_DATA_PATH`
in `/boot/config/plugins/user.scripts/maintenance.conf`. With multiple instances,
nested mounts or a custom layout, set `COG_ALERTS_CONTAINER_PATH` to the exact
**Container outbox** from `!notifications`, or `COG_ALERTS_PATH` to its host path.
If an earlier setup saved `COG_ALERTS_CONTAINER="redbot"`, change it to
`COG_ALERTS_CONTAINER="red-discordbot"` in the shared `maintenance.conf`; the
repository updater preserves existing settings. The name must exactly match
the Docker container, not its image or appdata directory name.

Preview sends nothing and leaves delivery cursors untouched. Host job failures
use Aria's existing failure/cooldown/recovery notifications.

## Standalone installation without Aria's updater

Use this alternative only when the repository-managed job is not installed.
Both approaches use the same cursor by default; keep only one scheduled job.


In **Settings → User Scripts**, add a script called `1_cog_failure_alerts`.
In the Unraid terminal, copy the bridge from the installed cog:

```sh
docker cp red-discordbot:/data/cogs/CogManager/cogs/notificationplus/unraid/script \
  /boot/config/plugins/user.scripts/scripts/1_cog_failure_alerts/bridge
```

Replace `red-discordbot` with your actual container name. The source above is the usual
installed-source location; adjust it if your Red instance installs cogs elsewhere.
This is the cog's source file, separate from the status message's data outbox path.

Edit the User Script to contain:

```bash
#!/bin/bash
export COG_ALERTS_CONTAINER=red-discordbot
export COG_ALERTS_CONTAINER_PATH='/paste/the/exact/Container-outbox/path/here'
exec bash /boot/config/plugins/user.scripts/scripts/1_cog_failure_alerts/bridge
```

Select a custom schedule of `* * * * *`. Run it manually once to check setup.
The bridge translates the exact data path through Docker's persistent mounts;
it does not read the container's environment, tokens or Config database.
Alternatively, set `COG_ALERTS_PATH` to the exact host file path and omit the
container variables. That option can read alerts while the container is stopped.

Host requirements are Bash, `jq`, `flock`, `timeout`, `stat`, `dd`, `mktemp` and
`sync`. Docker is only needed for mount translation. Notification state defaults
to `/mnt/user/appdata/user-scripts-state/notificationplus`; set
`COG_ALERTS_STATE_DIR` to another private persistent directory if needed.

## Enable and test email

In [Unraid Notification Settings](https://docs.unraid.net/unraid-os/getting-started/set-up-unraid/customize-unraid-settings/),
enable **Email** for **Alerts**. Use the SMTP **TEST** button to verify your saved
sender and recipients. Then run:

```text
!notifications test
```

Run the User Script again or wait for its next minute. Check Unraid's notification
history and your inbox. A successful bridge run means the Unraid notification
program ran successfully; it cannot verify SMTP delivery or receipt. Unraid's
notification program does not reliably expose email-send failures through its
exit status, so verify SMTP separately.

## Delivery behavior

New failures are grouped into one Alert per run, with up to eight details and a
count of additional failures. Unraid's configured browser/email/agent choices
apply. Matching failures are suppressed by the cog for ten minutes. This bridge
never edits or deletes the producer's file; its own private cursor remembers
which events Unraid has processed.

Failures older than seven days are skipped even if the bot has stopped. Future
timestamps and subsequent events wait until the host clock catches up, so keep
the Red container and Unraid host clocks synchronized.

Rejected or timed-out notification submissions retain the cursor and retry on
the next run. A crash after submission but before saving the cursor can repeat
an alert. A stopped bot/host, filesystem failure, malformed outbox or missing
program needs separate host monitoring; this bridge cannot notify without a
working host notification subsystem. Errors are visible in its User Scripts log.

Disabling with `!notifications disable` clears retained events. It cannot retract
notifications already processed by Unraid. To remove the host integration, disable
the User Script's schedule before deleting its files or delivery state.
