#!/usr/bin/env python3
"""Test-only App Server process with no network or Codex execution path."""

import json
import sys
import time


MODE = sys.argv[1] if len(sys.argv) > 1 else "complete"
THREAD_ID = "thread-fixture"
TURN_ID = "turn-fixture"
TURN_STATUS = "completed" if MODE == "complete" else "inProgress"


def send(value):
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "fake-app-server"}})
    elif method == "initialized":
        continue
    elif method == "account/read":
        send(
            {
                "id": message["id"],
                "result": {
                    "account": {
                        "type": "chatgpt",
                        "email": "fixture-account@example.invalid",
                        "planType": "pro",
                    },
                    "requiresOpenaiAuth": True,
                },
            }
        )
    elif method == "account/rateLimits/read":
        now = time.time()
        send(
            {
                "id": message["id"],
                "result": {
                    "rateLimits": {
                        "limitId": "codex",
                        "planType": "pro",
                        "credits": {
                            "hasCredits": False,
                            "unlimited": False,
                            "balance": "0",
                        },
                        "spendControlReached": False,
                        "primary": {
                            "usedPercent": 20,
                            "windowDurationMins": 300,
                            "resetsAt": now + 3600,
                        },
                        "secondary": {
                            "usedPercent": 30,
                            "windowDurationMins": 10080,
                            "resetsAt": now + 604800,
                        },
                        "rateLimitReachedType": None,
                    }
                },
            }
        )
    elif method == "hooks/list":
        send(
            {
                "id": message["id"],
                "result": {
                    "data": [
                        {
                            "cwd": message["params"]["cwds"][0],
                            "hooks": [
                                {
                                    "enabled": True,
                                    "isManaged": MODE == "hook",
                                    "key": "fixture-hook",
                                    "name": "fixture-hook",
                                }
                            ]
                            if MODE in {"hook", "overrides"}
                            else [],
                            "errors": [],
                            "warnings": [],
                        }
                    ]
                },
            }
        )
    elif method == "app/installed":
        send(
            {
                "id": message["id"],
                "result": {
                    "apps": (
                        [{"id": "fixture-app", "enabled": True, "callable": True}]
                        if MODE == "app"
                        or (MODE == "overrides" and "threadId" not in message["params"])
                        else []
                    )
                },
            }
        )
    elif method == "mcpServerStatus/list":
        send(
            {
                "id": message["id"],
                "result": {
                    "data": (
                        [{"name": "fixture-mcp", "runtimeStatus": "connected"}]
                        if MODE == "mcp"
                        else [
                            {
                                "name": "fixture-mcp",
                                "runtimeStatus": "disabled"
                                if "threadId" in message["params"]
                                else None,
                            },
                            {
                                "name": "codex_apps",
                                "runtimeStatus": "disabled"
                                if "threadId" in message["params"]
                                else None,
                            },
                        ]
                        if MODE == "overrides"
                        else []
                    ),
                    "nextCursor": None,
                },
            }
        )
    elif method == "experimentalFeature/list":
        disabled = [
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
        ]
        send(
            {
                "id": message["id"],
                "result": {
                    "data": [
                        {
                            "name": name,
                            "enabled": False,
                            "defaultEnabled": False,
                            "stage": "stable",
                        }
                        for name in disabled
                    ],
                    "nextCursor": None,
                },
            }
        )
    elif method == "thread/start":
        if MODE == "reject-thread-start":
            send(
                {
                    "id": message["id"],
                    "error": {"code": -32600, "message": "fixture rejection"},
                }
            )
            continue
        if MODE == "overrides":
            config = message["params"].get("config", {})
            feature_values = config.get("features", {})
            valid = (
                len(feature_values) == 19
                and all(value is False for value in feature_values.values())
                and config.get("hooks", {}).get("state", {}).get("fixture-hook", {}).get("enabled") is False
                and config.get("mcp_servers", {}).get("fixture-mcp", {}).get("enabled") is False
                and "codex_apps" not in config.get("mcp_servers", {})
            )
            if not valid:
                send({"id": message["id"], "error": {"code": -32602, "message": "unsafe config"}})
                continue
        send(
            {
                "id": message["id"],
                "result": {
                    "thread": {"id": THREAD_ID, "status": {"type": "idle"}, "turns": []}
                },
            }
        )
    elif method == "turn/start":
        TURN_STATUS = "inProgress"
        send(
            {
                "id": message["id"],
                "result": {"turn": {"id": TURN_ID, "status": "inProgress", "items": []}},
            }
        )
        if MODE in {"complete", "overrides"}:
            TURN_STATUS = "completed"
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": THREAD_ID,
                        "turn": {"id": TURN_ID, "status": "completed", "items": []},
                    },
                }
            )
    elif method == "turn/interrupt":
        TURN_STATUS = "interrupted"
        send({"id": message["id"], "result": {}})
    elif method == "thread/read":
        send(
            {
                "id": message["id"],
                "result": {
                    "thread": {
                        "id": message["params"]["threadId"],
                        "status": {"type": "active" if TURN_STATUS == "inProgress" else "idle"},
                        "turns": [{"id": TURN_ID, "status": TURN_STATUS, "items": []}],
                    }
                },
            }
        )
    else:
        send({"id": message.get("id"), "error": {"code": -32601, "message": "denied"}})
