"""Read-only inventory with persistent, metadata-keyed SHA-256 checkpoints."""

import csv
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .common import SafetyError, beneath, fingerprint, hash_file, plain_path, write_json

MEDIA = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp",
         ".tif", ".tiff", ".dng", ".raw", ".mp4", ".mov", ".m4v", ".avi",
         ".mkv", ".webm", ".3gp", ".mts", ".m2ts"}


def scan(root, workspace, progress=print, fresh=False):
    root, workspace = plain_path(root), plain_path(workspace)
    if not root.is_dir():
        raise SafetyError(f"Source is not a directory: {root}")
    if beneath(workspace, root) or beneath(root, workspace):
        raise SafetyError("Source and workspace must be separate, non-overlapping folders")
    workspace.mkdir(parents=True, exist_ok=True)
    # Refuse a linked database as well as linked workspace ancestors.
    cache = plain_path(workspace / "hashes.sqlite")
    db = sqlite3.connect(cache)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("""CREATE TABLE IF NOT EXISTS hashes(
        path TEXT, size INTEGER, mtime INTEGER, ctime INTEGER, dev TEXT, ino TEXT,
        kind TEXT, digest TEXT, PRIMARY KEY(path,size,mtime,ctime,dev,ino,kind))""")
    errors, skipped, records = [], [], []

    def fail(error):
        errors.append(str(error))

    def cached(path, info, kind):
        key = (str(path), *info[:3], str(info[3]), str(info[4]), kind)
        if not fresh:
            row = db.execute("SELECT digest FROM hashes WHERE path=? AND size=? AND mtime=? "
                             "AND ctime=? AND dev=? AND ino=? AND kind=?", key).fetchone()
            if row:
                if fingerprint(path) != info:
                    raise SafetyError(f"File changed since inventory: {path}")
                return row[0]
        digest, now = hash_file(path, sample=kind == "sample", progress=progress)
        if now != info:
            raise SafetyError(f"File changed since inventory: {path}")
        db.execute("INSERT OR REPLACE INTO hashes VALUES(?,?,?,?,?,?,?,?)", (*key, digest))
        db.commit()
        return digest

    try:
        by_size = defaultdict(list)
        types = defaultdict(lambda: {"files": 0, "bytes": 0})
        identities = {}
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
            for name in dirs[:]:
                try:
                    plain_path(Path(directory) / name)
                except (OSError, SafetyError) as exc:
                    dirs.remove(name)
                    skipped.append(str(exc))
            for name in files:
                path = Path(directory) / name
                try:
                    info = fingerprint(path)
                    record = {"path": str(path), "size_bytes": info[0],
                              "mtime_ns": info[1], "ctime_ns": info[2],
                              "extension": path.suffix.lower(),
                              "category": "media" if path.suffix.lower() in MEDIA else "other"}
                    records.append(record)
                    ext = record["extension"] or "(none)"
                    types[ext]["files"] += 1
                    types[ext]["bytes"] += info[0]
                    identity = (info[3], info[4])
                    # Multiple hardlink names refer to one physical file. Do not count
                    # them as independently reclaimable copies.
                    if identity[1] and identity in identities:
                        skipped.append(f"Hardlink alias: {path} -> {identities[identity]}")
                        continue
                    identities[identity] = str(path)
                    if info[0] > 0:
                        by_size[info[0]].append((path, info))
                except (OSError, SafetyError) as exc:
                    fail(exc)
        progress(f"Inventoried {len(records):,} files; screening equal-size candidates")
        sampled = []
        checked = 0
        for size, items in by_size.items():
            if len(items) < 2:
                continue
            buckets = defaultdict(list)
            for path, info in items:
                try:
                    buckets[cached(path, info, "sample")].append((path, info))
                except (OSError, SafetyError) as exc:
                    fail(exc)
                checked += 1
                if checked % 500 == 0:
                    progress(f"Screened {checked:,} candidates")
            sampled.extend((size, bucket) for bucket in buckets.values() if len(bucket) > 1)
        progress(f"Full verification: {sum(len(items) for _, items in sampled):,} files, "
                 f"{sum(size * len(items) for size, items in sampled) / 2**30:,.2f} GiB")
        groups, checked = [], 0
        for size, items in sampled:
            buckets = defaultdict(list)
            for path, info in items:
                try:
                    buckets[cached(path, info, "full")].append(str(path))
                except (OSError, SafetyError) as exc:
                    fail(exc)
                checked += 1
                if checked % 250 == 0:
                    progress(f"Verified {checked:,} full files")
            for digest, paths in buckets.items():
                if len(paths) > 1:
                    groups.append({"sha256": digest, "size_bytes": size,
                                   "paths": sorted(paths), "copies": len(paths),
                                   "recoverable_bytes": size * (len(paths) - 1)})
        groups.sort(key=lambda group: (-group["recoverable_bytes"], group["sha256"]))
        report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                  "summary": {"root": str(root), "files_scanned": len(records),
                              "total_bytes": sum(r["size_bytes"] for r in records),
                              "duplicate_groups": len(groups),
                              "extra_copies": sum(g["copies"] - 1 for g in groups),
                              "recoverable_bytes": sum(g["recoverable_bytes"] for g in groups),
                              "errors": errors, "skipped": skipped},
                  "types": dict(types), "groups": groups}
        write_json(workspace / "report.json", report)
        with open(plain_path(workspace / "inventory.csv"), "w", newline="", encoding="utf-8-sig") as stream:
            fields = ["path", "size_bytes", "mtime_ns", "ctime_ns", "extension", "category"]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
        with open(plain_path(workspace / "duplicates.csv"), "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(["group", "sha256", "size_bytes", "recoverable_bytes_in_group", "path"])
            for number, group in enumerate(groups, 1):
                for path in group["paths"]:
                    writer.writerow([number, group["sha256"], group["size_bytes"],
                                     group["recoverable_bytes"], path])
        summary = report["summary"]
        lines = ["# Duplicate scan", "", f"Source: `{root}`", "",
                 "Read-only report. No source files were changed.", "",
                 f"- Files: {len(records):,}",
                 f"- Logical size: {summary['total_bytes'] / 2**30:,.2f} GiB",
                 f"- Extra copies: {summary['extra_copies']:,}",
                 f"- Potential savings: {summary['recoverable_bytes'] / 2**30:,.2f} GiB",
                 f"- Read errors: {len(errors)}; excluded links/aliases: {len(skipped)}", "",
                 "Savings assume keeping one file per exact-content group. Compression, sparse files, "
                 "and filesystem allocation can change actual reclaimed space. Non-media files "
                 "are inventoried but excluded from cleanup plans by default. Google/cloud files "
                 "are included only if downloaded inside the source folder.", ""]
        lines += ["## Read errors", "", *[f"- {e}" for e in errors], ""] if errors else []
        lines += ["## Exclusions", "", *[f"- {e}" for e in skipped], ""] if skipped else []
        plain_path(workspace / "report.md").write_text("\n".join(lines), encoding="utf-8")
        return report
    finally:
        db.close()
