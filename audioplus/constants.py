"""Persistent defaults and Lavalink connection settings."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NodeConfig:
    uri: str
    password: str
    resume_timeout: int = 60
    secure: bool = False
    identifier: str = "MAIN"

    @classmethod
    def from_parts(cls, host: str, port: int, password: str, secure: bool) -> "NodeConfig":
        scheme = "https" if secure else "http"
        return cls(uri=f"{scheme}://{host}:{port}", password=password, secure=secure)


__red_end_user_data_statement__ = "Stores global Lavalink connection settings, including the node password, in Red Config. Playback state is transient."
