"""Crash-safe JSON state files.

Writes go to a temp file in the same directory, are fsynced and then renamed over the
target, so a crash mid-write leaves either the old or the new file, never half of one.

A file that exists but cannot be parsed is NOT treated as empty: the reader logs an
error, keeps a copy next to it (``<name>.corrupt-<ts>``) and blocks every later write to
that path, so one bad read can no longer overwrite everybody's points with ``{}``.
The block lifts by itself once the file reads cleanly again (e.g. after a manual fix).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import tempfile
import time

_log = logging.getLogger("QQBot")

_unreadable: set[str] = set()


def _key(path) -> str:
    return os.path.abspath(os.fspath(path))


def is_unreadable(path) -> bool:
    return _key(path) in _unreadable


def read_json(path, default, *, label: str = ""):
    """Return the parsed file, ``default`` (a copy) if the file does not exist.

    On a parse error the file is retried once; if it is still bad the error is logged,
    a copy is kept and writes to the path are blocked until it parses again.
    """
    key = _key(path)
    if not os.path.exists(key):
        _unreadable.discard(key)
        return copy.deepcopy(default)
    last_error = None
    for attempt in range(2):
        try:
            with open(key, "r", encoding="utf-8") as f:
                value = json.load(f)
            _unreadable.discard(key)
            return value
        except (OSError, ValueError) as error:
            last_error = error
            if attempt == 0:
                time.sleep(0.05)
    if key not in _unreadable:
        _unreadable.add(key)
        backup = f"{key}.corrupt-{time.strftime('%Y%m%dT%H%M%S')}"
        try:
            shutil.copy2(key, backup)
        except OSError:
            backup = "(copy failed)"
        _log.error(
            "[状态文件损坏] %s 读取失败：%s；已停止写入这个文件，副本 %s，需人工处理",
            label or key,
            last_error,
            backup,
        )
    return copy.deepcopy(default)


def write_json(path, value, *, indent: int | None = None, label: str = "") -> bool:
    """Atomically replace ``path`` with ``value``. Returns False if the write was refused or failed."""
    key = _key(path)
    if key in _unreadable:
        _log.error("[状态文件损坏] %s 上次读取失败，拒绝覆盖写入", label or key)
        return False
    directory = os.path.dirname(key) or "."
    tmp_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(key)}.", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(key):
            try:
                st = os.stat(key)
                os.chmod(tmp_path, st.st_mode & 0o7777)
            except OSError:
                pass
        os.replace(tmp_path, key)
        tmp_path = None
        return True
    except Exception as error:  # noqa: BLE001 - logged, caller keeps running
        _log.warning("[状态文件] 写入失败 %s: %s", label or key, error)
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
