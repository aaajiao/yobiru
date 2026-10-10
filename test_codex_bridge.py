"""Offline queue tests; no actual app, session, network, or Tailmsg replies."""
import contextlib
import datetime
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_bridge import Bridge, Rejected, load_config


class DeliveryError(Exception):
    def __init__(self, unknown):
        self.delivery_unknown = unknown
        super().__init__("private content must never be persisted")


class FakeClient:
    def __init__(self):
        self.calls = []
        self.send_error = None
        self.send_result = {"status": "queued"}
        self.read_result = {"turns": []}
        self.closes = 0

    def close(self):
        self.closes += 1

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "send_message_to_thread":
            if self.send_error is not None:
                raise self.send_error
            return self.send_result
        if name == "read_thread":
            return self.read_result
        raise AssertionError(name)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tailmsg = self.root / "tailmsg"
        self.inbox = self.tailmsg / "inbox"
        self.inbox.mkdir(parents=True)
        self.local = {"allow": ["grok-bot"], "default_agent": "codex"}
        self.write_local()
        self.now = 1700000000.0
        self.client = FakeClient()
        self.config = {
            "bridge_home": str(self.root / "state"), "tailmsg_home": str(self.tailmsg),
            "dispatcher_thread_id": "dispatcher-fixed", "dispatcher_host_id": "local",
            "retry_seconds": 10, "batch_delay_seconds": 30, "poll_seconds": 1,
        }
        self.bridge = self.open_bridge()
        self.addCleanup(lambda: self.bridge.close())

    def open_bridge(self):
        return Bridge(self.config, client_factory=lambda _: self.client, clock=lambda: self.now)

    def write_local(self):
        (self.tailmsg / "config.json").write_text(json.dumps(self.local), encoding="utf-8")

    def message(self, mid="message_0001", wake="now", **overrides):
        date = datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "envelope": {"v": 1, "id": mid, "from": "grok-bot.tailnet.ts.net", "agent": "codex",
                         "text": "PRIVATE body $(malicious) `command`", "wake": wake},
            "received": {"at": date, "peer": {"node": "grok-bot.tailnet.ts.net",
                         "host": "grok-bot", "tags": [], "user": "test@example.invalid"},
                         "agent": "codex", "agent_status": "known",
                         "wake": {"requested": wake, "effective": wake}},
        }
        record.update(overrides)
        path = self.inbox / (mid + ".json")
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def rewrite(self, path, update):
        record = json.loads(path.read_text(encoding="utf-8"))
        update(record)
        path.write_text(json.dumps(record), encoding="utf-8")

    def state(self):
        return self.bridge.status()[0]["state"]

    def test_deduplication_and_metadata_only_notification(self):
        path = self.message()
        before = path.read_bytes()
        self.assertEqual("queued", self.bridge.ingest(path)["status"])
        self.assertEqual("duplicate", self.bridge.ingest(path)["status"])
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        self.bridge.scan()
        self.bridge.dispatch_due()
        self.assertEqual(1, len(self.client.calls))
        name, args = self.client.calls[0]
        self.assertEqual("send_message_to_thread", name)
        self.assertEqual("dispatcher-fixed", args["threadId"])
        self.assertIn("[TAILMSG:message_0001]", args["prompt"])
        self.assertIn(str(path), args["prompt"])
        self.assertNotIn("PRIVATE", args["prompt"])
        self.assertNotIn("PRIVATE", json.dumps(self.bridge.status()))
        self.assertEqual(before, path.read_bytes())  # Does not write ack/wake metadata.
        self.assertEqual("enqueued", self.state())
        self.assertEqual(1, self.client.closes)

    def test_rejects_unknown_peer_and_sender_mismatch(self):
        path = self.message()
        self.local["allow"] = ["other-node"]
        self.write_local()
        with self.assertRaisesRegex(Rejected, "peer_not_allowed"):
            self.bridge.ingest(path)
        self.local["allow"] = ["grok-bot"]
        self.write_local()
        self.rewrite(path, lambda r: r["envelope"].update({"from": "other-node"}))
        with self.assertRaisesRegex(Rejected, "sender_mismatch"):
            self.bridge.ingest(path)
        self.assertEqual([], self.bridge.status())

    def test_rejects_agent_conflicts_and_missing_receipt(self):
        path = self.message()
        self.rewrite(path, lambda r: r["envelope"].update({"agent": "grok"}))
        with self.assertRaisesRegex(Rejected, "wrong_agent"):
            self.bridge.ingest(path)
        path = self.message()
        self.rewrite(path, lambda r: r["received"].update({"agent_status": "unknown"}))
        with self.assertRaisesRegex(Rejected, "wrong_agent"):
            self.bridge.ingest(path)
        path = self.message(received=None)
        with self.assertRaisesRegex(Rejected, "missing_verified_receipt"):
            self.bridge.ingest(path)

    def test_rejects_wrong_paths_symlinks_and_filename_id_mismatch(self):
        path = self.message()
        outside = self.root / path.name
        outside.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(Rejected, "invalid_inbox_path"):
            self.bridge.ingest(outside)
        link = self.inbox / "message_link.json"
        link.symlink_to(outside)
        with self.assertRaisesRegex(Rejected, "invalid_inbox_path"):
            self.bridge.ingest(link)
        self.rewrite(path, lambda r: r["envelope"].update({"id": "different_0001"}))
        with self.assertRaisesRegex(Rejected, "invalid_message_id"):
            self.bridge.ingest(path)

    def test_none_and_acked_are_never_automatically_dispatched(self):
        self.message(wake="none")
        self.message("message_0002", acked_at="2020-01-01T00:00:00Z")
        result = self.bridge.scan()
        self.assertEqual(2, result["skipped"])
        self.bridge.dispatch_due()
        self.assertEqual([], self.client.calls)
        self.assertEqual([], self.bridge.status())

    def test_batch_delayed_and_now_immediate(self):
        self.bridge.ingest(self.message(wake="batch"))
        self.bridge.ingest(self.message("message_0002", wake="now"))
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        self.now += 29
        self.assertEqual(0, self.bridge.dispatch_due()["enqueued"])
        self.now += 1
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        self.assertEqual(2, len(self.client.calls))

    def test_historical_messages_dispatch_by_receipt_time_not_filename(self):
        scanned_at = self.now
        self.now -= 120
        first = self.message("zzzz_first_0001")
        self.now += 60
        second = self.message("aaaa_second_0002")
        self.now = scanned_at
        self.assertEqual(2, self.bridge.scan()["queued"])
        self.assertEqual(2, self.bridge.dispatch_due()["enqueued"])
        prompts = [args["prompt"] for name, args in self.client.calls]
        self.assertIn("[TAILMSG:zzzz_first_0001]", prompts[0])
        self.assertIn("[TAILMSG:aaaa_second_0002]", prompts[1])
        rows = {row["id"]: row for row in self.bridge.status()}
        self.assertEqual(scanned_at, rows["zzzz_first_0001"]["created_at"])
        self.assertEqual(scanned_at - 120, rows["zzzz_first_0001"]["received_at"])
        self.assertEqual(scanned_at - 60, rows["aaaa_second_0002"]["received_at"])
        self.assertTrue(first.exists() and second.exists())

    def test_now_priority_precedes_older_due_batch(self):
        scanned_at = self.now
        self.now -= 120
        self.message("aaaa_batch_0001", wake="batch")
        self.now = scanned_at
        self.message("zzzz_now_0002", wake="now")
        self.bridge.scan()
        self.assertEqual(2, self.bridge.dispatch_due()["enqueued"])
        self.assertIn("[TAILMSG:zzzz_now_0002]", self.client.calls[0][1]["prompt"])

    def test_existing_schema_migrates_verified_receipt_without_fabricating_missing_time(self):
        original_now = self.now
        self.now -= 60
        path = self.message()
        self.now = original_now
        self.bridge.close()
        db_path = Path(self.config["bridge_home"]) / "bridge.sqlite3"
        db_path.unlink()
        with contextlib.closing(sqlite3.connect(str(db_path))) as legacy:
            legacy.execute("""CREATE TABLE messages (
                id TEXT PRIMARY KEY, path TEXT NOT NULL, wake TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                error TEXT, thread_id TEXT NOT NULL, host_id TEXT NOT NULL)""")
            for mid, source in (("message_0001", path), ("missing_0001", self.inbox / "missing_0001.json")):
                legacy.execute("INSERT INTO messages VALUES (?,?,?,'pending',0,?,?,?,NULL,?,?)",
                    (mid, str(source), "now", self.now, self.now - 10, self.now - 5,
                     self.config["dispatcher_thread_id"], "local"))
            legacy.commit()
        self.bridge = self.open_bridge()
        rows = {row["id"]: row for row in self.bridge.status()}
        self.assertEqual(self.now - 60, rows["message_0001"]["received_at"])
        self.assertIsNone(rows["missing_0001"]["received_at"])
        self.assertEqual(self.now - 10, rows["message_0001"]["created_at"])
        self.assertEqual(self.now - 5, rows["message_0001"]["updated_at"])
        self.bridge.close()
        self.bridge = self.open_bridge()  # Migration is safe to run again.
        dispatched = self.bridge.dispatch_due()
        self.assertEqual(1, dispatched["enqueued"])
        self.assertEqual(1, dispatched["skipped"])
        self.assertEqual(1, len(self.client.calls))

    def test_known_not_sent_failure_retries_after_backoff(self):
        self.bridge.ingest(self.message())
        self.client.send_error = DeliveryError(False)
        self.assertEqual(1, self.bridge.dispatch_due()["retry"])
        self.assertEqual("retry", self.state())
        self.assertNotIn("private content", json.dumps(self.bridge.status()))
        self.now += 9
        self.bridge.dispatch_due()
        self.assertEqual(1, len(self.client.calls))
        self.client.send_error = None
        self.now += 1
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        self.assertEqual(2, self.bridge.status()[0]["attempts"])

    def test_explicit_tool_rejection_retries(self):
        self.bridge.ingest(self.message())
        self.client.send_result = {"ok": False, "error": "private rejection details"}
        self.assertEqual(1, self.bridge.dispatch_due()["retry"])
        self.assertNotIn("private rejection", json.dumps(self.bridge.status()))

    def test_unknown_send_only_reconciles_and_never_blindly_resends(self):
        self.bridge.ingest(self.message())
        self.client.send_error = DeliveryError(True)
        self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])
        self.client.send_error = None
        for _ in range(3):
            self.now += 10
            self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])
        self.assertEqual(1, sum(name == "send_message_to_thread" for name, _ in self.client.calls))
        self.client.read_result = {"turns": [{"input": "[TAILMSG:message_0001]"}]}
        self.now += 10
        self.assertEqual(1, self.bridge.dispatch_due()["delivered"])
        self.assertEqual("delivered", self.state())

    def test_unclassified_failure_is_conservatively_uncertain(self):
        self.bridge.ingest(self.message())
        self.client.send_error = TimeoutError("private timeout details")
        self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])

    def test_restart_preserves_deduplication_and_recovers_inflight_sends(self):
        path = self.message()
        self.bridge.ingest(path)
        self.bridge.db.execute("UPDATE messages SET state='sending',attempts=1")
        self.bridge.db.commit()
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.assertEqual("duplicate", self.bridge.ingest(path)["status"])
        self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])
        self.assertEqual(["read_thread"], [name for name, _ in self.client.calls])
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.assertEqual("uncertain", self.state())

    def test_revoked_allowlist_is_checked_before_dispatch(self):
        self.bridge.ingest(self.message())
        self.local["allow"] = []
        self.write_local()
        self.assertEqual(1, self.bridge.dispatch_due()["skipped"])
        self.assertEqual([], self.client.calls)

    def test_single_dispatch_process_lock(self):
        import fcntl
        self.bridge.ingest(self.message())
        with (self.bridge.home / "dispatch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(0, self.bridge.dispatch_due()["enqueued"])
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])

    def test_config_rejects_invalid_timing(self):
        path = self.root / "bridge-config.json"
        for value in (True, -1, "fast", float("inf")):
            config = dict(self.config, poll_seconds=value)
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)

    def test_custom_config_path_and_home_are_explicit_trusted_notification_metadata(self):
        path = self.root / "separate-settings" / "custom-bridge.json"
        path.parent.mkdir()
        values = dict(self.config, bridge_config_path="/untrusted/config-override.json")
        path.write_text(json.dumps(values), encoding="utf-8")
        loaded = load_config(path)
        self.assertEqual(str(path), loaded["bridge_config_path"])
        self.bridge.close()
        self.bridge = Bridge(loaded, client_factory=lambda _: self.client, clock=lambda: self.now)
        message_path = self.message()
        self.bridge.ingest(message_path)
        self.bridge.dispatch_due()
        prompt = self.client.calls[0][1]["prompt"]
        self.assertIn("Tailmsg home: " + json.dumps(str(self.tailmsg)), prompt)
        self.assertIn("Bridge state directory: " + json.dumps(self.config["bridge_home"]), prompt)
        self.assertIn("Bridge configuration path: " + json.dumps(str(path)), prompt)
        self.assertNotIn("/untrusted/", prompt)
        self.assertNotIn("~/.tailmsg", prompt)
        self.assertNotIn("PRIVATE", prompt)

    def test_schema_initialization_failure_closes_database(self):
        self.bridge.close()
        db_path = Path(self.config["bridge_home"]) / "bridge.sqlite3"
        db_path.unlink()
        connection = sqlite3.connect(str(db_path))
        connection.execute("CREATE TABLE messages (id TEXT PRIMARY KEY)")
        connection.commit()
        with patch("codex_bridge.sqlite3.connect", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):
                self.open_bridge()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_daemon_and_wake_can_migrate_same_legacy_schema_concurrently(self):
        path = self.message()
        self.bridge.close()
        db_path = Path(self.config["bridge_home"]) / "bridge.sqlite3"
        db_path.unlink()
        with contextlib.closing(sqlite3.connect(str(db_path))) as legacy:
            legacy.execute("""CREATE TABLE messages (
                id TEXT PRIMARY KEY, path TEXT NOT NULL, wake TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                error TEXT, thread_id TEXT NOT NULL, host_id TEXT NOT NULL)""")
        barrier = threading.Barrier(2)
        def migrate_and_ingest(_index):
            barrier.wait(timeout=5)
            bridge = self.open_bridge()
            try:
                status = bridge.ingest(path)["status"]
                return status, bridge.status()[0]["received_at"]
            finally:
                bridge.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(migrate_and_ingest, range(2)))
        self.assertEqual(["duplicate", "queued"], sorted(status for status, _ in results))
        self.assertEqual([self.now, self.now], [received for _, received in results])
        self.bridge = self.open_bridge()
        self.assertEqual(1, len(self.bridge.status()))


if __name__ == "__main__":
    unittest.main()
