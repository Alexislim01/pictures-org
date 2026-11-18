import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from pictures_org.cleanup import make_plan, move_file, operate
from pictures_org.common import SafetyError, read_json, write_json
from pictures_org.scan import scan


def quiet(*args):
    pass


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "media"
        self.root.mkdir()
        self.workspace = self.base / "workspace"
        self.plan_path = self.base / "plan.json"
        self.trash = self.base / "trash"
        self.content = b"a meaningful memory\x00" * 100
        for name in ["a.jpg", "b - Copy.jpg", "c.jpg"]:
            (self.root / name).write_bytes(self.content)

    def scan(self, **kwargs):
        return scan(self.root, self.workspace, progress=quiet, **kwargs)

    def plan(self, **kwargs):
        self.scan()
        return make_plan(self.workspace / "report.json", self.trash, self.plan_path, **kwargs)

    def run_op(self, mode, **kwargs):
        return operate(self.plan_path, mode, progress=quiet, **kwargs)

    def interrupted_state(self, slot, status):
        db = sqlite3.connect(str(self.plan_path) + ".state.sqlite")
        try:
            db.execute("INSERT OR REPLACE INTO states VALUES(?,?)", (slot, status))
            db.commit()
        finally:
            db.close()

    def test_inventory_and_exact_duplicate_accounting_leave_files_unchanged(self):
        (self.root / "different.jpg").write_bytes(b"x" * len(self.content))
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        report = self.scan()
        self.assertEqual(report["summary"]["files_scanned"], 4)
        self.assertEqual(report["summary"]["extra_copies"], 2)
        self.assertEqual(report["summary"]["recoverable_bytes"], 2 * len(self.content))
        self.assertEqual(report["summary"]["errors"], [])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertTrue((self.workspace / "inventory.csv").is_file())

    def test_sample_match_is_not_accepted_without_full_hash(self):
        data = bytearray(1024 * 1024)
        (self.root / "one.mp4").write_bytes(data)
        data[200000] = 1  # Outside all three sampled regions.
        (self.root / "two.mp4").write_bytes(data)
        report = self.scan()
        self.assertFalse(any(g["size_bytes"] == len(data) for g in report["groups"]))

    def test_resume_uses_saved_hashes(self):
        first = self.scan()
        with patch("pictures_org.scan.hash_file", side_effect=AssertionError("Unexpected rehash")):
            second = self.scan()
        self.assertEqual(first["groups"], second["groups"])

    def test_changed_same_size_file_invalidates_cache(self):
        self.scan()
        p = self.root / "c.jpg"
        p.write_bytes(b"x" * len(self.content))
        report = self.scan()
        self.assertEqual(report["summary"]["extra_copies"], 1)

    def test_fresh_scan_bypasses_cache(self):
        self.scan()
        from pictures_org.common import hash_file
        with patch("pictures_org.scan.hash_file", wraps=hash_file) as hashing:
            self.scan(fresh=True)
            self.assertGreater(hashing.call_count, 0)

    def test_plan_moves_nothing_and_excludes_non_media(self):
        for name in ["one.apk", "two.apk"]:
            (self.root / name).write_bytes(b"installer")
        plan = self.plan()
        self.assertEqual(len(plan["actions"]), 2)
        self.assertFalse(self.trash.exists())
        self.assertEqual(len(list(self.root.iterdir())), 5)
        self.assertTrue(all(a["keeper"].endswith("a.jpg") for a in plan["actions"]))

    def test_preferred_folder_selects_keeper(self):
        preferred = self.root / "originals"
        preferred.mkdir()
        (preferred / "original.jpg").write_bytes(self.content)
        plan = self.plan(prefer=preferred)
        self.assertEqual(len(plan["actions"]), 3)
        self.assertTrue(all(Path(a["keeper"]).parent == preferred for a in plan["actions"]))

    def test_apply_restore_preserves_content_and_is_idempotent(self):
        plan = self.plan()
        self.assertEqual(self.run_op("apply"), 2)
        self.assertEqual(self.run_op("apply"), 0)
        self.assertTrue((self.root / "a.jpg").exists())
        self.assertEqual(self.run_op("restore"), 2)
        self.assertEqual(self.run_op("restore"), 0)
        for action in plan["actions"]:
            self.assertEqual(Path(action["source"]).read_bytes(), self.content)

    def test_changed_source_blocks_quarantine(self):
        plan = self.plan()
        source = Path(plan["actions"][0]["source"])
        source.write_bytes(b"z" * len(self.content))
        with self.assertRaises(SafetyError):
            self.run_op("apply")
        self.assertTrue(source.exists())

    def test_changed_keeper_blocks_quarantine(self):
        plan = self.plan()
        Path(plan["actions"][0]["keeper"]).write_bytes(b"changed")
        with self.assertRaises(SafetyError):
            self.run_op("apply")
        self.assertFalse(self.trash.exists())

    def test_interrupted_apply_recovers_from_rename_before_journal_update(self):
        self.plan()
        def interrupt(source, target):
            move_file(source, target)
            raise OSError("simulated interruption after rename")
        with patch("pictures_org.cleanup.move_file", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.run_op("apply")
        self.assertEqual(read_json(self.plan_path)["actions"][0]["status"], "moving")
        self.assertEqual(self.run_op("apply"), 1)
        self.assertTrue(all(a["status"] == "quarantined" for a in read_json(self.plan_path)["actions"]))
        self.assertEqual(self.run_op("restore"), 2)

    def test_interrupted_restore_can_resume(self):
        self.plan()
        self.run_op("apply")
        def interrupt(source, target):
            move_file(source, target)
            raise OSError("simulated interruption")
        with patch("pictures_org.cleanup.move_file", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.run_op("restore")
        self.assertEqual(self.run_op("restore"), 1)
        self.assertEqual(len(list(self.root.glob("*.jpg"))), 3)

    def test_restore_refuses_to_overwrite(self):
        plan = self.plan()
        self.run_op("apply")
        source = Path(plan["actions"][0]["source"])
        source.write_bytes(b"new memory")
        with self.assertRaises(SafetyError):
            self.run_op("restore")
        self.assertEqual(source.read_bytes(), b"new memory")
        self.assertTrue(Path(plan["actions"][0]["destination"]).exists())

    def test_purge_requires_confirmation_and_preserves_keeper(self):
        plan = self.plan()
        self.run_op("apply")
        with self.assertRaises(SafetyError):
            self.run_op("purge", confirm="wrong")
        self.assertEqual(self.run_op("purge", confirm=plan["id"]), 2)
        self.assertEqual(self.run_op("purge", confirm=plan["id"]), 0)
        self.assertEqual(Path(plan["actions"][0]["keeper"]).read_bytes(), self.content)

    def test_missing_keeper_blocks_permanent_deletion(self):
        plan = self.plan()
        self.run_op("apply")
        Path(plan["actions"][0]["keeper"]).unlink()
        with self.assertRaises(OSError):
            self.run_op("purge", confirm=plan["id"])
        self.assertTrue(Path(plan["actions"][0]["destination"]).exists())
        self.assertEqual(self.run_op("restore"), 2)

    def test_modified_quarantine_blocks_purge(self):
        plan = self.plan()
        self.run_op("apply")
        Path(plan["actions"][0]["destination"]).write_bytes(b"different")
        with self.assertRaises(SafetyError):
            self.run_op("purge", confirm=plan["id"])

    def test_keeper_change_after_duplicate_hash_blocks_purge(self):
        plan = self.plan()
        self.run_op("apply")
        from pictures_org.common import check_content
        def alter_keeper_after_target_read(path, size, digest):
            result = check_content(path, size, digest)
            if str(path) == plan["actions"][0]["destination"]:
                Path(plan["actions"][0]["keeper"]).write_bytes(b"changed while target was read")
            return result
        with patch("pictures_org.cleanup.check_content", side_effect=alter_keeper_after_target_read):
            with self.assertRaises(SafetyError):
                self.run_op("purge", confirm=plan["id"])
        self.assertTrue(Path(plan["actions"][0]["destination"]).exists())

    def test_restore_remaining_copies_after_partial_purge(self):
        self.plan()
        self.run_op("apply")
        data = read_json(self.plan_path)
        data["actions"][0]["status"] = "purged"
        self.interrupted_state(0, "purged")
        Path(data["actions"][0]["destination"]).unlink()
        write_json(self.plan_path, data)
        self.assertEqual(self.run_op("restore"), 1)
        self.assertTrue(Path(data["actions"][1]["source"]).exists())

    def test_windows_reparse_attribute_is_rejected_without_following_it(self):
        original = Path.lstat
        linked = self.root / "junction"
        def fake_lstat(path):
            if path == linked:
                return SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400)
            return original(path)
        from pictures_org.common import plain_path
        with patch.object(Path, "lstat", fake_lstat):
            with self.assertRaises(SafetyError):
                plain_path(linked / "hidden.jpg")

    def test_interrupted_purge_missing_target_reconciles(self):
        plan = self.plan()
        self.run_op("apply")
        data = read_json(self.plan_path)
        data["actions"][0]["status"] = "purging"
        self.interrupted_state(0, "purging")
        Path(data["actions"][0]["destination"]).unlink()
        write_json(self.plan_path, data)
        self.assertEqual(self.run_op("purge", confirm=plan["id"]), 1)

    def test_path_escape_in_plan_is_rejected(self):
        plan = self.plan()
        plan["actions"][0]["destination"] = str(self.base / "unrelated.jpg")
        write_json(self.plan_path, plan)
        with self.assertRaises(SafetyError):
            self.run_op("apply")

    def test_journal_recovers_when_final_json_snapshot_was_not_written(self):
        self.plan()
        with patch("pictures_org.cleanup.write_json", side_effect=OSError("snapshot unavailable")):
            with self.assertRaises(OSError):
                self.run_op("apply")
        self.assertTrue(all(a["status"] == "pending" for a in read_json(self.plan_path)["actions"]))
        self.assertEqual(self.run_op("apply"), 0)
        self.assertTrue(all(a["status"] == "quarantined" for a in read_json(self.plan_path)["actions"]))
        self.assertEqual(self.run_op("restore"), 2)

    def test_journal_refuses_changed_plan_after_apply(self):
        self.plan()
        self.run_op("apply")
        data = read_json(self.plan_path)
        data["actions"][0]["sha256"] = "f" * 64
        write_json(self.plan_path, data)
        with self.assertRaises(SafetyError):
            self.run_op("restore")

    def test_keeper_cannot_also_be_a_removal_source(self):
        plan = self.plan()
        plan["actions"][0]["keeper"] = plan["actions"][1]["source"]
        write_json(self.plan_path, plan)
        with self.assertRaises(SafetyError):
            self.run_op("apply")

    def test_existing_plan_is_not_overwritten(self):
        self.plan()
        original = self.plan_path.read_bytes()
        with self.assertRaises(SafetyError):
            make_plan(self.workspace / "report.json", self.trash, self.plan_path)
        self.assertEqual(self.plan_path.read_bytes(), original)

    def test_plan_lock_and_collection_lock_block_concurrent_operations(self):
        self.plan()
        lock = Path(str(self.plan_path) + ".lock")
        lock.write_text("another process")
        with self.assertRaises(SafetyError):
            self.run_op("apply")
        lock.unlink()
        (self.root / ".pictures-org-cleanup.lock").write_text("another process")
        with self.assertRaises(SafetyError):
            self.run_op("apply")

    def test_source_and_workspace_cannot_overlap(self):
        with self.assertRaises(SafetyError):
            scan(self.root, self.root / "reports", progress=quiet)

    def test_quarantine_inside_source_is_rejected(self):
        self.scan()
        with self.assertRaises(SafetyError):
            make_plan(self.workspace / "report.json", self.root / "trash", self.plan_path)

    def test_hardlinks_are_not_counted_as_extra_physical_copies(self):
        try:
            os.link(self.root / "a.jpg", self.root / "alias.jpg")
        except OSError as exc:
            self.skipTest(str(exc))
        report = self.scan()
        self.assertEqual(report["summary"]["extra_copies"], 2)
        self.assertTrue(any("Hardlink alias" in s for s in report["summary"]["skipped"]))

    def test_symlink_directory_is_not_followed(self):
        outside = self.base / "private"
        outside.mkdir()
        (outside / "secret.jpg").write_bytes(self.content)
        try:
            (self.root / "linked").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        report = self.scan()
        self.assertEqual(report["summary"]["files_scanned"], 3)
        self.assertTrue(report["summary"]["skipped"])

    def test_empty_files_are_inventoried_but_not_quarantined(self):
        (self.root / "empty.jpg").touch()
        (self.root / "also-empty.jpg").touch()
        report = self.scan()
        self.assertEqual(report["summary"]["files_scanned"], 5)
        self.assertEqual(report["summary"]["extra_copies"], 2)


if __name__ == "__main__":
    unittest.main()
