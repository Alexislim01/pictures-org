"""Shared path, hashing, and durable JSON helpers."""

import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path


class SafetyError(ValueError):
    pass


def beneath(path, root):
    return Path(path).is_relative_to(Path(root))


def plain_path(path):
    """Reject symlinks, junctions, and other reparse points in existing ancestors."""
    path = Path(os.path.abspath(path))
    for part in [path, *path.parents]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SafetyError(f"Links/reparse points are not supported: {part}")
    return path


def fingerprint(path):
    path = plain_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise SafetyError(f"Not a regular file: {path}")
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino


def hash_file(path, sample=False, progress=None):
    before = fingerprint(path)
    size = before[0]
    h = hashlib.sha256()
    last = time.monotonic()
    read = 0
    with open(path, "rb") as stream:
        if sample:
            for offset in sorted({0, max(0, size // 2 - 32768), max(0, size - 65536)}):
                stream.seek(offset)
                h.update(stream.read(65536))
        else:
            while block := stream.read(8 * 1024 * 1024):
                h.update(block)
                read += len(block)
                if progress and time.monotonic() - last >= 5:
                    progress(f"Reading {path}: {read / 2**20:,.0f}/{size / 2**20:,.0f} MiB")
                    last = time.monotonic()
    if fingerprint(path) != before:
        raise SafetyError(f"File changed while hashing: {path}")
    return h.hexdigest(), before


def check_content(path, size, digest):
    actual, before = hash_file(path)
    if before[0] != size or actual != digest:
        raise SafetyError(f"File contents no longer match the plan: {path}")
    return before


def write_json(path, data):
    path = plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    return json.loads(plain_path(path).read_text(encoding="utf-8"))
