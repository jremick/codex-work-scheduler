"""Bounded, payload-free control of existing local Codex threads."""

import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, TypedDict

from .constants import APP_VERSION
from .errors import SchedulerError


# This allowlist is deliberately local and immutable.  No account, config,
# tool, file, listener, or authentication method is reachable through it.
THREAD_CONTROL_OUTBOUND_ALLOWLIST = frozenset(
    {
        "initialize",
        "initialized",
        "thread/list",
        "thread/read",
        "thread/goal/get",
        "thread/goal/set",
        "thread/resume",
        "turn/interrupt",
        "turn/start",
    }
)

FIXED_CONTINUATION_PROMPT = (
    "Continue the interrupted task from the existing thread context. "
    "Stay within the approved scope and do not expand it."
)

_THREAD_STATUSES = frozenset({"notLoaded", "idle", "systemError", "active"})
_ACTIVE_TURN_STATUSES = frozenset({"inProgress", "in_progress", "active", "running"})
_TURN_STATUSES = frozenset({"completed", "interrupted", "failed", "inProgress"})
_GOAL_STATUSES = frozenset(
    {"active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"}
)
_IGNORED_NOTIFICATIONS = frozenset(
    {"thread/status/changed", "thread/goal/updated", "turn/completed"}
)


class ThreadSummary(TypedDict):
    """Minimal metadata for one active thread."""

    thread_id: str
    status: str
    active_turn_id: Optional[str]


class ThreadTurn(TypedDict):
    """A turn projection without items or content."""

    turn_id: str
    status: str


class ThreadReadResult(TypedDict):
    """Sanitized result from a thread read."""

    thread_id: str
    status: str
    active_turn_id: Optional[str]
    turns: List[ThreadTurn]


class ThreadGoal(TypedDict):
    """Goal status without its objective or usage accounting."""

    thread_id: str
    status: str
    updated_at: int


class ThreadActionResult(TypedDict, total=False):
    """Minimal result of an accepted action."""

    thread_id: str
    turn_id: str
    status: str
    accepted: bool


def _fail(code: str, message: str, *, retryable: bool = False) -> SchedulerError:
    return SchedulerError(code, message, retryable=retryable)


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise _fail("THREAD_CONTROL_INVALID_ARGUMENT", "%s is not a valid identifier" % name)
    if any(ord(character) < 0x20 for character in value):
        raise _fail("THREAD_CONTROL_INVALID_ARGUMENT", "%s is not a valid identifier" % name)
    return value


def _argv(command: Sequence[str]) -> tuple:
    if isinstance(command, (str, bytes)):
        raise _fail("THREAD_CONTROL_COMMAND_INVALID", "The App Server command must be an argv sequence")
    try:
        values = tuple(command)
    except TypeError as exc:
        raise _fail("THREAD_CONTROL_COMMAND_INVALID", "The App Server command must be an argv sequence") from exc
    if not values or any(
        not isinstance(argument, str) or not argument or "\x00" in argument for argument in values
    ):
        raise _fail("THREAD_CONTROL_COMMAND_INVALID", "The App Server command contains an invalid argument")
    return values


def _status(value: Any) -> str:
    if isinstance(value, str):
        result = value
    elif isinstance(value, dict):
        result = value.get("type")
        flags = value.get("activeFlags")
        if flags is not None and (
            not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags)
        ):
            raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The thread status flags are malformed")
    else:
        raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The thread status is malformed")
    if result not in _THREAD_STATUSES:
        raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The thread status is unknown")
    return result


def _turn_status(value: Any) -> str:
    if value not in _TURN_STATUSES:
        raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The turn status is unknown")
    return value


def _turns(value: Any) -> List[ThreadTurn]:
    if not isinstance(value, list):
        raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The thread turns are malformed")
    result: List[ThreadTurn] = []
    seen = set()
    for raw in value:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "A thread turn is malformed")
        turn_id = raw["id"]
        if turn_id in seen:
            raise _fail("THREAD_CONTROL_AMBIGUOUS", "The thread contains duplicate turn identifiers")
        seen.add(turn_id)
        result.append({"turn_id": turn_id, "status": _turn_status(raw.get("status"))})
    if sum(turn["status"] in _ACTIVE_TURN_STATUSES for turn in result) > 1:
        raise _fail("THREAD_CONTROL_AMBIGUOUS", "The thread has multiple active turns")
    return result


def _explicit_active_turn(thread: Dict[str, Any]) -> Optional[str]:
    values: List[str] = []
    for source in (thread, thread.get("status") if isinstance(thread.get("status"), dict) else {}):
        for key in ("activeTurnId", "active_turn_id"):
            value = source.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The active turn identifier is malformed")
            values.append(value)
    if len(set(values)) > 1:
        raise _fail("THREAD_CONTROL_AMBIGUOUS", "The response contains conflicting active turn identifiers")
    return values[0] if values else None


def _active_turn_id(explicit: Optional[str], turns: Optional[List[ThreadTurn]]) -> Optional[str]:
    observed = [turn["turn_id"] for turn in (turns or []) if turn["status"] in _ACTIVE_TURN_STATUSES]
    if len(observed) > 1:
        raise _fail("THREAD_CONTROL_AMBIGUOUS", "The response contains multiple active turns")
    if explicit is not None and observed and explicit != observed[0]:
        raise _fail("THREAD_CONTROL_AMBIGUOUS", "The response contains conflicting active turn state")
    return explicit or (observed[0] if observed else None)


class _Session:
    """One initialized JSONL connection."""

    def __init__(self, process: Any, timeout: float, clock: Callable[[], float]) -> None:
        self.process = process
        self.timeout = timeout
        self.clock = clock
        self.output: "queue.Queue[Any]" = queue.Queue()
        self.closed = False
        if process.stdin is None or process.stdout is None:
            self.close()
            raise _fail("THREAD_CONTROL_UNAVAILABLE", "The App Server stdio transport is unavailable")

        def read_output() -> None:
            try:
                for line in process.stdout:
                    self.output.put(line)
            finally:
                self.output.put(None)

        self.reader_thread = threading.Thread(
            target=read_output,
            name="thread-control-reader",
            daemon=True,
        )
        self.reader_thread.start()

    def _write(self, message: Dict[str, Any]) -> None:
        if message.get("method") not in THREAD_CONTROL_OUTBOUND_ALLOWLIST:
            raise _fail("METHOD_DENIED", "The thread-control method is not allowlisted")
        try:
            self.process.stdin.write(json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise _fail(
                "THREAD_CONTROL_UNAVAILABLE",
                "The App Server stdio transport closed unexpectedly",
            ) from exc

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._write({"method": method, "params": params or {}})

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if method not in THREAD_CONTROL_OUTBOUND_ALLOWLIST:
            raise _fail("METHOD_DENIED", "The thread-control method is not allowlisted")
        request_id = getattr(self, "next_id", 0) + 1
        self.next_id = request_id
        self._write({"id": request_id, "method": method, "params": params or {}})
        deadline = self.clock() + self.timeout
        wall_deadline = time.monotonic() + self.timeout
        while self.clock() < deadline and time.monotonic() < wall_deadline:
            remaining = min(deadline - self.clock(), wall_deadline - time.monotonic())
            try:
                line = self.output.get(timeout=min(0.1, max(0.01, remaining)))
            except queue.Empty:
                continue
            if line is None:
                raise _fail("THREAD_CONTROL_UNAVAILABLE", "The App Server exited before the response")
            try:
                message = json.loads(line)
            except (TypeError, ValueError) as exc:
                raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The App Server returned invalid JSON") from exc
            if not isinstance(message, dict):
                raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The App Server response is malformed")
            if "method" in message:
                if "id" in message:
                    raise _fail(
                        "THREAD_CONTROL_UNEXPECTED_REQUEST",
                        "The App Server requested an unsupported client action",
                    )
                if message.get("method") in _IGNORED_NOTIFICATIONS:
                    continue
                raise _fail(
                    "THREAD_CONTROL_UNEXPECTED_NOTIFICATION",
                    "The App Server sent an unsupported notification",
                )
            if "id" not in message:
                raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The App Server response has no identifier")
            response_id = message.get("id")
            if isinstance(response_id, bool) or response_id != request_id:
                raise _fail("THREAD_CONTROL_RESPONSE_MISMATCH", "The App Server response identifier did not match")
            if message.get("error") is not None:
                raise _fail("THREAD_CONTROL_REJECTED", "The App Server rejected the thread-control method")
            if "result" not in message or not isinstance(message.get("result"), dict):
                raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The App Server result is malformed")
            return message["result"]
        raise _fail("THREAD_CONTROL_TIMEOUT", "The App Server thread-control method timed out")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
        except (AttributeError, OSError, ValueError):
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=1.0)
        except Exception:
            try:
                self.process.kill()
                self.process.wait(timeout=1.0)
            except Exception:
                pass
        try:
            self.process.stdout.close()
        except (AttributeError, OSError, ValueError):
            pass
        self.reader_thread.join(timeout=1.0)


class CodexThreadControl:
    """Expose only bounded controls for existing local App Server threads."""

    COMMAND = ("codex", "app-server", "proxy")

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        command: Optional[Sequence[str]] = None,
        max_pages: int = 100,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise _fail("THREAD_CONTROL_TIMEOUT_CONFIG", "The App Server timeout must be positive")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
            raise _fail("THREAD_CONTROL_PAGINATION_CONFIG", "The pagination bound is invalid")
        self.timeout_seconds = float(timeout_seconds)
        self._popen = popen_factory
        self._clock = clock
        self._command = _argv(command if command is not None else self.COMMAND)
        self.max_pages = max_pages

    @staticmethod
    def allow(method: str) -> str:
        """Validate a method against the fixed outbound allowlist."""

        if method not in THREAD_CONTROL_OUTBOUND_ALLOWLIST:
            raise _fail("METHOD_DENIED", "The thread-control method is not allowlisted")
        return method

    def _open(self) -> _Session:
        try:
            process = self._popen(
                list(self._command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            raise _fail("THREAD_CONTROL_UNAVAILABLE", "The Codex App Server could not be started") from exc
        return _Session(process, self.timeout_seconds, self._clock)

    def _run(self, operation: Callable[[_Session], Any]) -> Any:
        session = self._open()
        try:
            session.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex_work_scheduler_thread_control",
                        "title": "Codex Work Scheduler Thread Control",
                        "version": APP_VERSION,
                    }
                },
            )
            session.notify("initialized")
            return operation(session)
        finally:
            session.close()

    @staticmethod
    def _thread_id(thread: Dict[str, Any]) -> str:
        value = thread.get("id")
        if not isinstance(value, str) or not value:
            raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The thread has no identifier")
        return value

    @classmethod
    def _summary(cls, thread: Dict[str, Any]) -> ThreadSummary:
        thread_id = cls._thread_id(thread)
        inline = _turns(thread["turns"]) if "turns" in thread else None
        return {
            "thread_id": thread_id,
            "status": _status(thread.get("status")),
            "active_turn_id": _active_turn_id(_explicit_active_turn(thread), inline),
        }

    def inventory_active_threads(self) -> List[ThreadSummary]:
        """Return active threads across bounded ``thread/list`` pagination."""

        def operation(session: _Session) -> List[ThreadSummary]:
            cursor: Optional[str] = None
            seen_cursors = set()
            seen_threads = set()
            result: List[ThreadSummary] = []
            for _ in range(self.max_pages):
                params: Dict[str, Any] = {"limit": 100, "archived": False, "useStateDbOnly": True}
                if cursor is not None:
                    params["cursor"] = cursor
                page = session.request("thread/list", params)
                data = page.get("data")
                if not isinstance(data, list):
                    raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The thread list data is malformed")
                for raw_thread in data:
                    if not isinstance(raw_thread, dict):
                        raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "A thread list entry is malformed")
                    summary = self._summary(raw_thread)
                    if summary["thread_id"] in seen_threads:
                        raise _fail("THREAD_CONTROL_AMBIGUOUS", "The thread list contains duplicate identifiers")
                    seen_threads.add(summary["thread_id"])
                    if summary["status"] == "active":
                        result.append(summary)
                next_cursor = page.get("nextCursor")
                if next_cursor is None:
                    return result
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                    raise _fail("THREAD_CONTROL_AMBIGUOUS", "The thread list cursor is malformed or repeated")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            raise _fail("THREAD_CONTROL_TIMEOUT", "The thread list exceeded its pagination bound")

        return self._run(operation)

    inventory = inventory_active_threads
    list_threads = inventory_active_threads

    def read_thread(self, thread_id: str) -> ThreadReadResult:
        """Read one thread, retaining only turn IDs and statuses."""

        expected = _identifier(thread_id, "thread_id")

        def operation(session: _Session) -> ThreadReadResult:
            result = session.request("thread/read", {"threadId": expected, "includeTurns": True})
            thread = result.get("thread")
            if not isinstance(thread, dict) or self._thread_id(thread) != expected:
                raise _fail("THREAD_CONTROL_AMBIGUOUS", "The thread read identity did not match")
            status = _status(thread.get("status"))
            turns = _turns(thread.get("turns"))
            active_turn_id = _active_turn_id(_explicit_active_turn(thread), turns)
            if status != "active" and active_turn_id is not None:
                raise _fail("THREAD_CONTROL_AMBIGUOUS", "The thread status conflicts with its active turn")
            return {
                "thread_id": expected,
                "status": status,
                "active_turn_id": active_turn_id,
                "turns": turns,
            }

        return self._run(operation)

    def get_goal(self, thread_id: str) -> Optional[ThreadGoal]:
        """Read a goal status without returning its objective or usage fields."""

        expected = _identifier(thread_id, "thread_id")

        def operation(session: _Session) -> Optional[ThreadGoal]:
            result = session.request("thread/goal/get", {"threadId": expected})
            if "goal" not in result:
                raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The goal response is malformed")
            goal = result.get("goal")
            if goal is None:
                return None
            if not isinstance(goal, dict) or goal.get("threadId", expected) != expected:
                raise _fail("THREAD_CONTROL_AMBIGUOUS", "The goal identity did not match")
            status = goal.get("status")
            if status not in _GOAL_STATUSES:
                raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The goal status is unknown")
            updated_at = goal.get("updatedAt")
            if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at < 0:
                raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The goal update timestamp is malformed")
            return {"thread_id": expected, "status": status, "updated_at": updated_at}

        return self._run(operation)

    def set_goal_status(self, thread_id: str, status: str) -> ThreadGoal:
        """Set only goal status; never send an objective replacement."""

        expected = _identifier(thread_id, "thread_id")
        if status not in _GOAL_STATUSES:
            raise _fail("THREAD_CONTROL_INVALID_ARGUMENT", "The goal status is unsupported")

        def operation(session: _Session) -> ThreadGoal:
            result = session.request("thread/goal/set", {"threadId": expected, "status": status})
            goal = result.get("goal")
            if not isinstance(goal, dict) or goal.get("threadId", expected) != expected or goal.get("status") != status:
                raise _fail("THREAD_CONTROL_AMBIGUOUS", "The goal update response did not match")
            updated_at = goal.get("updatedAt")
            if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at < 0:
                raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The goal update timestamp is malformed")
            return {"thread_id": expected, "status": status, "updated_at": updated_at}

        return self._run(operation)

    def interrupt_turn(self, thread_id: str, turn_id: str) -> ThreadActionResult:
        """Request interruption for the exact supplied turn ID."""

        expected_thread = _identifier(thread_id, "thread_id")
        expected_turn = _identifier(turn_id, "turn_id")

        def operation(session: _Session) -> ThreadActionResult:
            result = session.request(
                "turn/interrupt",
                {"threadId": expected_thread, "turnId": expected_turn},
            )
            if result:
                raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The interrupt response is not empty")
            return {"thread_id": expected_thread, "turn_id": expected_turn, "accepted": True}

        return self._run(operation)

    def resume_thread(self, thread_id: str) -> ThreadActionResult:
        """Reopen an existing thread without changing its settings."""

        expected = _identifier(thread_id, "thread_id")

        def operation(session: _Session) -> ThreadActionResult:
            result = session.request(
                "thread/resume",
                {
                    "threadId": expected,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                },
            )
            thread = result.get("thread")
            if not isinstance(thread, dict) or self._thread_id(thread) != expected:
                raise _fail("THREAD_CONTROL_AMBIGUOUS", "The resume response identity did not match")
            output: ThreadActionResult = {"thread_id": expected, "accepted": True}
            if "status" in thread:
                output["status"] = _status(thread["status"])
            return output

        return self._run(operation)

    def _start_continuation(self, session: _Session, thread_id: str) -> ThreadActionResult:
        result = session.request(
            "turn/start",
            {
                "threadId": thread_id,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "input": [{"type": "text", "text": FIXED_CONTINUATION_PROMPT}],
            },
        )
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
            raise _fail("THREAD_CONTROL_PROTOCOL_ERROR", "The continuation turn has no identifier")
        if "threadId" in turn and turn.get("threadId") != thread_id:
            raise _fail("THREAD_CONTROL_AMBIGUOUS", "The continuation response identity did not match")
        return {
            "thread_id": thread_id,
            "turn_id": turn["id"],
            "status": _turn_status(turn.get("status")),
            "accepted": True,
        }

    def start_continuation(self, thread_id: str) -> ThreadActionResult:
        """Start one fixed continuation turn on an existing thread."""

        expected = _identifier(thread_id, "thread_id")
        return self._run(lambda session: self._start_continuation(session, expected))

    def resume_and_continue(self, thread_id: str) -> ThreadActionResult:
        """Reopen a thread and start the fixed continuation in one session."""

        expected = _identifier(thread_id, "thread_id")

        def operation(session: _Session) -> ThreadActionResult:
            result = session.request(
                "thread/resume",
                {
                    "threadId": expected,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                },
            )
            thread = result.get("thread")
            if not isinstance(thread, dict) or self._thread_id(thread) != expected:
                raise _fail("THREAD_CONTROL_AMBIGUOUS", "The resume response identity did not match")
            return self._start_continuation(session, expected)

        return self._run(operation)


__all__ = [
    "CodexThreadControl",
    "FIXED_CONTINUATION_PROMPT",
    "THREAD_CONTROL_OUTBOUND_ALLOWLIST",
    "ThreadActionResult",
    "ThreadGoal",
    "ThreadReadResult",
    "ThreadSummary",
    "ThreadTurn",
]
