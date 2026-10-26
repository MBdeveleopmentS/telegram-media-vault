"""Per-job temporary storage.

- Root directory is created 0700 and must not be world-accessible.
- Each job gets its own randomly named 0700 subdirectory; the payload file
  name is fixed ("payload") so no remote-controlled string ever reaches the
  filesystem (no path traversal by construction).
- Cleanup: on success, on terminal failure, on retry (the next attempt
  re-downloads with a fresh file_reference), and a sweeper removes leftovers
  after crashes.

Deletion is logical (unlink). On SSDs, CoW filesystems and snapshots it is not
a forensic secure erase; production should mount TEMP_DIR on tmpfs or an
encrypted ephemeral volume (see docs/DEPLOYMENT.md).
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PAYLOAD_NAME = "payload"
DIR_PREFIX = "job-"


class TempStorageError(Exception):
    pass


def ensure_root(root: str | Path) -> Path:
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(p, 0o700)
    st = p.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise TempStorageError("temp root must be a real directory")
    if st.st_mode & 0o077:
        raise TempStorageError("temp root must not be group/world accessible")
    return p


@contextmanager
def job_workspace(root: Path, job_id: int) -> Iterator[Path]:
    """Yields the payload path inside a fresh private directory; always removes it."""
    d = root / f"{DIR_PREFIX}{job_id}-{secrets.token_hex(12)}"
    os.mkdir(d, 0o700)
    try:
        yield d / PAYLOAD_NAME
    finally:
        shutil.rmtree(d, ignore_errors=True)


def sweep(root: Path, max_age_seconds: int, *, now: float | None = None) -> int:
    """Remove job directories older than ``max_age_seconds`` (crash leftovers)."""
    now = time.time() if now is None else now
    removed = 0
    for entry in root.iterdir():
        if not entry.name.startswith(DIR_PREFIX):
            continue
        try:
            age = now - entry.lstat().st_mtime
        except FileNotFoundError:
            continue
        if age > max_age_seconds:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
    return removed


def sweep_all(root: Path) -> int:
    """On engine startup, nothing in TEMP_DIR belongs to a live job: remove all of it."""
    return sweep(root, -1)
