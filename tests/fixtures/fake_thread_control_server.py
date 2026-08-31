#!/usr/bin/env python3
"""Synthetic JSONL App Server used only by ``test_thread_control``.

The fixture has no network, authentication, file, or Codex execution path.  A
scenario selects deterministic response data so tests can exercise the
adapter's protocol and fail-closed handling.
"""

import json
import sys


SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "happy"
THREAD_ID = "thread-control-fixture"
ACTIVE_TURN_ID = "turn-control-active"
CONTINUATION_TURN_ID = "turn-control-continuation"


def send(message):
    sys.stdout.write(json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def thread(value=THREAD_ID, status="idle", turns=None):
    result = {"id": value, "status": {"type": status}}
    if turns is not None:
        result["turns"] = turns
    return result


def turns_for_read():
    if SCENARIO == "ambiguous":
        return [
            {"id": "turn-one", "status": "inProgress", "items": []},
            {"id": "turn-two", "status": "inProgress", "items": []},
        ]
    return [
        {
            "id": "turn-finished",
            "status": "completed",
            "items": [{"text": "must-not-leak-output"}],
        },
        {"id": ACTIVE_TURN_ID, "status": "inProgress", "items": []},
    ]


for line in sys.stdin:
    try:
        message = json.loads(line)
    except (TypeError, ValueError):
        continue
    if not isinstance(message, dict):
        continue
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialized":
        continue
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fake-thread-control"}})
    elif method == "thread/list":
        if SCENARIO == "timeout":
            continue
        if SCENARIO == "malformed":
            send({"id": request_id, "result": {"data": {"not": "a-list"}}})
            continue
        cursor = (message.get("params") or {}).get("cursor")
        if SCENARIO == "pagination" and cursor is None:
            send(
                {
                    "id": request_id,
                    "result": {
                        "data": [thread("thread-idle", "idle")],
                        "nextCursor": "page-two",
                    },
                }
            )
        elif SCENARIO == "pagination" and cursor == "page-two":
            send(
                {
                    "id": request_id,
                    "result": {
                        "data": [
                            thread(
                                THREAD_ID,
                                "active",
                                [{"id": ACTIVE_TURN_ID, "status": "inProgress", "items": []}],
                            )
                        ],
                        "nextCursor": None,
                    },
                }
            )
        elif SCENARIO == "repeated-cursor":
            send(
                {
                    "id": request_id,
                    "result": {
                        "data": [thread(THREAD_ID, "active")],
                        "nextCursor": "same-page",
                    },
                }
            )
        else:
            send(
                {
                    "id": request_id,
                    "result": {
                        "data": [thread(THREAD_ID, "active")],
                        "nextCursor": None,
                    },
                }
            )
    elif method == "thread/read":
        if SCENARIO == "timeout":
            continue
        if SCENARIO == "malformed-read":
            send({"id": request_id, "result": {"thread": {"id": THREAD_ID}}})
            continue
        send(
            {
                "id": request_id,
                "result": {
                    "thread": thread(
                        THREAD_ID,
                        "active" if SCENARIO in {"happy", "ambiguous"} else "idle",
                        turns_for_read(),
                    )
                },
            }
        )
    elif method == "thread/goal/get":
        send(
            {
                "id": request_id,
                "result": {
                    "goal": {
                        "threadId": THREAD_ID,
                        "objective": "must-not-leak-objective",
                        "status": "active",
                        "tokenBudget": 500,
                        "tokensUsed": 4,
                        "timeUsedSeconds": 2,
                        "updatedAt": 10,
                    }
                },
            }
        )
    elif method == "thread/goal/set":
        params = message.get("params") or {}
        if "objective" in params:
            send({"id": request_id, "error": {"code": -32602, "message": "objective is forbidden"}})
        else:
            send(
                {
                    "id": request_id,
                    "result": {
                        "goal": {
                            "threadId": THREAD_ID,
                            "objective": "must-not-leak-objective",
                            "status": params.get("status"),
                            "updatedAt": 11,
                        }
                    },
                }
            )
    elif method == "turn/interrupt":
        params = message.get("params") or {}
        if SCENARIO == "reject":
            send({"id": request_id, "error": {"code": -32000, "message": "fixture rejection"}})
        elif params.get("turnId") != ACTIVE_TURN_ID:
            send({"id": request_id, "error": {"code": -32602, "message": "wrong turn"}})
        else:
            send({"id": request_id, "result": {}})
    elif method == "thread/resume":
        params = message.get("params") or {}
        if SCENARIO == "reject":
            send({"id": request_id, "error": {"code": -32000, "message": "fixture rejection"}})
        elif params.get("approvalPolicy") != "never" or params.get("sandbox") != "read-only":
            send({"id": request_id, "error": {"code": -32602, "message": "unsafe resume"}})
        else:
            send({"id": request_id, "result": {"thread": thread(THREAD_ID, "idle")}})
    elif method == "turn/start":
        params = message.get("params") or {}
        input_items = params.get("input")
        if SCENARIO == "reject":
            send({"id": request_id, "error": {"code": -32000, "message": "fixture rejection"}})
        elif (
            params.get("threadId") != THREAD_ID
            or params.get("approvalPolicy") != "never"
            or params.get("sandboxPolicy") != {"type": "readOnly", "networkAccess": False}
            or not isinstance(input_items, list)
            or len(input_items) != 1
            or input_items[0].get("type") != "text"
            or input_items[0].get("text") != "Continue the interrupted task from the existing thread context. Stay within the approved scope and do not expand it."
        ):
            send({"id": request_id, "error": {"code": -32602, "message": "unsafe continuation"}})
        else:
            send(
                {
                    "id": request_id,
                    "result": {
                        "turn": {
                            "id": CONTINUATION_TURN_ID,
                            "threadId": THREAD_ID,
                            "status": "inProgress",
                            "items": [],
                        }
                    },
                }
            )
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": THREAD_ID,
                        "turn": {"id": CONTINUATION_TURN_ID, "status": "completed", "items": []},
                    },
                }
            )
    else:
        send({"id": request_id, "error": {"code": -32601, "message": "denied"}})
