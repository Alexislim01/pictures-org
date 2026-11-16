"""Reviewable cleanup plans and a journaled, same-volume quarantine."""

import os
import re
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .common import (SafetyError, beneath, check_content, fingerprint, plain_path,
                     read_json, write_json)
from .scan import MEDIA

STATES = {"pending", "moving", "quarantined", "restoring", "restored", "purging", "purged"}


def existing_parent(path):
    while not path.exists():
        path = path.parent
    return path


def separate(first, second):
    return not beneath(first, second) and not beneath(second, first)


def make_plan(report_path, quarantine, output, prefer=None):
    report = read_json(report_path)
    if report.get("schema_version") != 1:
        raise SafetyError("Unsupported scan report; run a new scan with this version")
    if report["summary"]["errors"]:
        raise SafetyError("Resolve scan errors and rescan before planning cleanup")
    root, quarantine, output = (plain_path(report["summary"]["root"]),
                                plain_path(quarantine), plain_path(output))
    if not separate(root, quarantine) or not separate(root, output):
        raise SafetyError("Plan and quarantine must be outside the source tree")
    if not separate(output, quarantine):
        raise SafetyError("Plan must be outside the quarantine tree")
    if output.exists():
        raise SafetyError("Plan output already exists; choose a new filename")
    if root.stat().st_dev != existing_parent(quarantine).stat().st_dev:
        raise SafetyError("Quarantine must be on the same volume as the source")
    preferred = plain_path(prefer) if prefer else None
    if preferred and not beneath(preferred, root):
        raise SafetyError("Preferred keeper folder must be inside the source")
    plan_id = str(uuid.uuid4())
    folder = quarantine / plan_id
    actions = []
    for group in report["groups"]:
        # v1 moves only recognized image/video extensions; APKs and documents stay put.
        paths = [plain_path(p) for p in group["paths"] if Path(p).suffix.lower() in MEDIA]
        if len(paths) < 2:
            continue

        def rank(path):
            copied = bool(re.search(r"(?:[ _-]copy(?:[ ._(]|$)|\(\d+\)(?=\.))", path.name, re.I))
            return (0 if preferred and beneath(path, preferred) else 1,
                    copied, len(path.parts), len(path.name), str(path).casefold(), str(path))

        keeper = min(paths, key=rank)
        for path in sorted(p for p in paths if p != keeper):
            index = len(actions)
            actions.append({"source": str(path), "keeper": str(keeper),
                            "destination": str(folder / f"{index:08d}.quarantined"),
                            "sha256": group["sha256"], "size_bytes": group["size_bytes"],
                            "status": "pending"})
    plan = {"schema_version": 1, "id": plan_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "root": str(root), "quarantine": str(folder), "actions": actions}
    validate(plan, output)
    write_json(output, plan)
    return plan


def validate(plan, plan_path):
    if plan.get("schema_version") != 1 or str(uuid.UUID(plan["id"])) != plan["id"]:
        raise SafetyError("Invalid plan ID or schema")
    root, quarantine = plain_path(plan["root"]), plain_path(plan["quarantine"])
    if quarantine.name != plan["id"] or not separate(root, quarantine.parent):
        raise SafetyError("Quarantine location does not match the plan")
    if not separate(plain_path(plan_path), root) or not separate(plain_path(plan_path), quarantine.parent):
        raise SafetyError("Plan must be outside the media and quarantine trees")
    sources, destinations, keepers = set(), set(), set()
    for index, action in enumerate(plan["actions"]):
        source, keeper, target = (plain_path(action["source"]), plain_path(action["keeper"]),
                                  plain_path(action["destination"]))
        if not beneath(source, root) or not beneath(keeper, root) or source == keeper:
            raise SafetyError("Source and keeper must be distinct files inside the source tree")
        if target != quarantine / f"{index:08d}.quarantined":
            raise SafetyError("Destination does not match its quarantine slot")
        if source in sources or target in destinations:
            raise SafetyError("Repeated source or destination in plan")
        if action["status"] not in STATES:
            raise SafetyError("Unknown action state")
        if not re.fullmatch(r"[a-f0-9]{64}", action["sha256"]):
            raise SafetyError("Invalid SHA-256 digest")
        if type(action["size_bytes"]) is not int or action["size_bytes"] <= 0:
            raise SafetyError("Invalid file size")
        if source.suffix.lower() not in MEDIA or keeper.suffix.lower() not in MEDIA:
            raise SafetyError("Cleanup supports media files only")
        sources.add(source)
        destinations.add(target)
        keepers.add(keeper)
    if sources & keepers:
        raise SafetyError("A keeper cannot also be scheduled for removal")
    return root, quarantine


@contextmanager
def locked(plan_path):
    plan_path = plain_path(plan_path)
    lock = plain_path(Path(str(plan_path) + ".lock"))
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SafetyError(f"Plan is locked: {lock}. See README for interrupted-run recovery.") from exc
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


def move_file(source, target):
    source, target = plain_path(source), plain_path(target)
    if target.exists():
        raise SafetyError(f"Refusing to overwrite: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    plain_path(target)
    if source.stat().st_dev != target.parent.stat().st_dev:
        raise SafetyError("Cross-volume moves are not supported")
    # On Windows rename refuses an existing destination. Each target is a unique
    # slot in a locked plan; do not edit the media/quarantine during an operation.
    os.rename(source, target)


def operate(plan_path, mode, confirm=None, progress=print):
    plan_path = plain_path(plan_path)
    if mode not in {"apply", "restore", "purge"}:
        raise SafetyError("Unknown operation")
    with ExitStack() as stack:
        stack.enter_context(locked(plan_path))
        plan = read_json(plan_path)
        root, quarantine = validate(plan, plan_path)
        # Different plans operating on the same collection must not race over
        # their keeper choices. This lock is used only by cleanup commands.
        stack.enter_context(locked(root / ".pictures-org-cleanup"))
        if mode == "purge" and confirm != plan["id"]:
            raise SafetyError("Permanent deletion requires --confirm with the exact plan ID")
        if mode == "apply" and root.stat().st_dev != existing_parent(quarantine).stat().st_dev:
            raise SafetyError("Quarantine must be on the same volume as the source")
        verified_keepers = {}
        completed = 0

        def save(action, state):
            action["status"] = state
            write_json(plan_path, plan)

        def verified(action, path):
            return check_content(path, action["size_bytes"], action["sha256"])

        def keeper_ok(action):
            path = action["keeper"]
            key = (path, action["size_bytes"], action["sha256"])
            old = verified_keepers.get(key)
            if old is None or fingerprint(path) != old:
                verified_keepers[key] = verified(action, path)

        for index, action in enumerate(plan["actions"], 1):
            source, target = Path(action["source"]), Path(action["destination"])
            status = action["status"]
            progress(f"{mode}: {index}/{len(plan['actions'])} {source.name} [{status}]")
            # Resolve an interrupted same-volume rename from actual file locations.
            if status in {"moving", "restoring"}:
                if source.exists() == target.exists():
                    raise SafetyError(f"Ambiguous interrupted move; inspect both paths: {source}, {target}")
                verified(action, target if target.exists() else source)
                save(action, "quarantined" if target.exists() else
                     ("pending" if status == "moving" else "restored"))
                status = action["status"]
            if status == "purging":
                if not target.exists():
                    save(action, "purged")
                    status = "purged"
                elif mode != "purge":
                    verified(action, target)
                    save(action, "quarantined")
                    status = "quarantined"
            if mode == "apply":
                if status != "pending":
                    continue
                keeper_ok(action)
                source_info = verified(action, source)
                if fingerprint(source)[3:] == fingerprint(action["keeper"])[3:]:
                    raise SafetyError("Source and keeper are hardlinks to the same physical file")
                if target.exists():
                    raise SafetyError(f"Quarantine slot is occupied: {target}")
                save(action, "moving")
                keeper_ok(action)
                if fingerprint(source) != source_info:
                    raise SafetyError(f"Source changed before move: {source}")
                move_file(source, target)
                save(action, "quarantined")
                completed += 1
            elif mode == "restore":
                if status == "purged":
                    progress(f"Already permanently purged; cannot restore: {source}")
                    continue
                if status != "quarantined":
                    continue
                if source.exists():
                    raise SafetyError(f"Restore would overwrite an existing file: {source}")
                target_info = verified(action, target)
                save(action, "restoring")
                if fingerprint(target) != target_info:
                    raise SafetyError(f"Quarantine file changed before restore: {target}")
                move_file(target, source)
                save(action, "restored")
                completed += 1
            else:
                if status not in {"quarantined", "purging"}:
                    continue
                # A missing/changed keeper blocks permanent deletion, even if the
                # scan and plan were originally valid.
                keeper_ok(action)
                target_info = verified(action, target)
                save(action, "purging")
                keeper_ok(action)
                if fingerprint(target) != target_info:
                    raise SafetyError(f"Quarantine file changed before deletion: {target}")
                plain_path(target).unlink()
                save(action, "purged")
                completed += 1
        return completed
