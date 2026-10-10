"""Fixed-session Tailmsg delivery through the installed official Codex CLI.

CodexAppClient(config).call_tool('send_message_to_thread', arguments) uses the
official `codex queue` command. The desktop owns and executes the real session;
this client never creates/resumes a model session or changes its permissions.
`read_thread` uses a short-lived official app-server for a read-only thread/read
request, returning only Tailmsg markers from user messages for reconciliation.

Configuration: dispatcher_thread_id (or caller_thread_id), codex_cli_path
(optional), timeout (seconds, default 30). Only the fixed local dispatcher can
be addressed. No send is retried here. CodexAppError.delivery_unknown is True
after a queue subprocess times out or fails; reconcile before any retry.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from typing import Any, Mapping


DEFAULT_CLI_PATH = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
APP_EXECUTABLE = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MARKER_RE = re.compile(r"\[TAILMSG:[A-Za-z0-9_-]{8,64}\]")
QUEUE_RECEIPT_RE = re.compile(r"^Queued message ([A-Za-z0-9_-]{8,128}) for thread ([A-Za-z0-9_-]{8,128})\.$", re.MULTILINE)


class CodexAppError(RuntimeError):
    def __init__(self, message: str, delivery_unknown: bool = True, code: str = "queue_error"):
        super().__init__(message)
        self.delivery_unknown = bool(delivery_unknown)
        self.code = code


def _user_markers(thread: Mapping[str, Any], turn_limit: int) -> list[str]:
    """Agent output and tool output cannot be evidence of user-message delivery."""
    found: set[str] = set()
    for turn in thread.get("turns", [])[-turn_limit:]:
        for item in turn.get("items", []):
            if item.get("type") != "userMessage":
                continue
            for content in item.get("content", []):
                if content.get("type") in ("text", "inputText") and isinstance(content.get("text"), str):
                    found.update(MARKER_RE.findall(content["text"]))
    return sorted(found)


class CodexAppClient:
    def __init__(self, config: Mapping[str, Any] | None = None, **kwargs: Any):
        settings = dict(config or {})
        settings.update(kwargs)
        self.thread_id = settings.get("dispatcher_thread_id") or settings.get("caller_thread_id")
        self.cli_path = str(settings.get("codex_cli_path") or settings.get("codex_path") or DEFAULT_CLI_PATH)
        if not isinstance(self.thread_id, str) or not self.thread_id.strip():
            raise CodexAppError("Configure the fixed dispatcher thread ID.", False, "invalid_config")
        try:
            self.timeout = float(settings.get("timeout", settings.get("timeout_seconds", 30)))
        except (TypeError, ValueError) as error:
            raise CodexAppError("Invalid Codex queue timeout.", False, "invalid_config") from error
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise CodexAppError("Codex queue timeout must be between 0 and 300 seconds.", False, "invalid_config")

    def _validate(self, arguments: Mapping[str, Any]) -> None:
        if arguments.get("threadId") != self.thread_id:
            raise CodexAppError("Only the configured dispatcher thread may be addressed.", False, "thread_mismatch")
        if arguments.get("hostId", "local") != "local":
            raise CodexAppError("The dispatcher must be on the local host.", False, "host_mismatch")
        if not Path(self.cli_path).is_file() or not os.access(self.cli_path, os.X_OK):
            raise CodexAppError("The installed official Codex CLI is unavailable.", False, "runtime_unavailable")

    def _app_running(self) -> bool:
        try:
            result = subprocess.run(
                ["/bin/ps", "-ww", "-U", str(os.getuid()), "-o", "pid=,comm="],
                capture_output=True, text=True,
                timeout=min(2, self.timeout), check=False,
            )
            if result.returncode != 0:
                return False
            for line in result.stdout.splitlines():
                fields = line.strip().split(None, 1)
                if len(fields) == 2 and fields[1] == APP_EXECUTABLE:
                    return True
            return False
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _send(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise CodexAppError("A nonempty queue notification is required.", False, "invalid_request")
        if not self._app_running():
            raise CodexAppError("Codex desktop is not available; delivery was not attempted.", False, "app_unavailable")
        try:
            result = subprocess.run(
                [self.cli_path, "queue", "--thread", self.thread_id, "--message", prompt],
                capture_output=True, text=True, timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise CodexAppError("Official Codex queue timed out; delivery is unknown.", True, "queue_timeout") from error
        except OSError as error:
            raise CodexAppError("Official Codex queue could not start.", False, "runtime_unavailable") from error
        if result.returncode != 0:
            # The CLI can lose its response after the desktop accepted a queue
            # entry. A nonzero exit is therefore never proof of non-delivery.
            raise CodexAppError("Official Codex queue failed; delivery is unknown.", True, "queue_failed")
        receipts = QUEUE_RECEIPT_RE.findall(result.stdout)
        if len(receipts) != 1 or receipts[0][1] != self.thread_id:
            raise CodexAppError("Official Codex queue returned no valid acceptance receipt; delivery is unknown.", True, "queue_receipt_unknown")
        return {"ok": True, "threadId": self.thread_id, "hostId": "local", "status": "enqueued", "transport": "codex_queue", "queueId": receipts[0][0]}

    def _read(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            turn_limit = int(arguments.get("turnLimit", 100))
        except (TypeError, ValueError) as error:
            raise CodexAppError("Invalid thread read limit.", False, "invalid_request") from error
        if not 1 <= turn_limit <= 1000:
            raise CodexAppError("Thread read limit must be between 1 and 1000.", False, "invalid_request")
        try:
            process = subprocess.Popen(
                [self.cli_path, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise CodexAppError("Official Codex read-only app-server could not start.", False, "runtime_unavailable") from error
        selector = None
        buffer = bytearray()
        pending: list[dict[str, Any]] = []
        deadline = time.monotonic() + self.timeout

        def send(value: dict[str, Any]) -> None:
            process.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
            process.stdin.flush()

        def response(request_id: int) -> dict[str, Any]:
            while True:
                for index, value in enumerate(pending):
                    if value.get("id") == request_id:
                        return pending.pop(index)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise CodexAppError("Official Codex thread read timed out.", False, "read_timeout")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise CodexAppError("Official Codex read-only app-server closed.", False, "read_failed")
                buffer.extend(chunk)
                if len(buffer) > MAX_RESPONSE_BYTES:
                    raise CodexAppError("Official Codex thread read exceeds 8 MiB.", False, "response_too_large")
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer[:] = rest
                    try:
                        value = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as error:
                        raise CodexAppError("Official Codex app-server returned invalid JSON.", False, "read_failed") from error
                    if isinstance(value, dict) and "id" in value:
                        pending.append(value)

        try:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "tailmsg_local_reader", "title": "Tailmsg local reader", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            }})
            initialized = response(1)
            if "error" in initialized:
                raise CodexAppError("Official Codex app-server initialization failed.", False, "read_failed")
            send({"method": "initialized"})
            send({"id": 2, "method": "thread/read", "params": {"threadId": self.thread_id, "includeTurns": True}})
            read_result = response(2)
            if "error" in read_result:
                raise CodexAppError("Official Codex thread read was rejected.", False, "read_failed")
            thread = read_result.get("result", {}).get("thread")
            if not isinstance(thread, dict) or thread.get("id") != self.thread_id:
                raise CodexAppError("Official Codex returned an unexpected thread.", False, "read_failed")
            return {"ok": True, "threadId": self.thread_id, "hostId": "local", "tailmsgMarkers": _user_markers(thread, turn_limit)}
        except (OSError, ValueError, BrokenPipeError) as error:
            raise CodexAppError("Official Codex read-only app-server failed.", False, "read_failed") from error
        finally:
            try:
                if selector is not None:
                    selector.close()
            finally:
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                finally:
                    for stream in (process.stdin, process.stdout):
                        if stream is not None:
                            stream.close()

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise CodexAppError("Tool arguments must be an object.", False, "invalid_request")
        self._validate(arguments)
        if name == "send_message_to_thread":
            return self._send(arguments)
        if name == "read_thread":
            return self._read(arguments)
        raise CodexAppError("This client only queues notifications and reads dispatcher markers.", False, "unsupported_tool")
