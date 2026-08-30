"""Read-only Codex task monitor with a fixed App Server method allowlist."""

import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence

from .constants import APP_VERSION, MONITOR_OUTBOUND_ALLOWLIST
from .errors import SchedulerError


class CodexThreadMonitor:
    COMMAND = ("codex", "app-server", "--stdio")

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.time,
        command: Optional[Sequence[str]] = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._popen_factory = popen_factory
        self._clock = clock
        self._command = tuple(command or self.COMMAND)

    @staticmethod
    def _method(message: Dict[str, Any]) -> Dict[str, Any]:
        if message["method"] not in MONITOR_OUTBOUND_ALLOWLIST:
            raise SchedulerError("METHOD_DENIED", "The monitor method is not allowlisted")
        return message

    def read(self, thread_id: str) -> Dict[str, Any]:
        messages = [
            self._method(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "codex_work_scheduler_monitor",
                            "title": "Codex Work Scheduler Monitor",
                            "version": APP_VERSION,
                        }
                    },
                }
            ),
            self._method({"method": "initialized", "params": {}}),
            self._method(
                {
                    "id": 2,
                    "method": "thread/read",
                    "params": {"threadId": thread_id, "includeTurns": True},
                }
            ),
        ]
        try:
            process = self._popen_factory(
                list(self._command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise SchedulerError("MONITOR_UNAVAILABLE", "The Codex monitor could not start", retryable=True) from exc
        if process.stdin is None or process.stdout is None:
            self._terminate(process)
            raise SchedulerError("MONITOR_UNAVAILABLE", "The monitor stdio transport is unavailable")
        output_queue: "queue.Queue[Any]" = queue.Queue()

        def read_output() -> None:
            try:
                for line in process.stdout:
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        threading.Thread(target=read_output, name="task-monitor-reader", daemon=True).start()
        try:
            for message in messages:
                process.stdin.write(json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n")
                process.stdin.flush()
            deadline = self._clock() + self.timeout_seconds
            response: Optional[Dict[str, Any]] = None
            while self._clock() < deadline:
                try:
                    line = output_queue.get(timeout=min(0.1, max(0.01, deadline - self._clock())))
                except queue.Empty:
                    continue
                if line is None:
                    break
                try:
                    message = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise SchedulerError("MONITOR_PROTOCOL_ERROR", "The monitor returned invalid JSON") from exc
                if message.get("id") == 2:
                    response = message
                    break
            if response is None:
                raise SchedulerError("MONITOR_TIMEOUT", "The task read did not complete in time", retryable=True)
            if response.get("error") is not None:
                raise SchedulerError("MONITOR_REJECTED", "The App Server rejected the task read")
            return self._normalize(response.get("result"), expected_thread_id=thread_id)
        finally:
            try:
                process.stdin.close()
            except (OSError, AttributeError):
                pass
            self._terminate(process)
            try:
                process.stdout.close()
            except (OSError, AttributeError):
                pass

    def _normalize(self, value: Any, *, expected_thread_id: str) -> Dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("thread"), dict):
            raise SchedulerError("MONITOR_PROTOCOL_ERROR", "The task response is malformed")
        thread = value["thread"]
        if thread.get("id") != expected_thread_id:
            raise SchedulerError("MONITOR_PROTOCOL_ERROR", "The task response identity does not match")
        status = thread.get("status")
        if isinstance(status, dict):
            status = status.get("type")
        if status not in {"notLoaded", "idle", "systemError", "active"}:
            raise SchedulerError("MONITOR_PROTOCOL_ERROR", "The task status is unknown")
        turns = thread.get("turns", [])
        if not isinstance(turns, list):
            raise SchedulerError("MONITOR_PROTOCOL_ERROR", "The task turns are malformed")
        latest_turn_id = None
        latest_turn_status = None
        if turns:
            latest = turns[-1]
            if not isinstance(latest, dict):
                raise SchedulerError("MONITOR_PROTOCOL_ERROR", "The latest turn is malformed")
            latest_turn_id = latest.get("id")
            latest_turn_status = latest.get("status")
            if not isinstance(latest_turn_id, str) or latest_turn_status not in {
                "completed",
                "interrupted",
                "failed",
                "inProgress",
            }:
                raise SchedulerError("MONITOR_PROTOCOL_ERROR", "The latest turn state is unknown")
        return {
            "latest_turn_id": latest_turn_id,
            "latest_turn_status": latest_turn_status,
            "observed_at": self._clock(),
            "outbound_methods": ["initialize", "initialized", "thread/read"],
            "thread_id": expected_thread_id,
            "thread_status": status,
        }

    @staticmethod
    def _terminate(process: Any) -> None:
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
