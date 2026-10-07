"""Install the pinned, verified upstream tunnel client while building the image."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import urllib.request
import zipfile
from pathlib import Path

VERSION = "v0.0.16"
ARCHIVE = f"tunnel-client-{VERSION}-linux-amd64"
URL = f"https://github.com/openai/tunnel-client/releases/download/{VERSION}/{ARCHIVE}.zip"
SHA256 = "d60cdba019bce451bcc3a15478cc5b9cb11270b049f5b56ea39a80b517f8b117"
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024


def install(data: bytes, destination: Path):
    """Select exact names after authentication; never extract archive paths."""
    if len(data) > MAX_ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("Tunnel client archive checksum mismatch")
    selected = {
        "tunnel-client": (destination / "bin/tunnel-client", 0o755),
        "LICENSE": (destination / "share/doc/tunnel-client/LICENSE", 0o644),
        "NOTICE": (destination / "share/doc/tunnel-client/NOTICE", 0o644),
        f"{ARCHIVE}-licenses.txt": (
            destination / "share/doc/tunnel-client/licenses.txt",
            0o644,
        ),
        f"{ARCHIVE}.spdx.json": (destination / "share/doc/tunnel-client/sbom.spdx.json", 0o644),
    }
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("Duplicate archive member")
        for name, (target, mode) in selected.items():
            info = archive.getinfo(name)
            file_mode = info.external_attr >> 16
            if info.is_dir() or stat.S_ISLNK(file_mode) or info.file_size > MAX_MEMBER_BYTES:
                raise ValueError("Unsupported archive member")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                remaining = info.file_size
                while remaining:
                    part = source.read(min(remaining, 1024 * 1024))
                    if not part:
                        raise ValueError("Truncated archive member")
                    output.write(part)
                    remaining -= len(part)
            target.chmod(mode)


def main():
    if os.uname().machine not in {"x86_64", "amd64"}:
        raise SystemExit("This deployment is pinned for Unraid x86_64 (linux/amd64).")
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    install(data, Path("/usr/local"))


if __name__ == "__main__":
    main()
