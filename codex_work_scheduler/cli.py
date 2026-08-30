"""Deterministic JSON-only command-line interface."""

import argparse
import json
import os
import random
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .background import BackgroundSupervisor
from .errors import SchedulerError
from .launchd import DEFAULT_LABEL, check_plist, render_plist, write_plist
from .notifications import LocalJsonlSink, NotificationBus
from .service import Controller
from .store import Store
from .util import canonical_json, load_json_file, make_envelope, new_id
from .validation import validate_config


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise SchedulerError("INVALID_ARGUMENT", "The command arguments are invalid", details={"reason": message})


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="codex-work-scheduler")
    parser.add_argument("--config", required=True, help="Path to a scheduler JSON configuration")
    parser.add_argument("--actor", default="agent", help="Local audit actor identifier")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    subparsers.add_parser("status")

    queue_parser = subparsers.add_parser("queue")
    queue_commands = queue_parser.add_subparsers(dest="queue_command", required=True, parser_class=JsonArgumentParser)
    queue_commands.add_parser("list")
    queue_commands.add_parser("proposals")
    queue_get = queue_commands.add_parser("get")
    queue_get.add_argument("--job-id", required=True)
    queue_proposal_get = queue_commands.add_parser("proposal-get")
    queue_proposal_get.add_argument("--job-id", required=True)
    queue_propose = queue_commands.add_parser("propose")
    queue_propose.add_argument("--package-file", required=True)
    queue_propose.add_argument("--idempotency-key", required=True)
    queue_approve = queue_commands.add_parser("approve")
    queue_approve.add_argument("--job-id", required=True)
    queue_approve.add_argument("--approval-file", required=True)
    queue_approve.add_argument("--idempotency-key", required=True)
    queue_submit = queue_commands.add_parser("submit")
    queue_submit.add_argument("--job-file", required=True)
    queue_submit.add_argument("--approval-file", required=True)
    queue_submit.add_argument("--idempotency-key", required=True)
    queue_cancel = queue_commands.add_parser("cancel")
    queue_cancel.add_argument("--job-id", required=True)
    queue_cancel.add_argument("--idempotency-key", required=True)

    policy_parser = subparsers.add_parser("policy")
    policy_commands = policy_parser.add_subparsers(dest="policy_command", required=True, parser_class=JsonArgumentParser)
    policy_commands.add_parser("show")
    policy_check = policy_commands.add_parser("check")
    policy_check.add_argument("--snapshot-file")
    policy_check.add_argument("--job-file")
    policy_set = policy_commands.add_parser("set")
    policy_set.add_argument("--policy-file", required=True)
    policy_set.add_argument("--approval-file", required=True)
    policy_set.add_argument("--idempotency-key", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument(
        "--action",
        choices=("queue.submit", "queue.approve", "policy.set", "resume", "reconcile", "live-test.run"),
    )
    preflight.add_argument("--input-file")

    approval_parser = subparsers.add_parser("approval")
    approval_commands = approval_parser.add_subparsers(
        dest="approval_command", required=True, parser_class=JsonArgumentParser
    )
    approval_prepare = approval_commands.add_parser("prepare")
    approval_prepare.add_argument(
        "--action",
        required=True,
        choices=("queue.submit", "queue.approve", "policy.set", "resume", "reconcile", "live-test.run"),
    )
    approval_prepare.add_argument("--input-file")
    approval_prepare.add_argument("--approver", default="operator")
    approval_prepare.add_argument("--ttl-seconds", type=int, default=3600)

    pause = subparsers.add_parser("pause")
    pause.add_argument("--reason-code", required=True)
    pause.add_argument("--idempotency-key", required=True)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--approval-file", required=True)
    resume.add_argument("--idempotency-key", required=True)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--reason-code", required=True)
    stop.add_argument("--idempotency-key", required=True)

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--dry-run", action="store_true")
    reconcile.add_argument("--approval-file")
    reconcile.add_argument("--idempotency-key")

    probe = subparsers.add_parser("probe")
    probe.add_argument("--idempotency-key", required=True)

    tick = subparsers.add_parser("tick")
    tick.add_argument("--idempotency-key", required=True)

    cycle = subparsers.add_parser("cycle")
    cycle.add_argument("--idempotency-key", required=True)

    live_test_parser = subparsers.add_parser("live-test")
    live_test_commands = live_test_parser.add_subparsers(
        dest="live_test_command", required=True, parser_class=JsonArgumentParser
    )
    live_test_preflight = live_test_commands.add_parser("preflight")
    live_test_preflight.add_argument("--refresh", action="store_true")
    live_test_preflight.add_argument("--idempotency-key")
    live_test_run = live_test_commands.add_parser("run")
    live_test_run.add_argument("--approval-file", required=True)
    live_test_run.add_argument("--idempotency-key", required=True)

    dispatch_parser = subparsers.add_parser("dispatch")
    dispatch_commands = dispatch_parser.add_subparsers(
        dest="dispatch_command", required=True, parser_class=JsonArgumentParser
    )
    dispatch_preflight = dispatch_commands.add_parser("preflight")
    dispatch_preflight.add_argument("--job-id")
    dispatch_preflight.add_argument("--refresh", action="store_true")
    dispatch_preflight.add_argument("--idempotency-key")
    dispatch_run = dispatch_commands.add_parser("run")
    dispatch_run.add_argument("--job-id")
    dispatch_run.add_argument("--idempotency-key", required=True)

    monitor_parser = subparsers.add_parser("monitor")
    monitor_commands = monitor_parser.add_subparsers(
        dest="monitor_command", required=True, parser_class=JsonArgumentParser
    )
    monitor_commands.add_parser("list")
    monitor_get = monitor_commands.add_parser("get")
    monitor_get.add_argument("--run-id", required=True)
    monitor_refresh = monitor_commands.add_parser("refresh")
    monitor_refresh.add_argument("--run-id", required=True)
    monitor_refresh.add_argument("--idempotency-key", required=True)

    service_parser = subparsers.add_parser("service")
    service_commands = service_parser.add_subparsers(
        dest="service_command", required=True, parser_class=JsonArgumentParser
    )
    service_commands.add_parser("status")
    service_commands.add_parser("once")
    service_run = service_commands.add_parser("run")
    service_run.add_argument("--max-cycles", type=int, default=0)

    notification_parser = subparsers.add_parser("notifications")
    notification_commands = notification_parser.add_subparsers(
        dest="notification_command", required=True, parser_class=JsonArgumentParser
    )
    notification_list = notification_commands.add_parser("list")
    notification_list.add_argument("--limit", type=int, default=100)

    launchd_parser = subparsers.add_parser("launchd")
    launchd_commands = launchd_parser.add_subparsers(
        dest="launchd_command", required=True, parser_class=JsonArgumentParser
    )
    launchd_render = launchd_commands.add_parser("render")
    launchd_render.add_argument("--output", required=True)
    launchd_render.add_argument("--label", default=DEFAULT_LABEL)
    launchd_render.add_argument("--python-path")
    launchd_render.add_argument("--codex-path")
    launchd_render.add_argument("--repository-path")
    launchd_render.add_argument("--stdout-path")
    launchd_render.add_argument("--stderr-path")
    launchd_check = launchd_commands.add_parser("check")
    launchd_check.add_argument("--plist", required=True)

    audit_parser = subparsers.add_parser("audit")
    audit_commands = audit_parser.add_subparsers(dest="audit_command", required=True, parser_class=JsonArgumentParser)
    audit_list = audit_commands.add_parser("list")
    audit_list.add_argument("--limit", type=int, default=100)
    audit_commands.add_parser("verify")
    return parser


def load_config(path: str) -> Dict[str, Any]:
    config = validate_config(load_json_file(path))
    if config["database_path"] != ":memory:" and not os.path.isabs(config["database_path"]):
        base_directory = Path(path).resolve().parent
        database_path = (base_directory / config["database_path"]).resolve()
        try:
            database_path.relative_to(base_directory)
        except ValueError as exc:
            raise SchedulerError(
                "PATH_DENIED",
                "database_path resolves outside the configuration directory",
            ) from exc
        config["database_path"] = str(database_path)
    base_directory = Path(path).resolve().parent
    resolved_roots = []
    for root in config["workspace_roots"]:
        root_path = Path(root)
        if not root_path.is_absolute():
            root_path = base_directory / root_path
        resolved = root_path.resolve()
        if not resolved.is_dir():
            raise SchedulerError("PATH_DENIED", "A configured workspace root is not a directory")
        resolved_roots.append(str(resolved))
    config["workspace_roots"] = resolved_roots
    notification_path = Path(config["background"]["notification_path"])
    if not notification_path.is_absolute():
        notification_path = base_directory / notification_path
    resolved_notification_path = notification_path.resolve()
    try:
        resolved_notification_path.relative_to(base_directory)
    except ValueError as exc:
        raise SchedulerError(
            "PATH_DENIED",
            "background.notification_path resolves outside the configuration directory",
        ) from exc
    config["background"]["notification_path"] = str(resolved_notification_path)
    return config


def command_name(args: argparse.Namespace) -> str:
    if args.command == "queue":
        return "queue.%s" % args.queue_command
    if args.command == "policy":
        return "policy.%s" % args.policy_command
    if args.command == "audit":
        return "audit.%s" % args.audit_command
    if args.command == "approval":
        return "approval.%s" % args.approval_command
    if args.command == "live-test":
        return "live-test.%s" % args.live_test_command
    if args.command == "monitor":
        return "monitor.%s" % args.monitor_command
    if args.command == "dispatch":
        return "dispatch.%s" % args.dispatch_command
    if args.command == "service":
        return "service.%s" % args.service_command
    if args.command == "notifications":
        return "notifications.%s" % args.notification_command
    if args.command == "launchd":
        return "launchd.%s" % args.launchd_command
    return args.command


def background_supervisor(controller: Controller) -> BackgroundSupervisor:
    notifications = NotificationBus(
        controller.store,
        LocalJsonlSink(controller.config["background"]["notification_path"]),
        clock=controller.clock,
    )
    return BackgroundSupervisor(
        controller.config,
        controller,
        controller.store,
        notifications,
        clock=controller.clock,
        random_source=random.random,
    )


def dispatch(args: argparse.Namespace, controller: Controller) -> Dict[str, Any]:
    if args.command == "status":
        return controller.status()
    if args.command == "queue":
        if args.queue_command == "list":
            return controller.queue_list()
        if args.queue_command == "proposals":
            return controller.queue_proposals()
        if args.queue_command == "get":
            return controller.queue_get(args.job_id)
        if args.queue_command == "proposal-get":
            return controller.queue_proposal_get(args.job_id)
        if args.queue_command == "propose":
            return controller.queue_propose(
                load_json_file(args.package_file),
                idempotency_key=args.idempotency_key,
                actor=args.actor,
            )
        if args.queue_command == "approve":
            return controller.queue_approve(
                args.job_id,
                load_json_file(args.approval_file),
                idempotency_key=args.idempotency_key,
                actor=args.actor,
            )
        if args.queue_command == "submit":
            return controller.queue_submit(
                load_json_file(args.job_file),
                load_json_file(args.approval_file),
                idempotency_key=args.idempotency_key,
                actor=args.actor,
            )
        if args.queue_command == "cancel":
            return controller.queue_cancel(
                args.job_id,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
            )
    if args.command == "policy":
        if args.policy_command == "show":
            return controller.policy_show()
        if args.policy_command == "check":
            snapshot = load_json_file(args.snapshot_file) if args.snapshot_file else None
            job = load_json_file(args.job_file) if args.job_file else None
            return controller.policy_check(snapshot_value=snapshot, job_value=job)
        if args.policy_command == "set":
            return controller.policy_set(
                load_json_file(args.policy_file),
                load_json_file(args.approval_file),
                idempotency_key=args.idempotency_key,
                actor=args.actor,
            )
    if args.command == "preflight":
        input_value = load_json_file(args.input_file) if args.input_file else None
        return controller.preflight(action=args.action, input_value=input_value)
    if args.command == "approval":
        input_value = load_json_file(args.input_file) if args.input_file else None
        return controller.approval_prepare(
            action=args.action,
            input_value=input_value,
            requested_approver=args.approver,
            suggested_ttl_seconds=args.ttl_seconds,
        )
    if args.command == "pause":
        return controller.pause(
            reason_code=args.reason_code,
            idempotency_key=args.idempotency_key,
            actor=args.actor,
        )
    if args.command == "resume":
        return controller.resume(
            load_json_file(args.approval_file),
            idempotency_key=args.idempotency_key,
            actor=args.actor,
        )
    if args.command == "stop":
        return controller.stop(
            reason_code=args.reason_code,
            idempotency_key=args.idempotency_key,
            actor=args.actor,
        )
    if args.command == "reconcile":
        if args.dry_run:
            if args.approval_file or args.idempotency_key:
                raise SchedulerError("INVALID_ARGUMENT", "Dry-run reconciliation does not accept approval or idempotency input")
            return controller.reconcile_plan()
        if not args.approval_file or not args.idempotency_key:
            raise SchedulerError("INVALID_ARGUMENT", "Reconciliation requires approval and idempotency input")
        return controller.reconcile(
            load_json_file(args.approval_file),
            idempotency_key=args.idempotency_key,
            actor=args.actor,
        )
    if args.command == "probe":
        return controller.probe_quota(idempotency_key=args.idempotency_key, actor=args.actor)
    if args.command == "tick":
        return controller.tick(idempotency_key=args.idempotency_key, actor=args.actor)
    if args.command == "cycle":
        return controller.cycle(idempotency_key=args.idempotency_key, actor=args.actor)
    if args.command == "live-test":
        if args.live_test_command == "preflight":
            if args.refresh:
                if not args.idempotency_key:
                    raise SchedulerError("IDEMPOTENCY_REQUIRED", "A refresh requires an idempotency key")
                controller.probe_quota(idempotency_key=args.idempotency_key, actor=args.actor)
            elif args.idempotency_key:
                raise SchedulerError("INVALID_ARGUMENT", "An idempotency key requires --refresh")
            return controller.live_test_preflight()
        if args.live_test_command == "run":
            return controller.live_test_run(
                load_json_file(args.approval_file),
                idempotency_key=args.idempotency_key,
                actor=args.actor,
            )
    if args.command == "dispatch":
        if args.dispatch_command == "preflight":
            if args.refresh:
                if not args.idempotency_key:
                    raise SchedulerError("IDEMPOTENCY_REQUIRED", "A refresh requires an idempotency key")
                controller.probe_quota(idempotency_key=args.idempotency_key, actor=args.actor)
            elif args.idempotency_key:
                raise SchedulerError("INVALID_ARGUMENT", "An idempotency key requires --refresh")
            return controller.dispatch_preflight(job_id=args.job_id)
        if args.dispatch_command == "run":
            return controller.dispatch_run(
                job_id=args.job_id,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
            )
    if args.command == "monitor":
        if args.monitor_command == "list":
            return controller.monitor_list()
        if args.monitor_command == "get":
            return controller.monitor_get(args.run_id)
        if args.monitor_command == "refresh":
            return controller.monitor_refresh(
                args.run_id,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
            )
    if args.command == "service":
        supervisor = background_supervisor(controller)
        if args.service_command == "status":
            return supervisor.status()
        if args.service_command == "once":
            return supervisor.run(max_cycles=1)
        if args.service_command == "run":
            return supervisor.run(max_cycles=args.max_cycles)
    if args.command == "notifications":
        controller._require_capability("background.read")
        if args.limit < 1 or args.limit > 1000:
            raise SchedulerError("INVALID_ARGUMENT", "Notification limit must be between 1 and 1000")
        return {"events": controller.store.list_notifications(limit=args.limit)}
    if args.command == "launchd":
        controller._require_capability("launchd.render")
        if args.launchd_command == "check":
            return check_plist(args.plist)
        if args.launchd_command == "render":
            config_path = Path(args.config).resolve()
            repository_path = args.repository_path or str(Path(__file__).resolve().parents[1])
            python_path = args.python_path or sys.executable
            codex_path = args.codex_path or shutil.which("codex")
            if codex_path is None:
                raise SchedulerError("LAUNCHD_PATH_INVALID", "The Codex executable was not found")
            runtime_directory = config_path.parent / ".scheduler"
            stdout_path = args.stdout_path or str(runtime_directory / "service.stdout.jsonl")
            stderr_path = args.stderr_path or str(runtime_directory / "service.stderr.log")
            content = render_plist(
                python_path=python_path,
                codex_path=codex_path,
                repository_path=repository_path,
                config_path=str(config_path),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                label=args.label,
            )
            return write_plist(args.output, content)
    if args.command == "audit":
        if args.audit_command == "list":
            return controller.audit_list(limit=args.limit)
        if args.audit_command == "verify":
            return controller.audit_verify()
    raise SchedulerError("INVALID_ARGUMENT", "The command is unsupported")


def main(argv: Optional[Sequence[str]] = None) -> int:
    request_id = new_id("req")
    command = "unknown"
    try:
        args = build_parser().parse_args(argv)
        command = command_name(args)
        config = load_config(args.config)
        controller = Controller(config, Store(config["database_path"]))
        result = dispatch(args, controller)
        envelope = make_envelope(command, request_id, result=result)
        exit_code = 0
    except SchedulerError as exc:
        envelope = make_envelope(command, request_id, error=exc.as_dict())
        exit_code = 2
    except Exception:
        error = SchedulerError("INTERNAL_ERROR", "The command failed without changing external state")
        envelope = make_envelope(command, request_id, error=error.as_dict())
        exit_code = 3
    sys.stdout.write(canonical_json(envelope) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
