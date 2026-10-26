import os
import stat
import time

import pytest

from vault.engine.tempfiles import TempStorageError, ensure_root, job_workspace, sweep, sweep_all


def test_root_is_private(tmp_path):
    root = ensure_root(tmp_path / "t")
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_symlink_root_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(TempStorageError):
        ensure_root(link)


def test_workspace_is_unique_private_and_cleaned(tmp_path):
    root = ensure_root(tmp_path / "t")
    with job_workspace(root, 1) as p1, job_workspace(root, 1) as p2:
        assert p1.parent != p2.parent
        assert stat.S_IMODE(p1.parent.stat().st_mode) == 0o700
        p1.write_bytes(b"x")
        assert p1.name == "payload"  # never a remote-controlled name
    assert list(root.iterdir()) == []


def test_workspace_cleaned_on_exception(tmp_path):
    root = ensure_root(tmp_path / "t")
    with pytest.raises(RuntimeError), job_workspace(root, 5) as p:
        p.write_bytes(b"secret media")
        raise RuntimeError("boom")
    assert list(root.iterdir()) == []


def test_sweep_removes_only_old_job_dirs(tmp_path):
    root = ensure_root(tmp_path / "t")
    old = root / "job-1-aaa"
    old.mkdir()
    (old / "payload").write_bytes(b"x")
    past = time.time() - 7200
    os.utime(old, (past, past))
    fresh = root / "job-2-bbb"
    fresh.mkdir()
    other = root / "keep.txt"
    other.write_text("not ours")
    assert sweep(root, 3600) == 1
    assert not old.exists() and fresh.exists() and other.exists()
    assert sweep_all(root) == 1
    assert not fresh.exists()
