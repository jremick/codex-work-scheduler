"""Bounded, fixed-prompt live canary adapter for the Codex App Server."""

import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence

from .constants import APP_VERSION, LIVE_TEST_OUTBOUND_ALLOWLIST
from .errors import SchedulerError


CANARY_PROMPT = 'Return exactly the JSON object {"status":"ok"}. Do not call tools.'
CANARY_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status"],
    "properties": {"status": {"const": "ok"}},
}


class CodexLiveTestRunner:
    """Runs one read-only canary. It exposes no arbitrary prompt or command surface."""

    COMMAND = ("codex", "app-server", "--stdio")

    def __init__(
        self,
        *,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.time,
        command: Optional[Sequence[str]] = None,
    ) -> None:
        self._popen_factory = popen_factory
        self._clock = clock
        self._command = tuple(command or self.COMMAND)

    @staticmethod
    def _allow(message: Dict[str, Any]) -> Dict[str, Any]:
        if message["method"] not in LIVE_TEST_OUTBOUND_ALLOWLIST:
            raise SchedulerError("METHOD_DENIED", "The live-test method is not allowlisted")
        return message

    def run(
        self,
        *,
        cwd: str,
        model: str,
        effort: str,
        max_runtime_seconds: int,
        poll_interval_seconds: int,
        safety_check: Callable[[], bool],
        on_started: Callable[[str, str], None],
    ) -> Dict[str, Any]:
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
            raise SchedulerError("LIVE_TEST_UNAVAILABLE", "The Codex App Server could not start", retryable=True) from exc
        if process.stdin is None or process.stdout is None:
            self._terminate(process)
            raise SchedulerError("LIVE_TEST_UNAVAILABLE", "The live-test stdio transport is unavailable")
        output_queue: "queue.Queue[Any]" = queue.Queue()

        def read_output() -> None:
            try:
                for line in process.stdout:
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        threading.Thread(target=read_output, name="live-test-reader", daemon=True).start()

        def send(message: Dict[str, Any]) -> None:
            self._allow(message)
            try:
                process.stdin.write(json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except OSError as exc:
                raise SchedulerError("LIVE_TEST_UNAVAILABLE", "The live-test transport closed", retryable=True) from exc

        def receive_until(response_id: int, deadline: float) -> Dict[str, Any]:
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
                    raise SchedulerError("LIVE_TEST_PROTOCOL_ERROR", "The live-test server returned invalid JSON") from exc
                if message.get("id") == response_id:
                    if message.get("error") is not None:
                        raise SchedulerError("LIVE_TEST_REJECTED", "The App Server rejected a live-test method")
                    return message
                if "id" in message and "method" in message:
                    raise SchedulerError("LIVE_TEST_APPROVAL_REQUESTED", "The canary requested an unsupported approval")
            raise SchedulerError("LIVE_TEST_TIMEOUT", "A live-test method timed out", retryable=True)

        thread_id: Optional[str] = None
        turn_id: Optional[str] = None
        try:
            send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "codex_work_scheduler_live_test",
                            "title": "Codex Work Scheduler Live Test",
                            "version": APP_VERSION,
                        }
                    },
                }
            )
            receive_until(1, self._clock() + min(10, max_runtime_seconds))
            send({"method": "initialized", "params": {}})
            if not safety_check():
                return {"state": "interrupted", "stop_reason": "SAFETY_CHECK_FAILED", "thread_id": None, "turn_id": None}
            send(
                {
                    "id": 2,
                    "method": "thread/start",
                    "params": {
                        "approvalPolicy": "never",
                        "cwd": cwd,
                        "ephemeral": False,
                        "model": model,
                        "sandbox": "read-only",
                    },
                }
            )
            thread_response = receive_until(2, self._clock() + min(10, max_runtime_seconds))
            thread = thread_response.get("result", {}).get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                raise SchedulerError("LIVE_TEST_PROTOCOL_ERROR", "The thread/start response is malformed")
            thread_id = thread["id"]
            if not safety_check():
                return {"state": "interrupted", "stop_reason": "SAFETY_CHECK_FAILED", "thread_id": thread_id, "turn_id": None}
            send(
                {
                    "id": 3,
                    "method": "turn/start",
                    "params": {
                        "approvalPolicy": "never",
                        "effort": effort,
                        "input": [{"type": "text", "text": CANARY_PROMPT}],
                        "model": model,
                        "outputSchema": CANARY_OUTPUT_SCHEMA,
                        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                        "threadId": thread_id,
                    },
                }
            )
            turn_response = receive_until(3, self._clock() + min(10, max_runtime_seconds))
            turn = turn_response.get("result", {}).get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise SchedulerError("LIVE_TEST_PROTOCOL_ERROR", "The turn/start response is malformed")
            turn_id = turn["id"]
            try:
                on_started(thread_id, turn_id)
            except Exception:
                try:
                    send(
                        {
                            "id": 4,
                            "method": "turn/interrupt",
                            "params": {"threadId": thread_id, "turnId": turn_id},
                        }
                    )
                    receive_until(4, self._clock() + 5)
                    stop_reason = "RUN_PERSISTENCE_FAILED"
                except Exception:
                    stop_reason = "RUN_PERSISTENCE_FAILED_INTERRUPT_UNCONFIRMED"
                return {
                    "state": "needs_review",
                    "stop_reason": stop_reason,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                }
            turn_deadline = self._clock() + max_runtime_seconds
            next_safety_check = self._clock() + poll_interval_seconds
            stop_reason: Optional[str] = None
            while self._clock() < turn_deadline:
                now = self._clock()
                if now >= next_safety_check:
                    try:
                        safe = safety_check()
                    except SchedulerError:
                        safe = False
                    if not safe:
                        stop_reason = "SAFETY_CHECK_FAILED"
                        break
                    next_safety_check = now + poll_interval_seconds
                try:
                    line = output_queue.get(timeout=min(0.1, max(0.01, turn_deadline - now)))
                except queue.Empty:
                    continue
                if line is None:
                    raise SchedulerError("LIVE_TEST_UNAVAILABLE", "The App Server exited during the canary", retryable=True)
                try:
                    message = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise SchedulerError("LIVE_TEST_PROTOCOL_ERROR", "The live-test server returned invalid JSON") from exc
                if "id" in message and "method" in message:
                    stop_reason = "APPROVAL_REQUESTED"
                    break
                if message.get("method") == "turn/completed":
                    params = message.get("params")
                    completed_turn = params.get("turn") if isinstance(params, dict) else None
                    if not isinstance(completed_turn, dict) or completed_turn.get("id") != turn_id:
                        continue
                    status = completed_turn.get("status")
                    if status == "completed":
                        return {"state": "succeeded", "stop_reason": None, "thread_id": thread_id, "turn_id": turn_id}
                    if status == "interrupted":
                        return {"state": "interrupted", "stop_reason": "SERVER_INTERRUPTED", "thread_id": thread_id, "turn_id": turn_id}
                    return {"state": "failed", "stop_reason": "TURN_FAILED", "thread_id": thread_id, "turn_id": turn_id}
            if stop_reason is None:
                stop_reason = "RUNTIME_LIMIT"
            send(
                {
                    "id": 4,
                    "method": "turn/interrupt",
                    "params": {"threadId": thread_id, "turnId": turn_id},
                }
            )
            try:
                receive_until(4, self._clock() + 5)
            except SchedulerError:
                stop_reason = "%s_INTERRUPT_UNCONFIRMED" % stop_reason
                return {
                    "state": "needs_review",
                    "stop_reason": stop_reason,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                }
            return {"state": "interrupted", "stop_reason": stop_reason, "thread_id": thread_id, "turn_id": turn_id}
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
