"""Offline installation and rollback checks; no services or real user files."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import install_codex_bridge as installer


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "tailmsg"
        self.home.mkdir()
        (self.home / "tailmsg.py").write_text("# existing receiver\n")
        self.original = b'{"allow":["grok-bot"],"agents":{"codex":{}},"port":7766}\n'
        (self.home / "config.json").write_bytes(self.original)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in installer.FILES:
            (self.source / name).write_text("version one\n")
        self.args = argparse.Namespace(
            thread_id="11111111-1111-4111-8111-111111111111", tailmsg_home=str(self.home),
            codex_path="/usr/bin/false", allow_replies=False, no_start=True, rollback=None)
        self.source_patch = patch.object(installer, "SOURCE", self.source)
        self.home_patch = patch.object(installer.Path, "home", return_value=self.root / "fake-user")
        self.run_patch = patch.object(installer.subprocess, "run")
        self.source_patch.start(); self.home_patch.start()
        self.run = self.run_patch.start()
        self.run.return_value.returncode = 1
        self.addCleanup(self.source_patch.stop); self.addCleanup(self.home_patch.stop); self.addCleanup(self.run_patch.stop)

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.args)
        return json.loads((self.home / "codex-bridge/installation.json").read_text())

    def rollback(self, backup):
        self.args.rollback = backup
        with contextlib.redirect_stdout(io.StringIO()):
            installer.rollback(self.args)

    def create_queue(self):
        path = self.home / "codex-bridge/bridge.sqlite3"
        path.parent.mkdir(exist_ok=True)
        with contextlib.closing(sqlite3.connect(str(path))) as db:
            db.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, state TEXT NOT NULL)")
            db.execute("INSERT INTO messages VALUES (?, ?, ?)",
                       ("pending_0001", self.args.thread_id, "pending"))
            db.commit()
        return path

    def files(self):
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in self.home.rglob("*") if path.is_file()}

    def test_first_install_does_not_create_a_queue(self):
        self.install()
        self.assertFalse((self.home / "codex-bridge/bridge.sqlite3").exists())

    def test_first_install_rollback_preserves_inbox_and_queue(self):
        receipt = self.install()
        queue = self.home / "codex-bridge/bridge.sqlite3"
        queue.write_bytes(b"retained queue")
        (self.home / "inbox").mkdir()
        inbox = self.home / "inbox/message.json"
        inbox.write_bytes(b"retained inbox")
        self.rollback(receipt["backup"])
        self.assertEqual(self.original, (self.home / "config.json").read_bytes())
        self.assertFalse((self.home / "codex-bridge.json").exists())
        self.assertFalse((self.home / "codex-local/codex_bridge.py").exists())
        self.assertEqual(b"retained queue", queue.read_bytes())
        self.assertEqual(b"retained inbox", inbox.read_bytes())
        self.assertTrue(all(call.args[0][1] == "print" for call in self.run.call_args_list))

    def test_upgrade_rollback_restores_code_instructions_and_receipt(self):
        self.install()
        expected = {str(path.relative_to(self.home)): path.read_bytes()
                    for path in self.home.rglob("*") if path.is_file() and "backups" not in path.parts}
        for name in installer.FILES:
            (self.source / name).write_text("version two\n")
        receipt = self.install()
        self.assertEqual("version two\n", (self.home / "codex-local/codex_bridge.py").read_text())
        self.rollback(receipt["backup"])
        for name, contents in expected.items():
            self.assertEqual(contents, (self.home / name).read_bytes(), name)

    def test_dispatcher_replacement_is_rejected_before_mutation(self):
        self.install()
        before = (self.home / "codex-bridge.json").read_bytes()
        self.args.thread_id = "22222222-2222-4222-8222-222222222222"
        with self.assertRaisesRegex(RuntimeError, "persistent dispatcher ID"):
            self.install()
        self.assertEqual(before, (self.home / "codex-bridge.json").read_bytes())

    def test_rollback_retained_sqlite_queue_blocks_dispatcher_replacement(self):
        receipt = self.install()
        queue = self.create_queue()
        queue_before = queue.read_bytes()
        self.rollback(receipt["backup"])
        self.assertFalse((self.home / "codex-bridge.json").exists())
        before = self.files()
        self.run.reset_mock()
        self.args.thread_id = "22222222-2222-4222-8222-222222222222"
        with self.assertRaisesRegex(RuntimeError, "persistent dispatcher ID.*existing queue"):
            self.install()
        self.assertEqual(before, self.files())
        self.assertEqual(queue_before, queue.read_bytes())
        self.run.assert_not_called()

    def test_rollback_retained_queue_allows_same_dispatcher_without_database_changes(self):
        receipt = self.install()
        queue = self.create_queue()
        before = queue.read_bytes()
        self.rollback(receipt["backup"])
        self.install()
        self.assertEqual(before, queue.read_bytes())
        self.assertEqual(self.args.thread_id, json.loads((self.home / "codex-bridge.json").read_text())["dispatcher_thread_id"])

    def test_dry_run_reports_plan_without_writes_or_service_calls(self):
        before = self.files()
        self.args.dry_run = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            installer.install(self.args)
        plan = json.loads(out.getvalue())
        self.assertTrue(plan["dry_run"])
        self.assertEqual(self.args.thread_id, plan["dispatcher_thread_id"])
        self.assertEqual(before, self.files())
        self.assertFalse((self.home / "backups").exists())
        self.run.assert_not_called()

    def test_dry_run_still_rejects_a_different_codex_adapter(self):
        config = json.loads(self.original)
        config["agents"]["codex"]["wake_adapter"] = {"type": "exec", "argv": ["/other/adapter"]}
        (self.home / "config.json").write_text(json.dumps(config))
        before = self.files()
        self.args.dry_run = True
        with self.assertRaisesRegex(RuntimeError, "different codex wake adapter"):
            installer.install(self.args)
        self.assertEqual(before, self.files())

    def test_rollback_dry_run_changes_nothing(self):
        receipt = self.install()
        before = self.files()
        self.args.dry_run = True
        out = io.StringIO()
        self.args.rollback = receipt["backup"]
        with contextlib.redirect_stdout(out):
            installer.rollback(self.args)
        plan = json.loads(out.getvalue())
        self.assertIn(str(self.home / "config.json"), plan["would_restore"])
        self.assertEqual(before, self.files())

    def test_unreadable_or_incompatible_queue_is_rejected_before_mutation(self):
        queue = self.home / "codex-bridge/bridge.sqlite3"
        queue.parent.mkdir()
        for contents in (b"not a sqlite database", b""):
            with self.subTest(contents=contents):
                queue.write_bytes(contents)
                before = self.files()
                self.run.reset_mock()
                with self.assertRaisesRegex(RuntimeError, "existing dispatcher queue cannot be read"):
                    self.install()
                self.assertEqual(before, self.files())
                self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
