"""Safe launchd plist rendering and validation without activation commands."""

import os
import plistlib
import re
from pathlib import Path
from typing import Any, Dict
from xml.sax.saxutils import escape

from .errors import SchedulerError


DEFAULT_LABEL = "io.github.jremick.codex-work-scheduler"
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{2,127}$")
_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "launchd"
    / "io.github.jremick.codex-work-scheduler.plist.template"
)
_DENIED_INSTALL_ROOTS = (
    Path.home() / "Library" / "LaunchAgents",
    Path("/Library/LaunchAgents"),
    Path("/Library/LaunchDaemons"),
    Path("/System/Library/LaunchAgents"),
    Path("/System/Library/LaunchDaemons"),
)


def _absolute_existing(path: str, name: str, *, directory: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise SchedulerError("LAUNCHD_PATH_INVALID", "%s must be an existing absolute path" % name)
    value = candidate.resolve()
    if not value.exists():
        raise SchedulerError("LAUNCHD_PATH_INVALID", "%s must be an existing absolute path" % name)
    if directory and not value.is_dir():
        raise SchedulerError("LAUNCHD_PATH_INVALID", "%s must be a directory" % name)
    if not directory and not value.is_file():
        raise SchedulerError("LAUNCHD_PATH_INVALID", "%s must be a file" % name)
    return value


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def render_plist(
    *,
    python_path: str,
    codex_path: str,
    repository_path: str,
    config_path: str,
    stdout_path: str,
    stderr_path: str,
    label: str = DEFAULT_LABEL,
) -> bytes:
    if not _LABEL.fullmatch(label):
        raise SchedulerError("LAUNCHD_LABEL_INVALID", "The launchd label is invalid")
    python = _absolute_existing(python_path, "python_path")
    codex = _absolute_existing(codex_path, "codex_path")
    repository = _absolute_existing(repository_path, "repository_path", directory=True)
    config = _absolute_existing(config_path, "config_path")
    stdout = Path(stdout_path).resolve()
    stderr = Path(stderr_path).resolve()
    values = {
        "__CODEX_BIN_DIR__": str(codex.parent),
        "__CONFIG_PATH__": str(config),
        "__LABEL__": label,
        "__PYTHON_PATH__": str(python),
        "__REPOSITORY_PATH__": str(repository),
        "__STDERR_PATH__": str(stderr),
        "__STDOUT_PATH__": str(stdout),
    }
    rendered = _TEMPLATE.read_text(encoding="utf-8")
    for marker, value in values.items():
        rendered = rendered.replace(marker, escape(value))
    if "__" in rendered:
        raise SchedulerError("LAUNCHD_TEMPLATE_INVALID", "The launchd template is incomplete")
    encoded = rendered.encode("utf-8")
    check_plist_bytes(encoded)
    return encoded


def write_plist(output_path: str, content: bytes) -> Dict[str, Any]:
    output = Path(output_path).resolve()
    if any(_is_within(output, root.resolve()) for root in _DENIED_INSTALL_ROOTS):
        raise SchedulerError(
            "LAUNCHD_INSTALL_DENIED",
            "Render to a staging path; this command never installs a LaunchAgent",
        )
    parent = output.parent
    parent_existed = parent.exists()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not parent_existed:
        parent.chmod(0o700)
    descriptor = os.open(str(output), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        payload = content
        while payload:
            payload = payload[os.write(descriptor, payload) :]
    finally:
        os.close(descriptor)
    output.chmod(0o600)
    checked = check_plist_bytes(content)
    return {"output_path": str(output), "plist": checked, "written": True}


def check_plist(path: str) -> Dict[str, Any]:
    plist_path = _absolute_existing(path, "plist_path")
    return check_plist_bytes(plist_path.read_bytes())


def check_plist_bytes(content: bytes) -> Dict[str, Any]:
    try:
        value = plistlib.loads(content)
    except Exception as exc:
        raise SchedulerError("LAUNCHD_PLIST_INVALID", "The launchd plist is malformed") from exc
    if not isinstance(value, dict):
        raise SchedulerError("LAUNCHD_PLIST_INVALID", "The launchd plist must contain a dictionary")
    required = {
        "Disabled",
        "EnvironmentVariables",
        "ExitTimeOut",
        "KeepAlive",
        "Label",
        "ProcessType",
        "Program",
        "ProgramArguments",
        "RunAtLoad",
        "StandardErrorPath",
        "StandardOutPath",
        "ThrottleInterval",
        "Umask",
        "WorkingDirectory",
    }
    if set(value) != required:
        raise SchedulerError(
            "LAUNCHD_PLIST_INVALID",
            "The launchd plist keys do not match the reviewed service contract",
        )
    arguments = value.get("ProgramArguments")
    if (
        value.get("Disabled") is not True
        or value.get("KeepAlive") != {"SuccessfulExit": False}
        or value.get("RunAtLoad") is not True
        or value.get("ProcessType") != "Background"
        or not isinstance(arguments, list)
        or len(arguments) != 7
        or arguments[1:3] != ["-m", "codex_work_scheduler"]
        or arguments[3] != "--config"
        or arguments[5:] != ["service", "run"]
        or arguments[0] != value.get("Program")
    ):
        raise SchedulerError(
            "LAUNCHD_PLIST_INVALID",
            "The launchd process contract is unsafe or incomplete",
        )
    environment = value.get("EnvironmentVariables")
    if not isinstance(environment, dict) or set(environment) != {"PATH", "PYTHONUNBUFFERED"}:
        raise SchedulerError("LAUNCHD_PLIST_INVALID", "The launchd environment is not bounded")
    return {
        "disabled": True,
        "keep_alive": {"successful_exit": False},
        "label": value["Label"],
        "program_arguments": arguments,
        "valid": True,
    }
