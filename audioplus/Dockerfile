# Native AudioPlus dependencies for the Debian-based PhasecoreX Red images.
ARG RED_IMAGE=phasecorex/red-discordbot:core
FROM node:22-bookworm-slim AS javascript
FROM ${RED_IMAGE}

USER root
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 libatomic1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Keep the base image's entrypoint, startup command, and /data volume.
# Red Downloader installs yt-dlp and its EJS scripts.
# Install PyNaCl and davey once in the running bot's /data/venv (see README).
# Installing them here would be hidden by PhasecoreX's /data volume at runtime.
