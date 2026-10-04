"""AudioPlus data-storage statement."""

__red_end_user_data_statement__ = "Preserves legacy Lavalink connection settings, including the node password, in Red Config for rollback. Native playback ignores them. Optional daily checks store the configuring owner's Discord ID, test server/channel IDs, public video URL, local schedule, latest safe result, and pending failure DM in Config. User-data hooks export or remove that owner's check settings and disable the monitor. Request contexts are transient. Optional recovery stores up to 100 public track records, requester IDs, position, pause state, volume and repeat mode for seven days, checkpointed every ten seconds and on clean unload. Intentional stop/disconnect clears active checkpoints. Guild settings store music panel/DJ/vote preferences and member-specific playlists/favorites with source URLs and metadata; Shared server playlists retain up to ten collections of 100 approved tracks and 100 suggestions, with proposer IDs. User-data hooks export/delete personal collections, remove pending suggestions and anonymize approved/recovery requester attribution. Listening history starts enabled and stores up to 100 public playback-start records per server for 30 days, capped at 512 KiB, with requester IDs, times and metadata. Members can clear their own records; administrators can disable/erase history. User-data hooks export/delete identified request records. No extracted signed stream URL, provider credentials or yt-dlp disk cache is persisted."

__red_end_user_data_statement__ += (
    " Opt-in session summaries retain up to 100 public track starts, requester IDs and "
    "decoded playback time per active connection in bounded memory. Up to 20 completed "
    "summaries and save controls remain in memory for three minutes; export/deletion hooks "
    "include these identified requests. Saving a session uses the existing personal playlist "
    "storage. Posted summaries remain on Discord; unload clears transient session records."
)

__red_end_user_data_statement__ += (
    " Local song caching starts automatically on upgrade. Known non-live provider tracks "
    "strictly under five minutes are copied as audio-only Opus files in AudioPlus's persistent "
    "cog data directory, separately scoped by server. Copies expire three calendar months "
    "after download, without extending on replay. Storage is capped at 2 GiB and 2,000 songs "
    "across the bot, with a 16 MiB per-copy limit. Cache metadata stores server IDs, hashed "
    "public source URLs, duration, size, creation time and up to 128 requester IDs per song; "
    "additional requesters create no cache attribution record. No source titles, raw URLs, "
    "signed streams, or HTTP headers are saved in cache metadata. User-data hooks export "
    "identified cache metadata and delete whole copies associated with that user, including "
    "shared copies, while cancelling associated downloads. Manage Server or Red administrators "
    "can clear their server's cache; server removal also erases its copies. Complete copies "
    "survive reload/restart, while owned downloads and partial files are cancelled/removed."
)

__red_end_user_data_statement__ += (
    " In-flight personal requests are cancelled and awaited during deletion, and new writes "
    "are rejected until deletion finishes. Playback attribution is anonymized before cache "
    "removal finishes; retained history, recovery and session writes are serialized with "
    "deletion. Request admission is limited to 32 simultaneous operations globally and "
    "16 per server. Active ownership records are discarded when operations finish; no "
    "permanent deleted-user tombstones are retained. New deliberate requests after deletion "
    "may create fresh data."
)
