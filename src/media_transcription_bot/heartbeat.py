"""Symlink-safe heartbeat shared by the bot and Docker health probe."""

from __future__ import annotations

import os
import stat
import tempfile
import time
from pathlib import Path


def _directory() -> Path:
    return Path(tempfile.gettempdir()) / f"media-transcription-bot-{os.getuid()}"


def _path() -> Path:
    return _directory() / "heartbeat"


def _no_follow_flag() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _secure_directory() -> Path:
    directory = _directory()
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("Heartbeat directory ownership is unsafe.")
    if stat.S_IMODE(info.st_mode) != 0o700:
        directory.chmod(0o700)
    return directory


def write_heartbeat() -> None:
    _secure_directory()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _no_follow_flag()
    descriptor = os.open(_path(), flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("Heartbeat file ownership is unsafe.")
        os.write(descriptor, f"{time.time():.6f}".encode("ascii"))
    finally:
        os.close(descriptor)


def heartbeat_age() -> float:
    descriptor = os.open(_path(), os.O_RDONLY | _no_follow_flag())
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("Heartbeat file ownership is unsafe.")
        raw = os.read(descriptor, 64)
    finally:
        os.close(descriptor)
    return time.time() - float(raw.decode("ascii"))
