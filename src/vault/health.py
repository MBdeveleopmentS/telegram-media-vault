"""File-based liveness for worker processes (used by Docker HEALTHCHECK).

Usage in a container: ``python -m vault.health engine`` exits 0 if the process
touched its heartbeat file within MAX_AGE seconds.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

RUN_DIR = Path(os.environ.get("VAULT_RUN_DIR", "/tmp/vault-run"))  # noqa: S108 - container-private tmpfs
MAX_AGE = 90


def touch(component: str) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    (RUN_DIR / f"{component}.alive").write_text(str(int(time.time())))


def check(component: str, max_age: int = MAX_AGE) -> bool:
    try:
        ts = int((RUN_DIR / f"{component}.alive").read_text())
    except (OSError, ValueError):
        return False
    return time.time() - ts <= max_age


if __name__ == "__main__":
    sys.exit(0 if check(sys.argv[1]) else 1)
