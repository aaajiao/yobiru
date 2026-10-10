"""Offline checks for official-CLI delivery and conservative reconciliation."""

import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from codex_queue_client import APP_EXECUTABLE, CodexAppClient, CodexAppError, _user_markers


class CodexQueueClientTests(unittest.TestCase):
    def setUp(self):
        self.client = CodexAppClient({"dispatcher_thread_id": "dispatcher-fixed"})
        self.args = {"threadId": "dispatcher-fixed", "hostId": "local", "prompt": "[TAILMSG:message_123]"}
        self.runtime = patch("codex_queue_client.Path.is_file", return_value=True)
        self.access = patch("codex_queue_client.os.access", return_value=True)
        self.runtime.start()
        self.access.start()
        self.addCleanup(self.runtime.stop)
        self.addCleanup(self.access.stop)

    def test_fixed_local_thread_only(self):
        for key, value in (("threadId", "another-thread"), ("hostId", "remote")):
            with self.subTest(key=key), patch("codex_queue_client.subprocess.run") as run:
                with self.assertRaises(CodexAppError) as caught:
                    self.client.call_tool("send_message_to_thread", dict(self.args, **{key: value}))
                self.assertFalse(caught.exception.delivery_unknown)
                run.assert_not_called()

    def test_app_closed_does_not_attempt_queue(self):
        with patch("codex_queue_client.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaises(CodexAppError) as caught:
                self.client.call_tool("send_message_to_thread", self.args)
            self.assertFalse(caught.exception.delivery_unknown)
            self.assertEqual("app_unavailable", caught.exception.code)
            self.assertEqual(1, run.call_count)

    def test_app_process_matching_uses_exact_executable(self):
        for executable, expected in ((APP_EXECUTABLE, True), (APP_EXECUTABLE + " Helper", False), (APP_EXECUTABLE + "/other", False)):
            with self.subTest(executable=executable), patch("codex_queue_client.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=" 54388 " + executable + "\n")):
                self.assertEqual(expected, self.client._app_running())

    def test_codex_path_config_alias(self):
        self.assertEqual("/custom/codex", CodexAppClient({"dispatcher_thread_id": "dispatcher-fixed", "codex_path": "/custom/codex"}).cli_path)

    def test_success_uses_official_queue_without_permission_overrides(self):
        with patch.object(self.client, "_app_running", return_value=True), patch(
            "codex_queue_client.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="Queued message 33333333-3333-4333-8333-333333333333 for thread dispatcher-fixed.\n")
        ) as run:
            result = self.client.call_tool("send_message_to_thread", self.args)
            self.assertEqual("enqueued", result["status"])
            self.assertEqual([self.client.cli_path, "queue", "--thread", "dispatcher-fixed", "--message", self.args["prompt"]], run.call_args.args[0])
            self.assertFalse(run.call_args.kwargs["check"])

    def test_zero_exit_without_matching_receipt_remains_unknown(self):
        for output in ("", "Queued message queue-id-123 for thread another-thread.\n"):
            with self.subTest(output=output), patch.object(self.client, "_app_running", return_value=True), patch("codex_queue_client.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=output)):
                with self.assertRaises(CodexAppError) as caught:
                    self.client.call_tool("send_message_to_thread", self.args)
                self.assertTrue(caught.exception.delivery_unknown)

    def test_nonzero_exit_remains_unknown(self):
        with patch.object(self.client, "_app_running", return_value=True), patch(
            "codex_queue_client.subprocess.run", return_value=SimpleNamespace(returncode=1)
        ):
            with self.assertRaises(CodexAppError) as caught:
                self.client.call_tool("send_message_to_thread", self.args)
            self.assertTrue(caught.exception.delivery_unknown)

    def test_timeout_remains_unknown(self):
        with patch.object(self.client, "_app_running", return_value=True), patch(
            "codex_queue_client.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 30)
        ):
            with self.assertRaises(CodexAppError) as caught:
                self.client.call_tool("send_message_to_thread", self.args)
            self.assertTrue(caught.exception.delivery_unknown)

    def test_spawn_failure_proves_no_delivery(self):
        with patch.object(self.client, "_app_running", return_value=True), patch(
            "codex_queue_client.subprocess.run", side_effect=FileNotFoundError()
        ):
            with self.assertRaises(CodexAppError) as caught:
                self.client.call_tool("send_message_to_thread", self.args)
            self.assertFalse(caught.exception.delivery_unknown)

    def test_only_user_markers_are_returned(self):
        thread = {"turns": [{"items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "private body [TAILMSG:message_123]"}]},
            {"type": "agentMessage", "text": "[TAILMSG:agent_fake]"},
            {"type": "commandExecution", "aggregatedOutput": "[TAILMSG:tool_fake]"},
        ]}]}
        markers = _user_markers(thread, 100)
        self.assertEqual(["[TAILMSG:message_123]"], markers)
        self.assertNotIn("private", str(markers))

    def test_recent_window_does_not_claim_complete_history(self):
        thread = {"turns": [{"items": [{"type": "userMessage", "content": [{"type": "text", "text": "[TAILMSG:old_message]"}]}]}, {"items": []}]}
        self.assertEqual([], _user_markers(thread, 1))

    def test_read_setup_failure_releases_process_and_streams(self):
        for setup in ("constructor", "registration"):
            with self.subTest(setup=setup):
                process = MagicMock()
                process.poll.return_value = None
                selector = MagicMock()
                if setup == "constructor":
                    factory = patch("codex_queue_client.selectors.DefaultSelector", side_effect=OSError("selector unavailable"))
                else:
                    selector.register.side_effect = OSError("registration failed")
                    factory = patch("codex_queue_client.selectors.DefaultSelector", return_value=selector)
                with patch("codex_queue_client.subprocess.Popen", return_value=process), factory:
                    with self.assertRaises(CodexAppError) as caught:
                        self.client.call_tool("read_thread", {"threadId": "dispatcher-fixed"})
                self.assertFalse(caught.exception.delivery_unknown)
                process.terminate.assert_called_once_with()
                process.wait.assert_called_once_with(timeout=2)
                process.stdin.close.assert_called_once_with()
                process.stdout.close.assert_called_once_with()
                if setup == "registration":
                    selector.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
