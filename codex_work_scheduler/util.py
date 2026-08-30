"""Small deterministic helpers with no external dependencies."""

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict

from .errors import SchedulerError

MAX_JSON_INPUT_BYTES = 1_048_576


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def keyed_fingerprint(value: Any, key_hex: str) -> str:
    """Return a local-only opaque fingerprint for privacy-sensitive identity data."""
    try:
        key = bytes.fromhex(key_hex)
    except ValueError as exc:
        raise SchedulerError("STATE_INVALID", "The account fingerprint key is invalid") from exc
    if len(key) != 32:
        raise SchedulerError("STATE_INVALID", "The account fingerprint key is invalid")
    return hashlib.blake2b(
        canonical_json(value).encode("utf-8"),
        key=key,
        digest_size=32,
    ).hexdigest()


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def load_json_file(path: str) -> Dict[str, Any]:
    try:
        input_path = Path(path)
        if input_path.stat().st_size > MAX_JSON_INPUT_BYTES:
            raise SchedulerError(
                "INPUT_TOO_LARGE",
                "The JSON input exceeds the scheduler size limit",
                details={"maximum_bytes": MAX_JSON_INPUT_BYTES},
            )
        with input_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except SchedulerError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise SchedulerError(
            "INVALID_INPUT",
            "The JSON input could not be read",
            details={"input_type": "json_file"},
        ) from exc
    if not isinstance(value, dict):
        raise SchedulerError("INVALID_INPUT", "The JSON input must be an object")
    return value


def make_envelope(
    command: str,
    request_id: str,
    *,
    result: Any = None,
    error: Any = None,
) -> Dict[str, Any]:
    return {
        "command": command,
        "error": error,
        "ok": error is None,
        "request_id": request_id,
        "result": result if error is None else None,
        "schema_version": "1",
    }


Clock = Callable[[], float]
