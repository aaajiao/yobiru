"""Actual encoded subprocess pipes, isolated inboxes, and the unpacked release archive."""
import json
import subprocess
import sys
import unittest

from support.pkg import REPO, Package, snapshot


PKG = None
TEXT = "中文 café 日本語 🙂"
AGENT = "助手🙂"
MESSAGE_ID = "abcdef0123456789"


def setUpModule():
    global PKG
    PKG = Package()


def tearDownModule():
    PKG.cleanup()


class StdioTests(unittest.TestCase):
    def setUp(self):
        self.user = PKG.new_home(self.id().rsplit(".", 1)[-1])
        self.home = self.user / "数据🙂"
        (self.home / "inbox").mkdir(parents=True)
        (self.home / "outbox").mkdir()
        (self.home / "config.json").write_text(json.dumps({
            "self_agent": AGENT, "default_agent": AGENT, "agents": {AGENT: {}}
        }), encoding="utf-8")
        self.record = {
            "envelope": {"v": 1, "id": MESSAGE_ID, "from": "sender.example.ts.net",
                         "to": "receiver.example.ts.net", "agent": AGENT,
                         "from_agent": AGENT, "text": TEXT, "ts": "2026-10-12T00:00:00Z"},
            "received": {"at": "2026-10-12T00:00:00Z", "wake": {"effective": "batch"}}
        }
        (self.home / "inbox" / (MESSAGE_ID + ".json")).write_text(
            json.dumps(self.record, ensure_ascii=False), encoding="utf-8")

    def run_cli(self, entry, encoding, *args):
        # capture bytes: the child really encodes stdout/stderr using PYTHONIOENCODING.
        env = PKG.env(self.user, extra={"PYTHONIOENCODING": encoding + ":strict"})
        return subprocess.run([sys.executable, str(entry), "--home", str(self.home)] + list(args),
                              env=env, cwd=str(PKG.work), capture_output=True, timeout=20)

    def test_inbox_in_legacy_pipes_keeps_encoding_and_does_not_change_messages(self):
        before = snapshot(self.home)
        for entry in (REPO / "tailmsg.py", PKG.entry):
            for encoding in ("cp1252", "ascii", "utf-8"):
                with self.subTest(entry=entry.name, encoding=encoding):
                    r = self.run_cli(entry, encoding, "inbox", "--pending")
                    self.assertEqual(0, r.returncode, r.stderr)
                    self.assertEqual(b"", r.stderr)
                    self.assertIn(TEXT.encode(encoding, errors="backslashreplace"), r.stdout)
                    self.assertIn(AGENT.encode(encoding, errors="backslashreplace"), r.stdout)
        self.assertEqual(before, snapshot(self.home))

    def test_show_json_round_trips_bmp_and_emoji_on_legacy_pipes(self):
        for entry in (REPO / "tailmsg.py", PKG.entry):
            for encoding in ("cp1252", "ascii", "utf-8"):
                with self.subTest(entry=entry.name, encoding=encoding):
                    r = self.run_cli(entry, encoding, "show", MESSAGE_ID)
                    self.assertEqual(0, r.returncode, r.stderr)
                    self.assertEqual(self.record, json.loads(r.stdout.decode(encoding)))

    def test_wrapper_status_text_and_json_handle_unicode_names_and_paths(self):
        for encoding in ("cp1252", "ascii", "utf-8"):
            with self.subTest(encoding=encoding):
                text = self.run_cli(PKG.entry, encoding, "status", "--no-probe")
                self.assertEqual(0, text.returncode, text.stderr)
                self.assertIn(AGENT.encode(encoding, errors="backslashreplace"), text.stdout)
                data = self.run_cli(PKG.entry, encoding, "status", "--no-probe", "--json")
                self.assertEqual(0, data.returncode, data.stderr)
                status = json.loads(data.stdout.decode(encoding))
                self.assertEqual(AGENT, status["self_agent"])
                self.assertEqual(str(self.home), status["home"])
                self.assertEqual(AGENT, status["agents"][0]["agent"])

    def test_error_exit_codes_and_unicode_diagnostics_survive_legacy_pipes(self):
        for encoding in ("cp1252", "ascii"):
            for entry, args, exit_code in ((REPO / "tailmsg.py", ("show", AGENT), 1),
                                           (PKG.entry, ("不存在🙂",), 2)):
                with self.subTest(entry=entry.name, encoding=encoding):
                    r = self.run_cli(entry, encoding, *args)
                    self.assertEqual(exit_code, r.returncode, r.stderr)
                    self.assertIn(b"error:", r.stderr)
                    self.assertNotIn(b"Traceback", r.stderr)

    def test_import_does_not_reconfigure_callers_streams(self):
        for entry in (REPO / "tailmsg.py", PKG.entry):
            with self.subTest(entry=entry.name):
                env = PKG.env(self.user, extra={"PYTHONIOENCODING": "cp1252:strict"})
                r = subprocess.run([sys.executable, "-c",
                                    "import runpy, sys; runpy.run_path(sys.argv[1]); "
                                    "print(sys.stdout.encoding, sys.stdout.errors)", str(entry)],
                                   env=env, cwd=str(PKG.work), capture_output=True, timeout=20)
                self.assertEqual(0, r.returncode, r.stderr)
                self.assertEqual(b"cp1252 strict\n", r.stdout)


if __name__ == "__main__":
    unittest.main()
