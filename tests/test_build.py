"""Packaging: generated files in sync, manifests valid, archives deterministic and complete."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from support.pkg import REPO, load_builder

B = load_builder()
P = B.load_product()
CODEX_VALIDATOR = Path("/opt/codex-skills/skills/.system/plugin-creator/scripts/validate_plugin.py")


def fixture_copy(dst):
    """A minimal repository copy: plugin, catalogs and packaging metadata."""
    for rel in (P["pluginDir"], ".agents", ".cursor-plugin", "packaging"):
        shutil.copytree(str(REPO / rel), str(dst / rel), symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(str(REPO / "LICENSE"), str(dst / "LICENSE"))
    return dst


class RepositoryTests(unittest.TestCase):
    def test_tree_is_in_sync_and_valid(self):
        self.assertEqual([], B.drift(P))
        self.assertEqual([], B.validate(P))

    def test_bundled_copies_match_canonical_sources(self):
        sources = json.loads((REPO / P["pluginDir"] / "scripts" / "SOURCES.json").read_text())["files"]
        self.assertIn("scripts/tailmsg.py", sources)
        for dst, info in sources.items():
            self.assertEqual((REPO / info["source"]).read_bytes(), (REPO / P["pluginDir"] / dst).read_bytes(), dst)

    def test_manifests_agree_on_identity(self):
        base = REPO / P["pluginDir"]
        manifests = [json.loads((base / rel).read_text()) for rel in
                     ("plugin.json", ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json")]
        self.assertEqual({P["id"]}, {m["name"] for m in manifests})
        self.assertEqual({P["version"]}, {m["version"] for m in manifests})
        self.assertNotIn("extensions", manifests[0])          # keeps the .codex-plugin overlay in effect
        self.assertEqual({P["repository"]}, {m["repository"] for m in manifests})
        self.assertNotIn("homepage", manifests[0])            # no website beyond the repository
        codex_market = json.loads((REPO / ".agents/plugins/marketplace.json").read_text())
        cursor_market = json.loads((REPO / ".cursor-plugin/marketplace.json").read_text())
        self.assertEqual("./" + P["pluginDir"], codex_market["plugins"][0]["source"]["path"])
        self.assertEqual(P["pluginDir"], cursor_market["plugins"][0]["source"])

    @unittest.skipUnless(CODEX_VALIDATOR.is_file(), "Codex plugin-creator validator not installed")
    def test_official_codex_validator_accepts_plugin(self):
        try:
            import yaml  # noqa: F401  (the validator needs PyYAML)
        except ImportError:
            self.skipTest("PyYAML not installed")
        r = subprocess.run([sys.executable, str(CODEX_VALIDATOR), str(REPO / P["pluginDir"])],
                           capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)

    def test_skills_are_discoverable_and_locate_the_bundled_command(self):
        skills = sorted((REPO / P["pluginDir"] / "skills").iterdir())
        self.assertEqual(["yobiru-inbox", "yobiru-send", "yobiru-sessions", "yobiru-setup", "yobiru-upgrade",
                          "yobiru-wake"], [s.name for s in skills])
        for s in skills:
            text = (s / "SKILL.md").read_text()
            self.assertTrue(text.startswith("---\nname: %s\n" % s.name))
            self.assertIn("../../scripts/yobiru", text)
            self.assertTrue((s / ".." / ".." / "scripts" / "yobiru").resolve().is_file())


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archives = B.archives(P)
        cls.plugin_zip = cls.archives["%s-%s-plugin.zip" % (P["id"], P["version"])]
        cls.market_zip = cls.archives["%s-%s-marketplace.zip" % (P["id"], P["version"])]

    def names(self, data):
        return zipfile.ZipFile(io.BytesIO(data)).namelist()

    def test_build_is_deterministic(self):
        self.assertEqual(self.archives, B.archives(P))

    def test_plugin_archive_holds_exactly_the_plugin(self):
        names = self.names(self.plugin_zip)
        self.assertEqual(sorted(names), names)
        expected = ["%s/%s" % (P["id"], rel) for rel in B.plugin_files(P)]
        self.assertEqual(expected, names)
        for rel in ("plugin.json", ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json", "README.md",
                    "LICENSE", "scripts/yobiru", "scripts/yobiru.py", "scripts/tailmsg.py",
                    "scripts/codex_bridge/install_codex_bridge.py", "assets/logo.svg", "assets/icon.png",
                    "skills/yobiru-setup/SKILL.md", "skills/yobiru-send/SKILL.md",
                    "skills/yobiru-inbox/SKILL.md", "skills/yobiru-wake/SKILL.md",
                    "skills/yobiru-sessions/SKILL.md", "skills/yobiru-upgrade/SKILL.md"):
            self.assertIn("%s/%s" % (P["id"], rel), names)

    def test_archive_has_no_runtime_state_or_junk(self):
        for name in self.names(self.plugin_zip) + self.names(self.market_zip):
            parts = name.split("/")
            self.assertFalse(set(parts) & {"__pycache__", "inbox", "outbox", "test-run", ".live-test", "dist"}, name)
            self.assertFalse(name.endswith((".pyc", ".sqlite3", ".log", ".bak", "config.json")), name)

    def test_modes_and_timestamps_are_fixed(self):
        for info in zipfile.ZipFile(io.BytesIO(self.plugin_zip)).infolist():
            self.assertEqual((1980, 1, 1, 0, 0, 0), info.date_time)
            mode = info.external_attr >> 16
            want = 0o100755 if info.filename.split("/", 1)[1] in B.executables(P) else 0o100644
            self.assertEqual(want, mode, info.filename)

    def test_marketplace_bundle_is_a_usable_repository_layout(self):
        names = self.names(self.market_zip)
        root = "%s-marketplace/" % P["id"]
        for rel in (".agents/plugins/marketplace.json", ".cursor-plugin/marketplace.json", "LICENSE",
                    P["pluginDir"] + "/.codex-plugin/plugin.json", P["pluginDir"] + "/skills/yobiru-setup/SKILL.md"):
            self.assertIn(root + rel, names)


class FixtureTests(unittest.TestCase):
    """The checks must catch what would make an archive differ from the checked tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = fixture_copy(Path(self.tmp.name))
        self.base = self.root / P["pluginDir"]

    def test_fixture_copy_is_clean(self):
        self.assertEqual([], B.validate(P, self.root))
        self.assertEqual([], B.drift(P, self.root))

    def test_symlinked_skills_directory_is_rejected_not_dropped(self):
        outside = Path(self.tmp.name) / "outside-skills"
        shutil.move(str(self.base / "skills"), str(outside))
        os.symlink(str(outside), str(self.base / "skills"))
        self.assertTrue(any("symlink" in e and "skills" in e for e in B.validate(P, self.root)))
        self.assertTrue(any("symlink" in e for e in B.drift(P, self.root)))
        with self.assertRaises(B.BuildError):
            B.archives(P, self.root)

    def test_symlinked_file_and_plugin_root_are_rejected(self):
        os.symlink(str(self.base / "README.md"), str(self.base / "assets" / "linked.md"))
        self.assertTrue(any("assets/linked.md" in e for e in B.validate(P, self.root)))
        os.remove(str(self.base / "assets" / "linked.md"))
        real = Path(self.tmp.name) / "real-plugin"
        shutil.move(str(self.base), str(real))
        os.symlink(str(real), str(self.base))
        self.assertTrue(any(P["pluginDir"] in e for e in B.validate(P, self.root)))
        with self.assertRaises(B.BuildError):
            B.plugin_files(P, self.root)

    def test_runtime_state_and_private_data_are_rejected(self):
        # Synthetic values only (made-up tailnet, address, user and key) that the scanner must reject.
        (self.base / "scripts" / "config.json").write_text("{}")
        (self.base / "skills" / "yobiru-send" / "notes.md").write_text(
            "ping laptop.tail1a2b3c.ts.net at 100.101.102.103 for /home/alice/.tailmsg, token tskey-abcdef123456\n")
        errors = "\n".join(B.validate(P, self.root))
        for needle in ("config.json", "tailnet MagicDNS", "Tailscale IPv4", "home directory", "auth key"):
            self.assertIn(needle, errors)

    def test_private_denylist_from_environment(self):
        deny = Path(self.tmp.name) / "deny.txt"
        deny.write_text("my-secret-hostname\n")
        (self.base / "README.md").write_text((self.base / "README.md").read_text() + "\nmy-secret-hostname\n")
        os.environ["YOBIRU_PRIVATE_DENYLIST"] = str(deny)
        try:
            self.assertTrue(any("private marker" in e for e in B.validate(P, self.root)))
        finally:
            del os.environ["YOBIRU_PRIVATE_DENYLIST"]

    def test_manifest_drift_and_bad_skill_names_are_reported(self):
        m = self.base / ".cursor-plugin" / "plugin.json"
        m.write_text(m.read_text().replace('"license": "MIT"', '"license": "Apache-2.0"'))
        self.assertTrue(any(".cursor-plugin/plugin.json is out of date" in e for e in B.drift(P, self.root)))
        skill = self.base / "skills" / "yobiru-send" / "SKILL.md"
        skill.write_text(skill.read_text().replace("name: yobiru-send", "name: send"))
        self.assertTrue(any("skills/yobiru-send" in e for e in B.validate(P, self.root)))


if __name__ == "__main__":
    unittest.main()
