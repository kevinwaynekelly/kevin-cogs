# NotificationPlus

NotificationPlus detects failures across Kevin's cogs and hands sanitized alerts
to Unraid's existing notification system. Unraid decides where to send them,
including email, using its configured notification recipients. No SMTP password,
email account or host shell access is needed inside Redbot.

## Install and enable

```text
!cog install kevin notificationplus
!load notificationplus
!notifications enable
!notifications
!notifications test
```

`notifications` shows the exact **container outbox** path. Configure the included
[Unraid host bridge](unraid/) to read the matching persistent host path and run it
every minute using Unraid's User Scripts plugin. Enable email for **Alert** events
in Unraid notification settings and verify its SMTP test first. The host bridge
runs outside Redbot; this cog does not mount Docker's socket or execute host
commands. A test queued by the cog confirms the file handoff, not email delivery.

All controls are bot owner only. Slash controls are `/notifications status`,
`/notifications enable`, `/notifications disable` and `/notifications test`.
Enable them with `!slash enablecog notificationplus` and `!slash sync`.

## Detected failures

- Unexpected command exceptions from the suite, including the CorePlus and
  DownloaderPlus management wrappers. Normal invalid arguments, denied
  permissions, cooldowns and other expected command errors do not create alerts.
- Error and warning logs originating in AudioPlus, BackupPlus, CommunityPlus,
  CorePlus, DashboardPlus, DownloaderPlus, EmojiStealerPlus, ExportPlus, IntroPlus,
  LevelPlus, LogPlus, OwoPlus, PresencePlus and SettingsHub. Unexpected listener
  exceptions logged by Red/Discord are attributed using their traceback modules.
- Explicit AudioPlus daily check and final playback failures, plus suite reply
  delivery failures. Existing Discord error messages and daily failure DMs remain.

Only loaded cogs generate events. NotificationPlus itself is excluded from its
logging detector so a failed outbox write cannot recursively generate alerts.
Failures caught by another cog without any log or explicit report cannot be
detected by an external observer. NotificationPlus does not watch for a stopped
container, a bot process crash or a disconnected host; use a host health check for
those conditions.

## Delivery and limits

The persistent `alerts/unraid.json` file is atomically replaced with mode `0600`.
It keeps up to 64 events from the last seven days. Matching cog, stage, category,
cause and server failures are suppressed for ten minutes, including after
reloads. A test always receives a new sequence number. The host bridge maintains
its own delivery cursor; retained events are not proof that emails remain unsent.
`!notifications disable` stops collection and clears the retained event list.

Log admission is bounded to 128 events, including callbacks scheduled by other
threads. During a larger burst additional events are dropped; the status command
shows the session's dropped count. Unload removes the logging handler, gives
already queued events up to five seconds to drain, cancels its owned worker and
waits for started file writes. Pending memory events can be lost during a process
crash or unload timeout. An invalid or oversized outbox is replaced with a new
producer identifier and a warning in Red's logs; corrupt queued records are never
forwarded. Filesystem write failures appear in the status and Red's logs.

## Stored data

The file contains UTC timestamps, suite cog names, fixed stages/categories,
sanitized exception class and numeric HTTP/OS codes, an optional server ID stored
as a decimal string, and a random producer identifier with a sequence counter.
Member identities, chat content, song titles, media URLs, secrets, raw exception
messages and tracebacks are excluded. Email alerts follow Unraid's configured
recipients; no email address is stored in this cog. User data export/deletion
hooks contain no member records. Disabling clears queued records but cannot
remove notifications already processed by Unraid or delivered by email.
