"""Guarded Codex App Server runner for one immutable approved work package."""

import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from .constants import APP_VERSION, WORK_OUTBOUND_ALLOWLIST
from .errors import SchedulerError


_OPERATING_RULES = (
    "Execute only the approved objective below. Work only in the configured cwd. "
    "Do not use network access, MCP servers, apps, hooks, credentials, external "
    "notifications, authentication changes, or approval escalation. Stop if the "
    "objective cannot be completed inside these limits.\n\nApproved objective:\n"
)

_DISABLED_FEATURES = frozenset(
    {
        "apps",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "computer_use",
        "enable_mcp_apps",
        "hooks",
        "in_app_browser",
        "multi_agent",
        "plugin_hooks",
        "plugin_sharing",
        "plugins",
        "remote_control",
        "remote_plugin",
        "search_tool",
        "standalone_web_search",
        "tool_search",
        "web_search_cached",
        "web_search_request",
    }
)

# App Server exposes its built-in connector runtime in MCP inventory as
# `codex_apps`, but it has no user-configurable transport.  Adding a partial
# `mcp_servers.codex_apps` override is rejected as an invalid transport.  The
# apps feature gate disables it, and the post-thread inventory verifies that it
# is not callable or connected.
_FEATURE_GATED_MCP_SERVERS = frozenset({"codex_apps"})


class CodexWorkRunner:
    """Runs one approved objective through a fixed, deny-by-default RPC surface."""

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
        if message["method"] not in WORK_OUTBOUND_ALLOWLIST:
            raise SchedulerError("METHOD_DENIED", "The work dispatch method is not allowlisted")
        return message

    def run(
        self,
        *,
        objective: str,
        cwd: str,
        model: str,
        effort: str,
        sandbox: str,
        max_runtime_seconds: int,
        poll_interval_seconds: int,
        safety_check: Callable[[], bool],
        on_thread_started: Callable[[str], None],
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
            raise SchedulerError(
                "WORK_RUNNER_UNAVAILABLE", "The Codex App Server could not start", retryable=True
            ) from exc
        if process.stdin is None or process.stdout is None:
            self._terminate(process)
            raise SchedulerError("WORK_RUNNER_UNAVAILABLE", "The App Server stdio transport is unavailable")

        output_queue: "queue.Queue[Any]" = queue.Queue()

        def read_output() -> None:
            try:
                for line in process.stdout:
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        threading.Thread(target=read_output, name="work-runner-reader", daemon=True).start()
        next_id = {"value": 0}

        def send(message: Dict[str, Any]) -> None:
            self._allow(message)
            try:
                process.stdin.write(json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except OSError as exc:
                raise SchedulerError(
                    "WORK_RUNNER_UNAVAILABLE", "The App Server transport closed", retryable=True
                ) from exc

        def receive_until(
            response_id: int,
            deadline: float,
            method: Optional[str] = None,
        ) -> Dict[str, Any]:
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
                    raise SchedulerError("WORK_PROTOCOL_ERROR", "The App Server returned invalid JSON") from exc
                if message.get("id") == response_id and "method" not in message:
                    if message.get("error") is not None:
                        suffix = ""
                        if method is not None:
                            suffix = "_" + method.upper().replace("/", "_").replace("-", "_")
                        raise SchedulerError(
                            "WORK_METHOD_REJECTED%s" % suffix,
                            "The App Server rejected a work method",
                        )
                    return message
                if "id" in message and "method" in message:
                    raise SchedulerError(
                        "WORK_APPROVAL_REQUESTED", "The App Server requested an unsupported action"
                    )
            raise SchedulerError("WORK_METHOD_TIMEOUT", "An App Server method timed out", retryable=True)

        def rpc(method: str, params: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
            next_id["value"] += 1
            response_id = next_id["value"]
            send({"id": response_id, "method": method, "params": params})
            return receive_until(
                response_id,
                self._clock() + timeout,
                method,
            ).get("result", {})

        def blocked(reason: str, thread_id: Optional[str] = None) -> Dict[str, Any]:
            return {
                "state": "blocked",
                "stop_reason": reason,
                "thread_id": thread_id,
                "turn_id": None,
            }

        hook_state: Dict[str, Dict[str, bool]] = {}
        mcp_state: Dict[str, Dict[str, bool]] = {}

        def inspect_hooks() -> Optional[str]:
            result = rpc("hooks/list", {"cwds": [cwd]})
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                return "HOOK_INVENTORY_INVALID"
            for entry in data:
                if not isinstance(entry, dict):
                    return "HOOK_INVENTORY_INVALID"
                errors = entry.get("errors", [])
                warnings = entry.get("warnings", [])
                hooks = entry.get("hooks", [])
                if not isinstance(errors, list) or not isinstance(warnings, list) or not isinstance(hooks, list):
                    return "HOOK_INVENTORY_INVALID"
                if errors or warnings:
                    return "HOOK_INVENTORY_UNCERTAIN"
                for hook in hooks:
                    if (
                        not isinstance(hook, dict)
                        or not isinstance(hook.get("enabled"), bool)
                        or not isinstance(hook.get("isManaged"), bool)
                        or not isinstance(hook.get("key"), str)
                        or not hook.get("key")
                    ):
                        return "HOOK_INVENTORY_INVALID"
                    if hook["enabled"]:
                        if hook["isManaged"]:
                            return "MANAGED_HOOKS_ENABLED"
                        hook_state[hook["key"]] = {"enabled": False}
            return None

        def inspect_apps(thread_id: Optional[str], *, require_disabled: bool) -> Optional[str]:
            params: Dict[str, Any] = {"forceRefresh": False}
            if thread_id is not None:
                params["threadId"] = thread_id
            result = rpc("app/installed", params)
            apps = result.get("apps") if isinstance(result, dict) else None
            if not isinstance(apps, list):
                return "APP_INVENTORY_INVALID"
            for app in apps:
                if not isinstance(app, dict):
                    return "APP_INVENTORY_INVALID"
                enabled = app.get("enabled")
                callable_value = app.get("callable")
                if not isinstance(enabled, bool) or not isinstance(callable_value, bool):
                    return "APP_INVENTORY_INVALID"
                if require_disabled and (enabled or callable_value):
                    return "APPS_ENABLED"
            return None

        def inspect_mcp(thread_id: Optional[str], *, require_disabled: bool) -> Optional[str]:
            cursor: Optional[str] = None
            seen: List[str] = []
            for _page in range(10):
                params: Dict[str, Any] = {"limit": 100}
                if thread_id is not None:
                    params["threadId"] = thread_id
                if cursor is not None:
                    params["cursor"] = cursor
                result = rpc("mcpServerStatus/list", params)
                data = result.get("data") if isinstance(result, dict) else None
                next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
                if not isinstance(data, list) or (next_cursor is not None and not isinstance(next_cursor, str)):
                    return "MCP_INVENTORY_INVALID"
                for server in data:
                    if not isinstance(server, dict) or not isinstance(server.get("name"), str):
                        return "MCP_INVENTORY_INVALID"
                    if require_disabled:
                        if server.get("runtimeStatus") != "disabled":
                            return "MCP_SERVERS_ENABLED"
                    elif server["name"] not in _FEATURE_GATED_MCP_SERVERS:
                        mcp_state[server["name"]] = {"enabled": False}
                if next_cursor is None:
                    return None
                if not next_cursor or next_cursor in seen:
                    return "MCP_INVENTORY_INVALID"
                seen.append(next_cursor)
                cursor = next_cursor
            return "MCP_INVENTORY_INVALID"

        def inspect_features(thread_id: str) -> Optional[str]:
            cursor: Optional[str] = None
            seen_cursors: List[str] = []
            observed: Dict[str, bool] = {}
            for _page in range(10):
                params: Dict[str, Any] = {"limit": 100, "threadId": thread_id}
                if cursor is not None:
                    params["cursor"] = cursor
                result = rpc("experimentalFeature/list", params)
                data = result.get("data") if isinstance(result, dict) else None
                next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
                if not isinstance(data, list) or (next_cursor is not None and not isinstance(next_cursor, str)):
                    return "FEATURE_INVENTORY_INVALID"
                for feature in data:
                    if (
                        not isinstance(feature, dict)
                        or not isinstance(feature.get("name"), str)
                        or not isinstance(feature.get("enabled"), bool)
                    ):
                        return "FEATURE_INVENTORY_INVALID"
                    observed[feature["name"]] = feature["enabled"]
                if next_cursor is None:
                    break
                if not next_cursor or next_cursor in seen_cursors:
                    return "FEATURE_INVENTORY_INVALID"
                seen_cursors.append(next_cursor)
                cursor = next_cursor
            else:
                return "FEATURE_INVENTORY_INVALID"
            missing = _DISABLED_FEATURES - set(observed)
            if missing or any(observed[name] for name in _DISABLED_FEATURES):
                return "EXTERNAL_FEATURES_ENABLED"
            return None

        thread_id: Optional[str] = None
        turn_id: Optional[str] = None
        try:
            rpc(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex_work_scheduler_dispatch",
                        "title": "Codex Work Scheduler Dispatch",
                        "version": APP_VERSION,
                    }
                },
            )
            send({"method": "initialized", "params": {}})
            if not safety_check():
                return blocked("SAFETY_CHECK_FAILED")
            for inspection in (
                inspect_hooks,
                lambda: inspect_apps(None, require_disabled=False),
                lambda: inspect_mcp(None, require_disabled=False),
            ):
                reason = inspection()
                if reason is not None:
                    return blocked(reason)
            if not safety_check():
                return blocked("SAFETY_CHECK_FAILED")

            thread_result = rpc(
                "thread/start",
                {
                    "approvalPolicy": "never",
                    "cwd": cwd,
                    "ephemeral": False,
                    "model": model,
                    "config": {
                        "features": {name: False for name in sorted(_DISABLED_FEATURES)},
                        "hooks": {"state": hook_state},
                        "include_apps_instructions": False,
                        "mcp_servers": mcp_state,
                    },
                    # Starting read-only avoids the documented workspace trust mutation.
                    "sandbox": "read-only",
                },
            )
            thread = thread_result.get("thread") if isinstance(thread_result, dict) else None
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                raise SchedulerError("WORK_PROTOCOL_ERROR", "The thread/start response is malformed")
            thread_id = thread["id"]
            try:
                on_thread_started(thread_id)
            except Exception:
                return {
                    "state": "needs_review",
                    "stop_reason": "THREAD_PERSISTENCE_FAILED",
                    "thread_id": thread_id,
                    "turn_id": None,
                }
            for inspection in (
                lambda: inspect_features(thread_id),
                lambda: inspect_apps(thread_id, require_disabled=True),
                lambda: inspect_mcp(thread_id, require_disabled=True),
            ):
                reason = inspection()
                if reason is not None:
                    return blocked(reason, thread_id)
            if not safety_check():
                return blocked("SAFETY_CHECK_FAILED", thread_id)

            if sandbox == "read_only":
                sandbox_policy = {"type": "readOnly", "networkAccess": False}
            elif sandbox == "workspace_write":
                sandbox_policy = {
                    "type": "workspaceWrite",
                    "networkAccess": False,
                    "writableRoots": [cwd],
                }
            else:
                raise SchedulerError("WORK_PACKAGE_INVALID", "The approved sandbox is unsupported")
            turn_result = rpc(
                "turn/start",
                {
                    "approvalPolicy": "never",
                    "effort": effort,
                    "input": [{"type": "text", "text": _OPERATING_RULES + objective}],
                    "model": model,
                    "sandboxPolicy": sandbox_policy,
                    "threadId": thread_id,
                },
            )
            turn = turn_result.get("turn") if isinstance(turn_result, dict) else None
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise SchedulerError("WORK_PROTOCOL_ERROR", "The turn/start response is malformed")
            turn_id = turn["id"]
            try:
                on_started(thread_id, turn_id)
            except Exception:
                return self._interrupt(
                    send, receive_until, thread_id, turn_id, "RUN_PERSISTENCE_FAILED"
                )

            turn_deadline = self._clock() + max_runtime_seconds
            next_safety_check = self._clock() + poll_interval_seconds
            stop_reason: Optional[str] = None
            while self._clock() < turn_deadline:
                now = self._clock()
                if now >= next_safety_check:
                    try:
                        safe = safety_check()
                    except Exception:
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
                    raise SchedulerError(
                        "WORK_RUNNER_UNAVAILABLE", "The App Server exited during the turn", retryable=True
                    )
                try:
                    message = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise SchedulerError("WORK_PROTOCOL_ERROR", "The App Server returned invalid JSON") from exc
                if "id" in message and "method" in message:
                    stop_reason = "APPROVAL_OR_TOOL_REQUESTED"
                    break
                if message.get("method") != "turn/completed":
                    continue
                params = message.get("params")
                completed_turn = params.get("turn") if isinstance(params, dict) else None
                if not isinstance(completed_turn, dict) or completed_turn.get("id") != turn_id:
                    continue
                status = completed_turn.get("status")
                if status == "completed":
                    return {
                        "state": "succeeded",
                        "stop_reason": None,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                    }
                if status == "interrupted":
                    return {
                        "state": "interrupted",
                        "stop_reason": "SERVER_INTERRUPTED",
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                    }
                return {
                    "state": "failed",
                    "stop_reason": "TURN_FAILED",
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                }
            return self._interrupt(
                send,
                receive_until,
                thread_id,
                turn_id,
                stop_reason or "RUNTIME_LIMIT",
            )
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

    def _interrupt(
        self,
        send: Callable[[Dict[str, Any]], None],
        receive_until: Callable[[int, float], Dict[str, Any]],
        thread_id: str,
        turn_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        interrupt_id = 10_000
        try:
            send(
                {
                    "id": interrupt_id,
                    "method": "turn/interrupt",
                    "params": {"threadId": thread_id, "turnId": turn_id},
                }
            )
            receive_until(interrupt_id, self._clock() + 5)
        except Exception:
            return {
                "state": "needs_review",
                "stop_reason": "%s_INTERRUPT_UNCONFIRMED" % reason,
                "thread_id": thread_id,
                "turn_id": turn_id,
            }
        return {
            "state": "interrupted",
            "stop_reason": reason,
            "thread_id": thread_id,
            "turn_id": turn_id,
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
