"""Hardening in the canonical tailmsg.py that the plugin relies on."""
import importlib.util
import os
import tempfile
import unittest

from support.pkg import REPO

spec = importlib.util.spec_from_file_location("tailmsg_core_under_test", str(REPO / "tailmsg.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

ENV = {"v": 1, "id": "abcdef0123456789", "from": "a.example.ts.net", "to": "b.example.ts.net",
       "ts": "2026-10-10T00:00:00Z"}


class CoreTests(unittest.TestCase):
    def test_parse_config_is_pure_and_keeps_error_messages(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                cfg, agents = core.parse_config({"allow": ["x"], "agents": {"café": None}})
                self.assertEqual([], os.listdir(d))
            finally:
                os.chdir(old)
        self.assertEqual(7766, cfg["port"])
        self.assertEqual({"café": {}}, agents)
        with self.assertRaisesRegex(RuntimeError, r"config agents\['a/b'\]"):
            core.parse_config({"agents": {"a/b": {}}})
        with self.assertRaisesRegex(RuntimeError, "JSON object"):
            core.parse_config(["not", "an", "object"])

    def test_ids_with_trailing_newline_or_paths_are_rejected(self):
        self.assertIsNone(core.validate_env(dict(ENV)))
        self.assertEqual("bad id", core.validate_env(dict(ENV, id="abcdef0123456789\n")))
        self.assertEqual("bad reply_to", core.validate_env(dict(ENV, reply_to="abcdef01\n")))
        for ref in ("../config", "*", "", "a\nb", "a/b", "x" * 65):
            self.assertIsNone(core.MSG_REF_RE.match(ref), repr(ref))
        self.assertTrue(core.MSG_REF_RE.match("abcd"))

    def test_attachment_names_stay_inside_the_inbox(self):
        for raw, want in (("../../etc/passwd", "passwd"), ("..", "file"), ("a\\..\\b.txt", "b.txt"),
                          (".hidden", "hidden"), ("my report.pdf", "my report.pdf")):
            self.assertEqual(want, core.safe_name(raw))


if __name__ == "__main__":
    unittest.main()
