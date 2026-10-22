#!/usr/bin/env python3
"""Restic-based, systemd-friendly game-node backup manager."""

from __future__ import annotations

import argparse
import curses
from collections import deque
import fcntl
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import termios
import tty
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError, available_timezones


APP_NAME = "restic-node-backup"
BACKUP_TAG = "game-node-backup"
BACKUP_HOST = socket.gethostname()
CONFIG_PATH = Path(os.environ.get("RNB_CONFIG", "/etc/restic-node-backup/config.json"))
STATE_DIR = Path(os.environ.get("RNB_STATE_DIR", "/var/lib/restic-node-backup"))
STATE_PATH = STATE_DIR / "state.json"
LOCK_PATH = STATE_DIR / "backup.lock"
LOG_PATH = STATE_DIR / "backup.log"
DEFAULT_SOURCE = "/var/lib/pterodactyl"
PTERODACTYL_VOLUMES = Path("/var/lib/pterodactyl/volumes")
SERVER_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
LOCK_ERRORS = (
    "repository is already locked",
    "repository is locked exclusively",
    "unable to create lock",
    "stale lock",
)
LOCK_RETRY_INTERVAL = "30s"
LOCK_RETRY_ATTEMPTS = 60
LOCK_RETRY_DURATION = "30m"
STORAGE_REFRESH_SECONDS = 1800
MISSING_REPOSITORY_ERRORS = (
    "config file does not exist",
    "repository does not exist",
    "is not a restic repository",
    "404 not found",
    "specified key does not exist",
    "the key does not exist",
    "nosuchkey",
)


class AppError(Exception):
    """An actionable application or Restic error."""


class TaskCancelled(AppError):
    """A Restic operation was cancelled by the operator."""


class TaskControl:
    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self.phase = "Starting"
        self.detail = ""
        self.progress: dict[str, Any] = {}

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def attach(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._process = process
            if self.cancelled and process.poll() is None:
                process.terminate()

    def detach(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            if self._process is process:
                self._process = None

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

            def force_stop() -> None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

            threading.Thread(target=force_stop, name="restic-cancel-watch", daemon=True).start()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise TaskCancelled("Operation cancelled by user.")

    def update(self, **values: Any) -> None:
        if "phase" in values:
            phase = str(values["phase"])
            if phase != self.phase:
                self.progress.clear()
            self.phase = phase
        if "detail" in values:
            self.detail = str(values["detail"])
        self.progress.update(values)


@dataclass
class Config:
    source: str = DEFAULT_SOURCE
    repository: str = ""
    password: str = ""
    access_key: str = ""
    secret_key: str = ""
    session_token: str = ""
    region: str = ""
    interval_hours: int = 24
    keep_last: int = 7
    timezone_name: str = "UTC"
    restic_binary: str = "restic"

    def validate(self) -> None:
        string_fields = (
            self.source, self.repository, self.password, self.access_key,
            self.secret_key, self.session_token, self.region, self.restic_binary,
            self.timezone_name,
        )
        if any(not isinstance(value, str) for value in string_fields):
            raise AppError("Text configuration fields must be strings.")
        if isinstance(self.interval_hours, bool) or not isinstance(self.interval_hours, int):
            raise AppError("Backup interval must be a whole number of hours.")
        if isinstance(self.keep_last, bool) or not isinstance(self.keep_last, int):
            raise AppError("Snapshot retention must be a whole number.")
        if not Path(self.source).is_absolute():
            raise AppError("The backup source must be an absolute path.")
        if not self.repository.strip():
            raise AppError("Set a Restic repository (for example s3:https://s3.example/bucket/node).")
        if bool(self.access_key) != bool(self.secret_key):
            raise AppError("Set both S3 access key and secret key, or leave both empty.")
        if self.session_token and not self.access_key:
            raise AppError("An S3 session token requires an access key and secret key.")
        if not 1 <= self.interval_hours <= 8760:
            raise AppError("Backup interval must be between 1 and 8760 hours.")
        if not 1 <= self.keep_last <= 10000:
            raise AppError("Keep-last retention must be between 1 and 10000 snapshots.")
        if not self.restic_binary.strip():
            raise AppError("Restic binary path cannot be empty.")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise AppError(
                f"Unknown time zone {self.timezone_name!r}; enter an IANA name such as Asia/Kolkata."
            ) from exc


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_PATH
        self._thread_lock = threading.RLock()

    def _acquire(self, operation: int) -> Any:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        lock_path = self.path.with_suffix(".lock")
        stream = open(lock_path, "a", encoding="utf-8")
        os.chmod(lock_path, 0o600)
        fcntl.flock(stream.fileno(), operation)
        return stream

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise AppError(f"State file {self.path} is invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise AppError(f"State file {self.path} must contain a JSON object.")
        return data

    def read(self) -> dict[str, Any]:
        with self._thread_lock:
            lock = self._acquire(fcntl.LOCK_SH)
            try:
                return self._read_unlocked()
            finally:
                lock.close()

    def update(self, **changes: Any) -> dict[str, Any]:
        with self._thread_lock:
            lock = self._acquire(fcntl.LOCK_EX)
            try:
                state = self._read_unlocked()
                state.update(changes)
                temporary = self.path.with_suffix(".tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(state, stream, indent=2)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                    os.chmod(self.path, 0o600)
                except OSError:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                    raise
                return state
            finally:
                lock.close()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime | None = None) -> str:
    return (value or now_utc()).isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AppError(f"Invalid timestamp in backup state: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AppError(f"Configuration not found at {path}. Run `configure` first.") from exc
    except json.JSONDecodeError as exc:
        raise AppError(f"Configuration file {path} is invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AppError(f"Configuration file {path} must contain a JSON object.")
    allowed = Config.__dataclass_fields__.keys()
    try:
        config = Config(**{key: value for key, value in raw.items() if key in allowed})
    except TypeError as exc:
        raise AppError(f"Configuration file {path} has invalid fields: {exc}") from exc
    config.validate()
    return config


def save_config(config: Config, path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    config.validate()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(asdict(config), stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def create_logger() -> logging.Logger:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.addHandler(logging.StreamHandler(sys.stderr))
    os.chmod(LOG_PATH, 0o600)
    return logger


def _is_restic_progress(line: str) -> bool:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    return isinstance(event, dict) and event.get("message_type") in ("status", "verbose_status")


def restic_environment(config: Config) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "RESTIC_REPOSITORY": config.repository,
        "RESTIC_CACHE_DIR": str(STATE_DIR / "cache"),
    })
    env.pop("RESTIC_PASSWORD_FILE", None)
    env.pop("RESTIC_PASSWORD_COMMAND", None)
    if config.password:
        env["RESTIC_PASSWORD"] = config.password
    else:
        env.pop("RESTIC_PASSWORD", None)
    if config.access_key:
        env["AWS_ACCESS_KEY_ID"] = config.access_key
    else:
        env.pop("AWS_ACCESS_KEY_ID", None)
    if config.secret_key:
        env["AWS_SECRET_ACCESS_KEY"] = config.secret_key
    else:
        env.pop("AWS_SECRET_ACCESS_KEY", None)
    if config.session_token:
        env["AWS_SESSION_TOKEN"] = config.session_token
    else:
        env.pop("AWS_SESSION_TOKEN", None)
    if config.region:
        env["AWS_DEFAULT_REGION"] = config.region
    return env


def _redact(text: str, config: Config) -> str:
    for secret in (config.password, config.secret_key, config.access_key, config.session_token):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _unlock(config: Config, logger: logging.Logger) -> bool:
    logger.warning("Restic reported a repository lock; asking Restic to remove stale locks safely.")
    try:
        result = subprocess.run(
            _restic_command(config, ["unlock"]),
            env=restic_environment(config),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except OSError as exc:
        logger.error("Could not run `restic unlock`: %s", exc)
        return False
    output = _redact(result.stdout or "", config).strip()
    if output:
        logger.info("restic unlock: %s", output[-4000:])
    if result.returncode != 0:
        logger.error("Restic could not safely unlock the repository (exit %s).", result.returncode)
        return False
    return True


def _run_restic(
    config: Config,
    arguments: list[str],
    logger: logging.Logger,
    *,
    lock_retry: bool = True,
    control: TaskControl | None = None,
) -> subprocess.CompletedProcess[str]:
    max_attempts = LOCK_RETRY_ATTEMPTS + 1 if lock_retry else 1
    for attempt in range(max_attempts):
        command = _restic_command(
            config,
            arguments,
            retry_lock=LOCK_RETRY_INTERVAL if attempt else None,
        )
        if control is not None:
            control.check_cancelled()
        try:
            process = subprocess.Popen(
                command,
                env=restic_environment(config),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise AppError(f"Restic executable {config.restic_binary!r} was not found. Install Restic first.") from exc
        except OSError as exc:
            raise AppError(f"Could not start Restic: {exc}") from exc
        if control is not None:
            control.attach(process)
            control.update(phase="Restic " + " ".join(arguments[:2]), detail="")
        output_lines: deque[str] | list[str] = (
            [] if arguments[:2] == ["snapshots", "--json"] else deque(maxlen=200)
        )
        if process.stdout is not None:
            for line in process.stdout:
                safe_line = _redact(line.rstrip(), config)
                output_lines.append(safe_line)
                if arguments[:2] != ["snapshots", "--json"] and not _is_restic_progress(safe_line):
                    logger.info("restic: %s", safe_line)
                if control is not None:
                    control.update(detail=safe_line[:500])
                    _persist_task_progress(control, control.phase)
        return_code = process.wait()
        if control is not None:
            control.detach(process)
        output = "\n".join(output_lines)
        if return_code == 0:
            return subprocess.CompletedProcess(command, return_code, output)
        if control is not None:
            control.check_cancelled()
        if attempt + 1 < max_attempts and any(marker in output.lower() for marker in LOCK_ERRORS):
            if _unlock(config, logger):
                logger.info("Stale repository lock removed safely; retrying Restic.")
            else:
                logger.warning(
                    "Repository lock is still active or could not be confirmed stale; "
                    "retrying Restic and waiting up to %s for it to clear.",
                    LOCK_RETRY_DURATION,
                )
            continue
        safe_output = _redact(output, config).strip()
        detail = safe_output[-4000:] if safe_output else "No diagnostic output."
        raise AppError(f"Restic {' '.join(arguments[:2])} failed (exit {return_code}):\n{detail}")
    raise AppError("Restic retry ended unexpectedly.")


def _stream_backup(
    config: Config,
    logger: logging.Logger,
    control: TaskControl | None = None,
) -> None:
    arguments = [
        "backup", "--json",
        "--tag", BACKUP_TAG,
        "--host", BACKUP_HOST,
        config.source,
    ]
    for attempt in range(LOCK_RETRY_ATTEMPTS + 1):
        command = _restic_command(
            config,
            arguments,
            retry_lock=LOCK_RETRY_INTERVAL if attempt else None,
        )
        try:
            process = subprocess.Popen(
                command,
                env=restic_environment(config),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise AppError(f"Restic executable {config.restic_binary!r} was not found. Install Restic first.") from exc
        except OSError as exc:
            raise AppError(f"Could not start Restic: {exc}") from exc
        if control is not None:
            control.attach(process)
            control.update(phase="Backing up", detail="Waiting for Restic progress…")
        lines: deque[str] = deque(maxlen=100)
        if process.stdout is not None:
            for line in process.stdout:
                safe_line = _redact(line.rstrip(), config)
                if not _is_restic_progress(safe_line):
                    logger.info("restic: %s", safe_line)
                lines.append(safe_line)
                if control is not None:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        control.update(detail=safe_line[:500])
                    else:
                        if isinstance(event, dict):
                            control.update(**event)
                            _persist_task_progress(control, "Backing up")
        return_code = process.wait()
        if control is not None:
            control.detach(process)
        if return_code == 0:
            return
        if control is not None:
            control.check_cancelled()
        output = "\n".join(lines)
        if attempt + 1 < LOCK_RETRY_ATTEMPTS + 1 and any(
            marker in output.lower() for marker in LOCK_ERRORS
        ):
            if _unlock(config, logger):
                logger.info("Stale repository lock removed safely; retrying backup.")
            else:
                logger.warning(
                    "Repository lock is still active or could not be confirmed stale; "
                    "retrying backup and waiting up to %s for it to clear.",
                    LOCK_RETRY_DURATION,
                )
            continue
        detail = output[-4000:] if output else "No diagnostic output."
        raise AppError(f"Restic backup failed (exit {return_code}):\n{detail}")
    raise AppError("Restic backup retry ended unexpectedly.")


def _persist_task_progress(control: TaskControl, operation: str) -> None:
    store = StateStore()
    backup_operation = operation == "Backing up" or operation.startswith("Restic forget")
    values: dict[str, Any] = dict(
        task_status="running",
        operation=operation,
        operation_pid=os.getpid(),
        progress=control.progress,
        active_detail=control.detail,
    )
    if backup_operation or store.read().get("status") == "running":
        values["status"] = "running"
    store.update(**values)


def _task_process_is_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        command = Path(f"/proc/{process_id}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return any(
        argument and Path(os.fsdecode(argument)).name == "restic_node_backup.py"
        for argument in command
    )


def _recover_interrupted_task(store: StateStore, logger: logging.Logger) -> None:
    state = store.read()
    if (
        state.get("status") == "failed"
        and state.get("retry_at") is None
        and state.get("last_success")
        and (
            str(state.get("last_error") or "").startswith("Previous task (Restic ")
            or (
                state.get("operation") is None
                and state.get("failure_count", 0) == 0
            )
        )
    ):
        store.update(
            status="success",
            task_status="failed",
            last_operation_error=state.get("last_error") or state.get("last_operation_error"),
            last_error=None,
        )
        logger.info("Restored the last successful backup status after a non-backup task failure.")
        return
    if state.get("status") != "running" and state.get("task_status") != "running":
        return
    try:
        process_id = int(state.get("operation_pid", 0))
    except (TypeError, ValueError):
        process_id = 0
    if process_id > 0 and _task_process_is_alive(process_id):
        return
    operation = str(state.get("operation") or "Unknown operation")
    is_backup = operation in ("Preparing backup", "Backing up") or operation.startswith("Restic forget")
    message = f"Previous task ({operation}) stopped before recording completion."
    retry_time = None
    failures = state.get("failure_count", 0)
    if is_backup:
        message += " Backup will be retried automatically."
        retry_time = iso_time(now_utc().replace(microsecond=0) + timedelta(minutes=1))
        try:
            failures = int(failures) + 1
        except (TypeError, ValueError):
            failures = 1
    store.update(
        status=("failed" if is_backup else "success" if state.get("last_success") else "waiting"),
        task_status="failed",
        operation=None,
        operation_pid=None,
        progress={},
        active_detail=None,
        cancel_requested=False,
        failure_count=failures,
        retry_at=retry_time,
        last_error=message if is_backup else None,
        last_operation_error=None if is_backup else message,
    )
    if retry_time:
        logger.warning("%s Next attempt at %s.", message, retry_time)
    else:
        logger.warning(message)


def _backup_lock() -> Any:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    stream = open(LOCK_PATH, "a", encoding="utf-8")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        return None
    os.chmod(LOCK_PATH, 0o600)
    return stream


def _start_manual_backup() -> str:
    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        raise AppError("systemd-run is required to start a backup that survives closing the dashboard.")
    script_path = Path(__file__).resolve()
    unit_name = f"restic-node-backup-manual-{os.getpid()}-{int(time.time())}"
    command = [
        systemd_run,
        "--system",
        "--no-block",
        "--collect",
        f"--unit={unit_name}",
        "--property=Type=exec",
        "--property=User=root",
        "--property=Group=root",
        "--property=StateDirectory=restic-node-backup",
        "--property=StateDirectoryMode=0700",
        "--property=UMask=0077",
        "--property=NoNewPrivileges=true",
        "--property=PrivateTmp=true",
        "--property=ProtectSystem=full",
        "--property=ProtectHome=read-only",
        "--property=ProtectKernelTunables=true",
        "--property=ProtectKernelModules=true",
        "--property=ProtectControlGroups=true",
        "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        f"--property=WorkingDirectory={script_path.parent}",
        sys.executable,
        str(script_path),
        "run-now",
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except OSError as exc:
        raise AppError(f"Could not start the persistent backup service: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stdout or "").strip()
        raise AppError(
            "Could not start the persistent backup service"
            + (f":\n{detail[-2000:]}" if detail else f" (exit {result.returncode}).")
        )
    return f"Backup started as {unit_name}; it will continue if you close the dashboard."


def run_backup(control: TaskControl | None = None) -> bool:
    logger = create_logger()
    config = load_config()
    control = control or TaskControl()
    lock = _backup_lock()
    if lock is None:
        logger.warning("Backup skipped because another local backup process holds the lock.")
        return False
    store = StateStore()
    started = now_utc()
    watcher_stop = threading.Event()

    def watch_cancel_request() -> None:
        while not watcher_stop.wait(0.5):
            try:
                if StateStore().read().get("cancel_requested"):
                    control.cancel()
                    return
            except AppError as exc:
                logger.error("Cannot read cancellation state: %s", exc)

    watcher = threading.Thread(target=watch_cancel_request, name="backup-cancel-watch", daemon=True)
    try:
        store.update(
            status="running",
            task_status="running",
            operation="Preparing backup",
            operation_pid=os.getpid(),
            progress={},
            active_detail="Checking source directory…",
            cancel_requested=False,
            last_attempt=iso_time(started),
            last_error=None,
        )
        watcher.start()
        source = Path(config.source)
        if not source.is_dir():
            raise AppError(f"Backup source {source} does not exist or is not a directory.")
        logger.info("Starting backup of %s to configured Restic repository.", source)
        _stream_backup(config, logger, control)
        retention_error = None
        try:
            _run_restic(
                config,
                ["forget", "--tag", BACKUP_TAG, "--host", BACKUP_HOST,
                 "--keep-last", str(config.keep_last), "--prune"],
                logger,
                control=control,
            )
        except AppError as exc:
            retention_error = str(exc)
            logger.error("Backup completed, but retention cleanup failed: %s", exc)
        completed = now_utc()
        store.update(
            status="success",
            task_status="success",
            operation=None,
            operation_pid=None,
            progress={},
            active_detail=None,
            cancel_requested=False,
            last_success=iso_time(completed),
            last_error=None,
            failure_count=0,
            retry_at=None,
            retention_error=retention_error,
        )
        logger.info("Backup completed successfully.")
        return True
    except TaskCancelled as exc:
        store.update(
            status="cancelled",
            task_status="cancelled",
            operation=None,
            operation_pid=None,
            progress={},
            active_detail=None,
            cancel_requested=False,
            last_error=str(exc),
            retry_at=None,
        )
        logger.warning("Backup cancelled by user.")
        return False
    except AppError as exc:
        previous = store.read()
        try:
            failures = int(previous.get("failure_count", 0)) + 1
        except (TypeError, ValueError):
            failures = 1
        delay = min(3600, 60 * (2 ** min(failures - 1, 6)))
        retry_at = now_utc().timestamp() + delay
        retry_time = iso_time(datetime.fromtimestamp(retry_at, timezone.utc))
        store.update(
            status="failed",
            task_status="failed",
            operation=None,
            operation_pid=None,
            progress={},
            active_detail=None,
            cancel_requested=False,
            last_error=str(exc),
            failure_count=failures,
            retry_at=retry_time,
        )
        logger.error("Backup failed; retry scheduled for %s: %s", retry_time, exc)
        return False
    finally:
        watcher_stop.set()
        if watcher.is_alive():
            watcher.join(timeout=1)
        lock.close()


def run_scheduler(stop: threading.Event | None = None) -> None:
    logger = create_logger()
    stop = stop or threading.Event()
    store = StateStore()
    _recover_interrupted_task(store, logger)
    logger.info("Scheduler started.")
    while not stop.is_set():
        _recover_interrupted_task(store, logger)
        config = load_config()
        state = store.read()
        next_run = parse_time(state.get("next_run"))
        if next_run is None:
            last_success = parse_time(state.get("last_success"))
            next_run = (
                last_success.timestamp() + config.interval_hours * 3600
                if last_success is not None
                else now_utc().timestamp()
            )
            next_run = datetime.fromtimestamp(next_run, timezone.utc)
            store.update(next_run=iso_time(next_run))
        retry_at = parse_time(state.get("retry_at"))
        current = now_utc()
        retry_before_schedule = retry_at is not None and retry_at < next_run
        run_at = retry_at if retry_before_schedule else next_run
        if current >= run_at:
            scheduled = next_run
            success = run_backup()
            if success:
                if not retry_before_schedule:
                    completed = now_utc()
                    interval = config.interval_hours * 3600
                    due = scheduled.timestamp() + interval
                    while due <= completed.timestamp():
                        due += interval
                    store.update(next_run=iso_time(datetime.fromtimestamp(due, timezone.utc)))
            elif store.read().get("status") == "cancelled":
                due = now_utc().timestamp() + config.interval_hours * 3600
                store.update(next_run=iso_time(datetime.fromtimestamp(due, timezone.utc)))
            elif parse_time(store.read().get("retry_at")) is None:
                stop.wait(10)
            continue
        wait_until = next_run
        if retry_at is not None and retry_at > current and retry_at < next_run:
            wait_until = retry_at
        delay = max(1, min(30, int((wait_until - current).total_seconds())))
        stop.wait(delay)
    logger.info("Scheduler stopped.")


def restic_snapshots(control: TaskControl | None = None) -> list[dict[str, Any]]:
    config = load_config()
    result = _run_restic(config, ["snapshots", "--json"], create_logger(), control=control)
    try:
        snapshots = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise AppError(f"Restic returned invalid snapshot data: {exc}") from exc
    if not isinstance(snapshots, list):
        raise AppError("Restic returned an unexpected snapshots response.")
    if any(not isinstance(snapshot, dict) for snapshot in snapshots):
        raise AppError("Restic returned a snapshot entry in an unexpected format.")
    return snapshots


def _server_volume_path(server_uuid: str) -> Path:
    if not SERVER_UUID_PATTERN.fullmatch(server_uuid):
        raise AppError("Enter a valid server UUID, for example 2d976043-3b60-40cb-915b-bfae65896c91.")
    return PTERODACTYL_VOLUMES / server_uuid.lower()


def find_server_snapshots(
    server_uuid: str,
    control: TaskControl | None = None,
) -> list[dict[str, Any]]:
    volume_path = _server_volume_path(server_uuid)
    config = load_config()
    result = _run_restic(
        config,
        ["find", "--json", server_uuid.lower()],
        create_logger(),
        control=control,
    )
    try:
        matches = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise AppError(f"Restic returned invalid search results: {exc}") from exc
    if not isinstance(matches, list) or any(not isinstance(match, dict) for match in matches):
        raise AppError("Restic returned server search results in an unexpected format.")

    matching_ids: set[str] = set()
    for result_group in matches:
        snapshot_id = result_group.get("snapshot")
        entries = result_group.get("matches")
        if not isinstance(snapshot_id, str) or not isinstance(entries, list):
            raise AppError("Restic returned a server search result in an unexpected format.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise AppError("Restic returned a server search match in an unexpected format.")
            path = entry.get("path")
            if isinstance(path, str) and (
                path == str(volume_path) or path.startswith(f"{volume_path}/")
            ):
                matching_ids.add(snapshot_id)
                break

    snapshots = restic_snapshots(control)
    return [
        snapshot
        for snapshot in snapshots
        if isinstance(snapshot.get("id"), str) and snapshot["id"] in matching_ids
    ]


def _restic_command(
    config: Config,
    arguments: list[str],
    *,
    retry_lock: str | None = None,
) -> list[str]:
    command = [config.restic_binary]
    if not config.password:
        command.append("--insecure-no-password")
    if retry_lock is not None:
        command.extend(["--retry-lock", retry_lock])
    command.extend(arguments)
    return command


def verify_repository(read_data: bool = False, control: TaskControl | None = None) -> str:
    config = load_config()
    args = ["check"]
    if read_data:
        args.append("--read-data")
    result = _run_restic(config, args, create_logger(), control=control)
    output = _redact(result.stdout or "", config).strip()
    return f"{output}\nRepository check completed successfully.".strip()


def restore_snapshot(
    snapshot_id: str,
    target: str,
    control: TaskControl | None = None,
) -> str:
    config = load_config()
    if not re.fullmatch(r"(?:latest|[0-9a-fA-F]{4,64})", snapshot_id):
        raise AppError("Snapshot must be `latest` or a hexadecimal Restic snapshot ID.")
    target_path = _validate_restore_target(target, config)
    try:
        target_path.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise AppError(f"Cannot create restore directory {target_path}: {exc}") from exc
    _run_restic(
        config,
        ["restore", snapshot_id, "--target", str(target_path)],
        create_logger(),
        control=control,
    )
    return f"Restored snapshot {snapshot_id} into {target_path}."


def restore_server_snapshot(
    snapshot_id: str,
    server_uuid: str,
    control: TaskControl | None = None,
) -> str:
    volume_path = _server_volume_path(server_uuid)
    if not re.fullmatch(r"[0-9a-fA-F]{4,64}", snapshot_id):
        raise AppError("Snapshot must be a hexadecimal Restic snapshot ID.")
    config = load_config()
    search = _run_restic(
        config,
        ["find", "--json", "--snapshot", snapshot_id, volume_path.name],
        create_logger(),
        control=control,
    )
    try:
        matches = json.loads(search.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise AppError(f"Restic returned invalid server search results: {exc}") from exc
    if not isinstance(matches, list) or any(not isinstance(match, dict) for match in matches):
        raise AppError("Restic returned server search results in an unexpected format.")
    snapshot_has_server = False
    for result_group in matches:
        entries = result_group.get("matches")
        if not isinstance(entries, list):
            raise AppError("Restic returned a server search result in an unexpected format.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise AppError("Restic returned a server search match in an unexpected format.")
            path = entry.get("path")
            if isinstance(path, str) and (
                path == str(volume_path) or path.startswith(f"{volume_path}/")
            ):
                snapshot_has_server = True
                break
        if snapshot_has_server:
            break
    if not snapshot_has_server:
        raise AppError(
            f"Snapshot {snapshot_id} does not contain server volume {volume_path}."
        )
    _run_restic(
        config,
        ["restore", snapshot_id, "--target", "/", "--include", str(volume_path)],
        create_logger(),
        control=control,
    )
    return f"Restored server {volume_path.name} from snapshot {snapshot_id} into {volume_path}."


def _validate_restore_target(target: str, config: Config) -> Path:
    raw_target = Path(target).expanduser()
    if not raw_target.is_absolute():
        raise AppError("Enter an absolute restore path, for example /srv/restores/game-node.")
    try:
        target_path = raw_target.resolve()
    except (OSError, RuntimeError) as exc:
        raise AppError(f"Cannot resolve restore path {raw_target}: {exc}") from exc
    if target_path.exists():
        if not target_path.is_dir():
            raise AppError(f"Restore target {target_path} exists but is not a directory.")
        parent_to_check = target_path
    else:
        parent_to_check = target_path.parent
        if not parent_to_check.exists():
            raise AppError(
                f"Parent folder {parent_to_check} does not exist. Create it first or enter "
                "a restore path inside an existing folder."
            )
        if not parent_to_check.is_dir():
            raise AppError(f"Parent path {parent_to_check} exists but is not a directory.")
    if not os.access(parent_to_check, os.W_OK | os.X_OK):
        raise AppError(f"No write permission for restore folder {parent_to_check}.")
    source_path = Path(config.source).resolve()
    if target_path != Path("/") and (
        target_path == source_path
        or target_path in source_path.parents
        or source_path in target_path.parents
    ):
        raise AppError(
            "Restore target overlaps the live backup source. Choose a separate directory "
            "such as /srv/restores/game-node."
        )
    return target_path


def prune_repository(control: TaskControl | None = None) -> str:
    config = load_config()
    result = _run_restic(
        config,
        ["forget", "--tag", BACKUP_TAG, "--host", BACKUP_HOST,
         "--keep-last", str(config.keep_last), "--prune"],
        create_logger(),
        control=control,
    )
    output = _redact(result.stdout or "", config).strip()
    return f"{output}\nRetention applied: keep the latest {config.keep_last} snapshots.".strip()


def initialize_repository(control: TaskControl | None = None) -> str:
    config = load_config()
    result = _run_restic(config, ["init"], create_logger(), control=control)
    output = _redact(result.stdout or "", config).strip()
    return output or "Restic repository initialized."


def ensure_repository() -> str:
    config = load_config()
    logger = create_logger()
    try:
        _run_restic(config, ["cat", "config"], logger)
        return "S3 connection successful; Restic repository is already initialized."
    except AppError as exc:
        message = str(exc).lower()
        if not any(marker in message for marker in MISSING_REPOSITORY_ERRORS):
            raise AppError(
                f"S3 connection/repository check failed. The repository was not initialized: {exc}"
            ) from exc
    initialized = initialize_repository()
    _run_restic(config, ["cat", "config"], logger)
    return f"{initialized}\nS3 connection successful; Restic repository is ready."


def s3_repository(endpoint: str, bucket: str) -> str:
    endpoint = endpoint.strip()
    bucket = bucket.strip()
    if not endpoint:
        raise AppError("S3 endpoint cannot be empty.")
    if (
        not bucket
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or ".." in bucket
        or ".-" in bucket
        or "-." in bucket
    ):
        raise AppError("Enter a valid S3 bucket name (3-63 lowercase letters, digits, dots, or hyphens).")
    candidate = endpoint if "://" in endpoint else f"https://{endpoint}"
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise AppError(f"Invalid S3 endpoint: {exc}") from exc
    if (
        parsed.scheme not in ("https", "http")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in endpoint)
    ):
        raise AppError(
            "Enter an S3 endpoint host or URL, such as s3.eu-central-1.amazonaws.com "
            "or https://s3.example.net (without credentials or a bucket path)."
        )
    return f"s3:{parsed.scheme}://{parsed.netloc.rstrip('/')}/{bucket}"


def _s3_defaults(repository: str) -> tuple[str, str]:
    if not repository.startswith("s3:"):
        return "", ""
    parsed = urlsplit(repository[len("s3:"):])
    bucket = parsed.path.strip("/").split("/", 1)[0]
    endpoint = parsed.netloc
    if parsed.scheme and endpoint:
        endpoint = f"{parsed.scheme}://{endpoint}"
    return endpoint, bucket


def s3_repository_storage_bytes(config: Config) -> int:
    if not config.repository.startswith("s3:"):
        raise AppError("Automatic storage usage is supported for S3 repositories only.")
    parsed = urlsplit(config.repository[len("s3:"):])
    parts = parsed.path.strip("/").split("/", 1)
    if not parsed.scheme or not parsed.netloc or not parts[0]:
        raise AppError("Cannot determine the S3 endpoint and bucket from the repository URL.")
    bucket = parts[0]
    prefix = parts[1].strip("/") + "/" if len(parts) > 1 and parts[1].strip("/") else ""
    endpoint_url = f"{parsed.scheme}://{parsed.netloc}"

    try:
        import boto3
        from botocore.config import Config as BotoConfig
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        raise AppError(
            "The boto3 package is required for automatic S3 storage usage. "
            "Re-run the installer to install it."
        ) from exc

    try:
        session = boto3.Session(
            aws_access_key_id=config.access_key or None,
            aws_secret_access_key=config.secret_key or None,
            aws_session_token=config.session_token or None,
            region_name=config.region or None,
        )
        client = session.client(
            "s3",
            endpoint_url=endpoint_url,
            config=BotoConfig(s3={"addressing_style": "path"}),
        )
        paginator = client.get_paginator("list_objects_v2")
        return sum(
            int(item["Size"])
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
            for item in page.get("Contents", [])
        )
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError) as exc:
        raise AppError(f"Could not read S3 storage usage: {exc}") from exc


def _refresh_storage_usage() -> None:
    store = StateStore()
    logger = create_logger()
    config: Config | None = None
    try:
        config = load_config()
        used_bytes = s3_repository_storage_bytes(config)
    except AppError as exc:
        try:
            store.update(
                storage_checked_at=iso_time(),
                storage_repository=config.repository if config else None,
                storage_error=str(exc),
            )
        except (AppError, OSError) as state_exc:
            logger.error("Could not save S3 storage usage error state: %s", state_exc)
        logger.warning("Could not refresh S3 storage usage: %s", exc)
        return
    try:
        store.update(
            storage_bytes=used_bytes,
            storage_checked_at=iso_time(),
            storage_repository=config.repository,
            storage_error=None,
        )
    except (AppError, OSError) as exc:
        logger.error("Could not save S3 storage usage state: %s", exc)


def _maybe_refresh_storage(
    state: dict[str, Any],
    config: Config | None,
    thread: threading.Thread | None,
) -> threading.Thread | None:
    if (
        config is None
        or not config.repository.startswith("s3:")
        or (thread is not None and thread.is_alive())
    ):
        return thread
    checked_at = parse_time(state.get("storage_checked_at"))
    if (
        state.get("storage_repository") == config.repository
        and checked_at is not None
        and (now_utc() - checked_at).total_seconds() < STORAGE_REFRESH_SECONDS
    ):
        return None
    thread = threading.Thread(
        target=_refresh_storage_usage,
        name="s3-storage-refresh",
        daemon=True,
    )
    thread.start()
    return thread


def _prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    value = getpass.getpass(f"{label}{suffix}: ") if secret else input(f"{label}{suffix}: ")
    return value if value else default


def _timezone_options() -> list[tuple[str, str]]:
    country_names: dict[str, str] = {}
    country_file = Path("/usr/share/iso-codes/json/iso_3166-1.json")
    try:
        countries = json.loads(country_file.read_text(encoding="utf-8")).get("3166-1", [])
    except (OSError, json.JSONDecodeError):
        countries = []
    for country in countries:
        if isinstance(country, dict) and isinstance(country.get("alpha_2"), str):
            country_names[country["alpha_2"]] = str(country.get("name", country["alpha_2"]))

    zone_countries: dict[str, list[str]] = {}
    for root in TZPATH:
        for filename in ("zone1970.tab", "zone.tab"):
            table = Path(root) / filename
            try:
                rows = table.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for row in rows:
                if not row or row.startswith("#"):
                    continue
                columns = row.split("\t")
                if len(columns) >= 3:
                    zone_countries[columns[2]] = columns[0].split(",")
            if zone_countries:
                break
        if zone_countries:
            break

    options: list[tuple[str, str]] = []
    for name in sorted(available_timezones()):
        if name == "localtime" or name.startswith(("posix/", "right/")):
            continue
        codes = zone_countries.get(name, [])
        labels = [country_names.get(code, code) for code in codes]
        label = f"{', '.join(labels)} — {name}" if labels else name
        options.append((name, label))
    if "UTC" not in {name for name, _ in options}:
        options.insert(0, ("UTC", "UTC"))
    return options


def _timezone_matches(options: list[tuple[str, str]], query: str) -> list[tuple[str, str]]:
    normalized = query.strip().casefold()
    if not normalized:
        return options
    country_labels = [
        option for option in options
        if normalized in option[1].split(" — ", 1)[0].casefold()
        and " — " in option[1]
    ]
    exact_country_matches = [
        option for option in country_labels
        if option[1].split(" — ", 1)[0].casefold() == normalized
    ]
    if exact_country_matches:
        return exact_country_matches
    if country_labels:
        return country_labels
    return [
        option for option in options
        if normalized in option[0].casefold() or normalized in option[1].casefold()
    ]


def _select_timezone_cli(current: str) -> str:
    options = _timezone_options()
    while True:
        query = _prompt("Search country, city, or time zone", current)
        matches = _timezone_matches(options, query)
        if not matches:
            print(f"No time zones match {query!r}. Try a country, city, or IANA zone name.")
            continue
        print(f"Matching time zones ({len(matches)}):")
        for index, (_name, label) in enumerate(matches[:40], start=1):
            print(f"  {index:2}. {label}")
        if len(matches) > 40:
            print("  Refine your search to show fewer results.")
        choice = input("Choose a number, or press Enter to search again: ").strip()
        if not choice:
            continue
        if choice.isdigit() and 1 <= int(choice) <= min(40, len(matches)):
            return matches[int(choice) - 1][0]
        try:
            ZoneInfo(choice)
        except (ZoneInfoNotFoundError, ValueError):
            print("Enter a listed number or a valid IANA time-zone name.")
            continue
        return choice


def _prompt_optional_secret(label: str, default: str) -> str:
    value = getpass.getpass(f"{label} [blank keeps current; enter - to clear]: ")
    return "" if value == "-" else value or default


def _prompt_secret_masked(prompt: str) -> str:
    if not sys.stdin.isatty():
        return getpass.getpass(prompt)

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    entered = bytearray()
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        tty.setraw(fd)
        while True:
            char = os.read(fd, 1)
            if char in (b"\r", b"\n"):
                break
            if char == b"\x03":
                raise KeyboardInterrupt
            if char == b"\x04":
                if not entered:
                    raise EOFError
                break
            if char in (b"\x7f", b"\x08"):
                if entered:
                    entered.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if char >= b" ":
                entered.extend(char)
                sys.stdout.write("*")
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)
        sys.stdout.write("\n")
        sys.stdout.flush()
    try:
        return entered.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AppError("Password input must be valid UTF-8.") from exc


def configure_cli() -> None:
    try:
        current = load_config()
    except AppError:
        current = Config()
    print(f"Configure {APP_NAME}. Secret values are stored in a root-only (0600) file.")
    old_endpoint, old_bucket = _s3_defaults(current.repository)
    while True:
        endpoint = _prompt("S3 URL or endpoint (e.g. s3.fra.databucket.eu)", old_endpoint)
        bucket = _prompt("S3 bucket name", old_bucket)
        try:
            repository = s3_repository(endpoint, bucket)
            break
        except AppError as exc:
            print(f"Invalid S3 settings: {exc}", file=sys.stderr)
    entered_password = _prompt_secret_masked(
        "Restic encryption password [blank keeps current; enter - for no encryption]: "
    )
    password = "" if entered_password == "-" else entered_password or current.password
    if not password:
        print(
            "WARNING: Restic repository encryption is disabled. Anyone with access "
            "to the repository can read your backups.",
            file=sys.stderr,
        )
    source = _prompt("Backup source", current.source)
    access_key = _prompt_optional_secret("S3 access key ID", current.access_key)
    secret_key = _prompt_optional_secret("S3 secret access key", current.secret_key)
    session_token = _prompt_optional_secret("S3 session token", current.session_token)
    region = _prompt("S3 region (optional)", current.region)
    interval = _prompt("Backup interval in hours", str(current.interval_hours))
    keep_last = _prompt("Snapshots to retain", str(current.keep_last))
    timezone_name = _select_timezone_cli(current.timezone_name)
    try:
        config = Config(
            source=source,
            repository=repository,
            password=password,
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            region=region,
            interval_hours=int(interval),
            keep_last=int(keep_last),
            timezone_name=timezone_name,
            restic_binary=current.restic_binary,
        )
    except ValueError as exc:
        raise AppError("Backup interval and retention must be whole numbers.") from exc
    save_config(config)
    print(f"Configuration saved securely to {CONFIG_PATH}.")


def _tui_prompt(screen: Any, label: str, default: str = "", *, secret: bool = False) -> str:
    curses.noecho()
    curses.curs_set(1)
    value = list("" if secret else default)
    cursor = len(value)
    while True:
        screen.erase()
        screen.addstr(1, 2, "Configure backup", curses.A_BOLD)
        screen.addstr(3, 2, label[: max(1, curses.COLS - 5)])
        screen.addstr(5, 2, ("*" * len(value)) if secret else "".join(value))
        screen.addstr(curses.LINES - 2, 2, "Enter to accept · Esc to cancel")
        _ui_footer(screen)
        try:
            screen.move(5, min(curses.COLS - 1, 2 + cursor))
        except curses.error:
            pass
        screen.refresh()
        key = screen.getch()
        if key in (10, 13, curses.KEY_ENTER):
            curses.curs_set(0)
            return "".join(value) or default
        if key == 27:
            curses.curs_set(0)
            raise AppError("Configuration cancelled.")
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if cursor:
                value.pop(cursor - 1)
                cursor -= 1
        elif key == 21:
            value.clear()
            cursor = 0
        elif key == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_RIGHT:
            cursor = min(len(value), cursor + 1)
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(value)
        elif key == curses.KEY_DC:
            if cursor < len(value):
                value.pop(cursor)
        elif 32 <= key <= 126 and len(value) < 1024:
            value.insert(cursor, chr(key))
            cursor += 1


def _prompt_restore_target(screen: Any, default: str, config: Config) -> tuple[Path, bool] | None:
    while True:
        try:
            entered = _tui_prompt(
                screen,
                "Restore folder (absolute path; Esc cancels)",
                default,
            )
        except AppError:
            return None
        try:
            target = _validate_restore_target(entered, config)
        except AppError as exc:
            _show_output(
                screen,
                f"Restore folder not accepted.\n\n{exc}\n\n"
                "Check the spelling and enter the full path. Press Enter to try again.",
            )
            default = entered
            continue
        return target, not target.exists()


def _confirm_restore(screen: Any, snapshot_id: str, target: Path, create_target: bool) -> bool:
    restore_to_root = target == Path("/")
    required = "RESTORE TO /" if restore_to_root else "CREATE" if create_target else "RESTORE"
    action = (
        "restore directly into the filesystem root (this can overwrite live files)"
        if restore_to_root
        else "create the missing folder and restore"
        if create_target
        else "restore"
    )
    while True:
        try:
            entered = _tui_prompt(
                screen,
                f"Type {required} to {action} snapshot {snapshot_id[:12]} to {target}",
                "",
            )
        except AppError:
            return False
        if entered == required:
            return True
        _show_output(
            screen,
            f"Confirmation did not match. Type {required} exactly to continue, "
            "or press Esc to cancel.",
        )


def _prompt_server_uuid(screen: Any) -> str | None:
    default = ""
    while True:
        try:
            entered = _tui_prompt(
                screen,
                "Server UUID to search (Esc cancels)",
                default,
            )
        except AppError:
            return None
        try:
            _server_volume_path(entered)
        except AppError as exc:
            _show_output(screen, f"Server UUID not accepted.\n\n{exc}\n\nEnter a UUID to try again.")
            default = entered
            continue
        return entered.lower()


def _confirm_server_restore(screen: Any, snapshot_id: str, server_uuid: str) -> bool:
    volume_path = _server_volume_path(server_uuid)
    required = f"RESTORE {volume_path.name}"
    while True:
        try:
            entered = _tui_prompt(
                screen,
                f"Type {required} to overwrite {volume_path} from snapshot {snapshot_id[:12]}",
                "",
            )
        except AppError:
            return False
        if entered == required:
            return True
        _show_output(
            screen,
            f"Confirmation did not match. Type {required} exactly to overwrite this server "
            "volume, or press Esc to cancel.",
        )


def _timezone_picker(screen: Any, current: str) -> str:
    options = _timezone_options()
    query = ""
    matches = options
    selected = next((index for index, (name, _label) in enumerate(matches) if name == current), 0)
    offset = 0
    while True:
        height, width = screen.getmaxyx()
        visible = max(1, height - 6)
        offset = min(offset, max(0, len(matches) - visible))
        if matches and selected < offset:
            offset = selected
        elif matches and selected >= offset + visible:
            offset = selected - visible + 1
        screen.erase()
        screen.addstr(0, 2, " SELECT TIME ZONE ", curses.A_BOLD)
        screen.addstr(1, 2, f"Search: {query}▏"[:max(1, width - 4)], curses.color_pair(1))
        screen.addstr(2, 2, "Type to filter country/city/zone · ↑/↓ move · PgUp/PgDn · Enter select · Esc cancel")
        if not matches:
            screen.addstr(4, 2, "No matching zones. Backspace to edit your search.")
        for row, item_index in enumerate(range(offset, min(len(matches), offset + visible)), start=4):
            _name, label = matches[item_index]
            attr = curses.color_pair(1) | curses.A_BOLD if item_index == selected else curses.A_NORMAL
            screen.addstr(row, 2, label[:max(1, width - 4)], attr)
        if matches:
            timezone_name = matches[selected][0]
            preview = _timezone_preview(timezone_name)
            screen.addstr(height - 2, 2, f"Local time: {preview} · {selected + 1}/{len(matches)} matches"[:max(1, width - 4)])
        _ui_footer(screen, f"Current setting: {current}")
        screen.refresh()
        key = screen.getch()
        if key == 27:
            raise AppError("Time-zone selection cancelled.")
        if key in (curses.KEY_BACKSPACE, 127, 8):
            query = query[:-1]
            matches = _timezone_matches(options, query)
            selected = next((index for index, (name, _label) in enumerate(matches) if name == current), 0)
            offset = 0
        elif matches and key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif matches and key == curses.KEY_DOWN:
            selected = min(len(matches) - 1, selected + 1)
        elif matches and key == curses.KEY_PPAGE:
            selected = max(0, selected - visible)
        elif matches and key == curses.KEY_NPAGE:
            selected = min(len(matches) - 1, selected + visible)
        elif matches and key == curses.KEY_HOME:
            selected = 0
        elif matches and key == curses.KEY_END:
            selected = len(matches) - 1
        elif key in (10, 13, curses.KEY_ENTER) and matches:
            return matches[selected][0]
        elif 32 <= key <= 126:
            query += chr(key)
            matches = _timezone_matches(options, query)
            selected = next((index for index, (name, _label) in enumerate(matches) if name == current), 0)
            offset = 0
        if matches and selected < offset:
            offset = selected
        elif matches and selected >= offset + visible:
            offset = selected - visible + 1


def _timezone_preview(timezone_name: str) -> str:
    local = now_utc().astimezone(ZoneInfo(timezone_name))
    offset = local.strftime("%z")
    if len(offset) == 5:
        offset = f"{offset[:3]}:{offset[3:]}"
    return f"{local.strftime('%Y-%m-%d %H:%M:%S %Z')} (UTC{offset})"


def _configure_tui(screen: Any) -> str:
    try:
        original = load_config()
    except AppError:
        original = Config()
    endpoint, bucket = _s3_defaults(original.repository)
    values: dict[str, Any] = {
        "endpoint": endpoint,
        "bucket": bucket,
        "password": original.password,
        "source": original.source,
        "access_key": original.access_key,
        "secret_key": original.secret_key,
        "session_token": original.session_token,
        "region": original.region,
        "interval_hours": original.interval_hours,
        "keep_last": original.keep_last,
        "timezone_name": original.timezone_name,
    }
    fields = [
        ("S3 endpoint", "endpoint"),
        ("S3 bucket", "bucket"),
        ("Encryption password", "password"),
        ("Backup source path", "source"),
        ("S3 access key ID", "access_key"),
        ("S3 secret access key", "secret_key"),
        ("S3 session token", "session_token"),
        ("S3 region", "region"),
        ("Backup interval (hours)", "interval_hours"),
        ("Snapshots to retain", "keep_last"),
        ("Snapshot time zone", "timezone_name"),
    ]
    selected = 0
    notice = "Select a setting to edit; Save validates and writes changes."
    save_index = len(fields)
    cancel_index = save_index + 1

    def display_value(key: str) -> str:
        value = values[key]
        if key in ("password", "access_key", "secret_key", "session_token"):
            if not value:
                return "Not set" if key != "password" else "Disabled (unencrypted)"
            return "Configured (hidden)"
        if key == "interval_hours":
            return f"{value} hour(s)"
        if key == "keep_last":
            return f"{value} snapshot(s)"
        if key == "timezone_name":
            return f"{value} · {_snapshot_time_text(now_utc().isoformat(), value).rsplit(' ', 1)[-1]}"
        return str(value) if value else "Not set"

    while True:
        height, width = screen.getmaxyx()
        screen.erase()
        screen.addstr(0, 2, " CONFIGURATION EDITOR ", curses.A_BOLD)
        screen.addstr(1, 2, "↑/↓ move · Enter edit/select · Esc cancel")
        first_row = 3
        visible_fields = max(1, height - first_row - 5)
        offset = max(0, min(selected - visible_fields + 1, len(fields) + 1 - visible_fields))
        rows = fields + [("Save changes", "save"), ("Cancel without saving", "cancel")]
        for row, item_index in enumerate(range(offset, min(len(rows), offset + visible_fields)), start=first_row):
            label, key = rows[item_index]
            if key == "save":
                text = label
            elif key == "cancel":
                text = label
            else:
                text = f"{label:<28} {display_value(key)}"
            attr = curses.color_pair(1) | curses.A_BOLD if item_index == selected else curses.A_NORMAL
            screen.addstr(row, 2, text[:max(1, width - 4)], attr)
        _ui_put(screen, height - 2, 2, notice, width - 4)
        _ui_footer(screen, "Enter edit/save · Esc cancel")
        screen.refresh()
        keypress = screen.getch()
        if keypress in (27, ord("q"), ord("Q")):
            return "Configuration cancelled; no changes saved."
        if keypress in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
            continue
        if keypress in (curses.KEY_DOWN, ord("j")):
            selected = min(cancel_index, selected + 1)
            continue
        if keypress not in (10, 13, curses.KEY_ENTER):
            continue
        if selected == cancel_index:
            return "Configuration cancelled; no changes saved."
        if selected == save_index:
            try:
                config = Config(
                    source=str(values["source"]),
                    repository=s3_repository(str(values["endpoint"]), str(values["bucket"])),
                    password=str(values["password"]),
                    access_key=str(values["access_key"]),
                    secret_key=str(values["secret_key"]),
                    session_token=str(values["session_token"]),
                    region=str(values["region"]),
                    interval_hours=int(values["interval_hours"]),
                    keep_last=int(values["keep_last"]),
                    timezone_name=str(values["timezone_name"]),
                    restic_binary=original.restic_binary,
                )
                config.validate()
                save_config(config)
            except (AppError, ValueError) as exc:
                notice = f"Cannot save: {exc}"
                continue
            message = f"Configuration saved securely to {CONFIG_PATH}."
            if not config.password:
                message += "\nWARNING: This repository is not encrypted."
            return message

        label, key_name = fields[selected]
        if key_name == "timezone_name":
            try:
                values[key_name] = _timezone_picker(screen, str(values[key_name]))
                notice = f"Time zone set to {values[key_name]}."
            except AppError as exc:
                notice = str(exc)
            continue
        if key_name in ("interval_hours", "keep_last"):
            default = str(values[key_name])
        else:
            default = str(values[key_name])
        secret = key_name in ("password", "access_key", "secret_key", "session_token")
        prompt_label = label
        if secret:
            prompt_label += " (blank keeps current; - clears; password - disables encryption)"
        elif key_name == "region":
            prompt_label += " (enter - to clear)"
        try:
            edited = _tui_prompt(screen, prompt_label, default, secret=secret)
        except AppError as exc:
            notice = str(exc)
            continue
        if secret and edited == "-":
            values[key_name] = ""
        elif key_name == "region" and edited == "-":
            values[key_name] = ""
        elif edited != default:
            values[key_name] = edited
        notice = f"Updated {label}. Choose Save changes to validate and persist."


def _do_tui_action(
    action: str,
    result_queue: queue.Queue[Any],
    control: TaskControl,
    snapshot_id: str = "",
    target: str = "",
    server_uuid: str = "",
) -> None:
    task_status = "success"
    task_error: str | None = None
    try:
        if action != "run":
            StateStore().update(
                task_status="running",
                operation=action.replace("_", " ").title(),
                operation_pid=os.getpid(),
                progress={},
                active_detail="Starting task…",
                last_operation_error=None,
            )
        if action == "run":
            result_queue.put(("message", _start_manual_backup()))
        elif action == "verify":
            result_queue.put(("message", verify_repository(control=control)))
        elif action in ("snapshots", "select_restore"):
            result_queue.put(("snapshots", restic_snapshots(control), action))
        elif action == "select_server_restore":
            result_queue.put((
                "server_snapshots",
                find_server_snapshots(server_uuid, control),
                server_uuid,
            ))
        elif action == "prune":
            result_queue.put(("message", prune_repository(control)))
        elif action == "init":
            control.update(phase="Initializing repository")
            result_queue.put(("message", initialize_repository(control)))
        elif action == "restore":
            result_queue.put(("message", restore_snapshot(snapshot_id, target, control)))
        elif action == "restore_server":
            result_queue.put((
                "message",
                restore_server_snapshot(snapshot_id, server_uuid, control),
            ))
    except TaskCancelled as exc:
        task_status = "cancelled"
        task_error = str(exc)
        result_queue.put(("message", str(exc)))
    except (AppError, OSError) as exc:
        task_status = "failed"
        task_error = str(exc)
        result_queue.put(("message", f"ERROR: {exc}"))
    finally:
        if action != "run":
            try:
                StateStore().update(
                    task_status=task_status,
                    operation=None,
                    operation_pid=None,
                    progress={},
                    active_detail=None,
                    cancel_requested=False,
                    last_operation_error=task_error,
                )
            except AppError:
                pass


def _show_output(screen: Any, message: str) -> None:
    height, width = screen.getmaxyx()
    content_width = max(1, width - 6)
    lines: list[str] = []
    for original in message.splitlines() or [""]:
        lines.extend(textwrap.wrap(original, content_width) or [""])
    offset = 0
    page_size = max(1, height - 4)
    while True:
        screen.erase()
        screen.addstr(0, 2, " TASK OUTPUT ", curses.A_BOLD)
        for row, line in enumerate(lines[offset:offset + page_size], start=2):
            screen.addstr(row, 2, line[:content_width])
        _ui_footer(screen, "↑/k previous · ↓/j next · Enter/q close")
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 10, 13, curses.KEY_ENTER, 27):
            return
        if key in (ord("j"), curses.KEY_DOWN):
            offset = min(max(0, len(lines) - page_size), offset + 1)
        elif key in (ord("k"), curses.KEY_UP):
            offset = max(0, offset - 1)


def _activity_log_viewer(screen: Any, timezone_name: str = "UTC") -> None:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = ["No activity log has been created yet."]
    except OSError as exc:
        lines = [f"Cannot read activity log {LOG_PATH}: {exc}"]
    offset = len(lines)
    while True:
        height, width = screen.getmaxyx()
        page_size = max(1, height - 3)
        offset = min(offset, max(0, len(lines) - page_size))
        screen.erase()
        screen.addstr(0, 2, " ACTIVITY LOG ", curses.A_BOLD)
        for row, line in enumerate(lines[offset:offset + page_size], start=1):
            screen.addstr(row, 2, _localized_log_line(line, timezone_name)[:max(1, width - 4)])
        _ui_footer(screen, "↑/k previous · ↓/j next · PgUp/PgDn · Home/End · q close")
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            offset = min(max(0, len(lines) - page_size), offset + 1)
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - page_size)
        elif key == curses.KEY_NPAGE:
            offset = min(max(0, len(lines) - page_size), offset + page_size)
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = max(0, len(lines) - page_size)


def _snapshot_browser(
    screen: Any,
    snapshots: list[dict[str, Any]],
    *,
    allow_restore: bool,
    timezone_name: str = "UTC",
    heading: str = "SNAPSHOTS",
    server_uuid: str = "",
) -> tuple[dict[str, Any], str] | None:
    snapshots = sorted(snapshots, key=lambda snapshot: snapshot.get("time", ""), reverse=True)
    if not snapshots:
        _show_output(screen, "No snapshots found in this repository.")
        return None
    selected = 0
    offset = 0
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        visible = max(1, height - 7)
        offset = min(offset, max(0, len(snapshots) - visible))
        screen.addstr(
            0, 2, f" {heading} · {len(snapshots)} total · {timezone_name} "[: width - 4],
            curses.A_BOLD,
        )
        instruction = (
            "Enter details · r server restore"
            if server_uuid
            else "Enter details · r restore · u server only"
            if allow_restore
            else "Enter details"
        )
        screen.addstr(1, 2, f"↑/↓ or j/k · PgUp/PgDn · {instruction} · q close")
        for row, snapshot_index in enumerate(range(offset, min(len(snapshots), offset + visible)), start=3):
            snapshot = snapshots[snapshot_index]
            snapshot_id = str(snapshot.get("short_id") or snapshot.get("id", ""))[:12]
            date = _snapshot_time_text(snapshot.get("time"), timezone_name)
            hostname = str(snapshot.get("hostname", "unknown"))
            paths = ", ".join(str(path) for path in snapshot.get("paths", []))
            tags = ",".join(str(tag) for tag in snapshot.get("tags", []))
            line = f"{snapshot_id:<12} {date:<23} {hostname}  {paths}  [{tags}]"
            attr = curses.color_pair(1) | curses.A_BOLD if snapshot_index == selected else curses.A_NORMAL
            screen.addstr(row, 2, line[: width - 4], attr)
        snapshot = snapshots[selected]
        screen.addstr(height - 3, 2, f"Selected: {snapshot.get('id', 'unknown')}"[: width - 4])
        screen.addstr(height - 2, 2, f"{selected + 1} / {len(snapshots)}")
        footer = (
            "Enter details · r server restore · q close"
            if server_uuid
            else "Enter details · r restore · u server only · q close"
            if allow_restore
            else "Enter details · q close"
        )
        _ui_footer(screen, footer)
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return None
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(snapshots) - 1, selected + 1)
        elif key == curses.KEY_PPAGE:
            selected = max(0, selected - visible)
        elif key == curses.KEY_NPAGE:
            selected = min(len(snapshots) - 1, selected + visible)
        elif key in (10, 13, curses.KEY_ENTER, ord("d"), ord("D")):
            detail_action = _snapshot_details(
                screen,
                snapshots[selected],
                timezone_name,
                allow_restore,
                server_only=bool(server_uuid),
            )
            if detail_action is not None:
                return snapshots[selected], detail_action
        elif allow_restore and key in (ord("r"), ord("R")):
            return snapshots[selected], "restore_server" if server_uuid else "restore"
        elif allow_restore and not server_uuid and key in (ord("u"), ord("U")):
            return snapshots[selected], "restore_server"
        if selected < offset:
            offset = selected
        elif selected >= offset + visible:
            offset = selected - visible + 1


def _snapshot_details(
    screen: Any,
    snapshot: dict[str, Any],
    timezone_name: str,
    allow_restore: bool,
    *,
    server_only: bool = False,
) -> str | None:
    scalar_fields = (
        ("Snapshot ID", snapshot.get("id")),
        ("Short ID", snapshot.get("short_id")),
        ("Time", _snapshot_time_text(snapshot.get("time"), timezone_name)),
        ("Host", snapshot.get("hostname")),
        ("Username", snapshot.get("username")),
        ("UID", snapshot.get("uid")),
        ("GID", snapshot.get("gid")),
        ("Program version", snapshot.get("program_version")),
        ("Parent snapshot", snapshot.get("parent")),
    )
    lines = [f"{label}: {value if value not in (None, '') else '—'}" for label, value in scalar_fields]
    for label in ("Paths", "Tags", "Excludes"):
        values = snapshot.get(label.lower(), [])
        if isinstance(values, list):
            lines.append(f"{label}: {', '.join(str(value) for value in values) or '—'}")
    summary = snapshot.get("summary")
    if isinstance(summary, dict):
        lines.extend((
            f"Files: {summary.get('total_files_processed', '—')} processed · "
            f"{summary.get('files_new', '—')} new · {summary.get('files_changed', '—')} changed",
            f"Data: {summary.get('total_bytes_processed', '—')} bytes processed · "
            f"{summary.get('data_added', '—')} bytes added",
            f"Duration: {summary.get('total_duration', '—')} seconds",
        ))
    height, width = screen.getmaxyx()
    offset = 0
    page_size = max(1, height - 4)
    while True:
        screen.erase()
        screen.addstr(0, 2, " SNAPSHOT DETAILS ", curses.A_BOLD)
        for row, line in enumerate(lines[offset:offset + page_size], start=2):
            screen.addstr(row, 2, line[:max(1, width - 4)])
        footer = (
            "r server restore · ↑/↓ scroll · q back"
            if allow_restore and server_only
            else "r restore · u server only · ↑/↓ scroll · q back"
            if allow_restore
            else "↑/↓ scroll · q back"
        )
        _ui_footer(screen, footer)
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return None
        if allow_restore and key in (ord("r"), ord("R")):
            return "restore_server" if server_only else "restore"
        if allow_restore and not server_only and key in (ord("u"), ord("U")):
            return "restore_server"
        if key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            offset = min(max(0, len(lines) - page_size), offset + 1)
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - page_size)
        elif key == curses.KEY_NPAGE:
            offset = min(max(0, len(lines) - page_size), offset + page_size)


def _progress_text(progress: dict[str, Any]) -> str:
    total_bytes = progress.get("total_bytes")
    bytes_done = progress.get("bytes_done")
    if isinstance(total_bytes, (int, float)) and total_bytes > 0 and isinstance(bytes_done, (int, float)):
        percent = min(100.0, max(0.0, bytes_done * 100 / total_bytes))
        return (
            f"{percent:5.1f}% · {bytes_done / (1024 ** 3):.2f}/"
            f"{total_bytes / (1024 ** 3):.2f} GiB · "
            f"{progress.get('files_done', 0)}/{progress.get('total_files', 0)} files"
        )
    percent = progress.get("percent_done")
    if isinstance(percent, (int, float)):
        return f"{percent * 100:5.1f}% · {progress.get('files_done', 0)}/{progress.get('total_files', 0)} files"
    return ""


def _snapshot_time_text(value: Any, timezone_name: str = "UTC") -> str:
    if not isinstance(value, str) or not value:
        return "unknown"
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        display_timezone = ZoneInfo(timezone_name)
    except ValueError:
        return value
    except ZoneInfoNotFoundError as exc:
        raise AppError(f"Unknown time zone {timezone_name!r}.") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    label = timestamp.astimezone(display_timezone).tzname() or timezone_name
    return f"{timestamp.astimezone(display_timezone).strftime('%Y-%m-%d %H:%M:%S')} {label}"


def _gui_timestamp_text(value: Any, timezone_name: str) -> str:
    if not isinstance(value, str) or not value:
        return "Never"
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        display_timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    local = timestamp.astimezone(display_timezone)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {local.tzname() or timezone_name}"


def _dashboard_field_lines(label: str, value: str, width: int) -> list[str]:
    prefix = f"{label:<8}"
    content_width = max(1, width - len(prefix))
    wrapped = textwrap.wrap(
        value,
        width=content_width,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [""]
    return [prefix + wrapped[0], *(" " * len(prefix) + line for line in wrapped[1:])]


def _format_storage_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


def _localized_log_line(line: str, timezone_name: str) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})(.*)$", line)
    if match is None:
        return line
    try:
        local_timestamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f").astimezone(
            ZoneInfo(timezone_name)
        )
    except (ValueError, ZoneInfoNotFoundError):
        return line
    return f"{local_timestamp.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} {local_timestamp.tzname()}{match.group(2)}"


WATERMARK = "Made by Ariyan Kumar | Rybernt"


def _ui_put(screen: Any, row: int, column: int, text: str, width: int, attr: int = 0) -> None:
    if width <= 0:
        return
    try:
        screen.addstr(row, column, str(text)[:width], attr)
    except curses.error:
        pass


def _ui_panel(screen: Any, top: int, left: int, height: int, width: int, title: str) -> None:
    if height < 3 or width < 4:
        return
    _ui_put(screen, top, left, "+" + "-" * (width - 2) + "+", width)
    _ui_put(screen, top + height - 1, left, "+" + "-" * (width - 2) + "+", width)
    for row in range(top + 1, top + height - 1):
        _ui_put(screen, row, left, "|", 1)
        _ui_put(screen, row, left + width - 1, "|", 1)
    _ui_put(screen, top, left + 2, f" {title} ", width - 4, curses.A_BOLD)


def _ui_footer(screen: Any, hint: str = "") -> None:
    height, width = screen.getmaxyx()
    mark = WATERMARK
    left_width = max(0, width - len(mark) - 5)
    _ui_put(screen, height - 1, 2, hint, left_width)
    _ui_put(screen, height - 1, max(2, width - len(mark) - 2), mark, min(len(mark), width - 2))


def _tui(screen: Any) -> None:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_RED, -1)
    screen.timeout(400)
    result_queue: queue.Queue[Any] = queue.Queue()
    worker: threading.Thread | None = None
    storage_thread: threading.Thread | None = None
    active_control: TaskControl | None = None
    active_action: str | None = None
    notice = "Ready. The scheduler runs independently as a systemd service."
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 26 or width < 72:
            screen.addstr(0, 0, "Resize terminal to at least 72x26. Press q to quit.")
            _ui_footer(screen)
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                return
            continue
        try:
            config = load_config()
            configured = True
            config_error = None
        except AppError as exc:
            config = None
            configured = False
            config_error = str(exc)
        try:
            state = StateStore().read()
            state_error = None
        except AppError as exc:
            state = {}
            state_error = str(exc)
        storage_thread = _maybe_refresh_storage(state, config, storage_thread)
        task_running = state.get("status") == "running" or state.get("task_status") == "running"
        timezone_name = config.timezone_name if config else "UTC"
        display_timezone = ZoneInfo(timezone_name)
        current_time = datetime.now(timezone.utc).astimezone(display_timezone)
        _ui_put(screen, 0, 2, "◆ RYBERNT  /  RESTIC NODE BACKUP", width - 4, curses.color_pair(1) | curses.A_BOLD)
        clock = current_time.strftime("%a %d %b %Y  %H:%M:%S %Z")
        _ui_put(screen, 0, max(2, width - len(clock) - 3), clock, len(clock), curses.A_BOLD)
        _ui_put(screen, 1, 2, "=" * (width - 4), width - 4, curses.color_pair(1))

        status = str(state.get("status", "waiting")).upper()
        color = curses.color_pair(2) if status == "SUCCESS" else curses.color_pair(4) if status == "FAILED" else curses.color_pair(3)
        left_width = min(58, max(32, width // 2 - 4))
        gap = 2
        right_left = 2 + left_width + gap
        right_width = width - right_left - 2
        inner_width = left_width - 4
        retry_at = state.get("retry_at")
        schedule_label = "Retry at" if retry_at else "Next run"
        schedule_time = _gui_timestamp_text(
            retry_at or state.get("next_run"), timezone_name
        ) if (retry_at or state.get("next_run")) else "When scheduler starts"
        task_text = "Idle"
        if task_running or (worker is not None and worker.is_alive()):
            task_text = active_control.phase if active_control else state.get("operation") or "Running"
        storage_error = (
            state.get("storage_error")
            if config is not None and state.get("storage_repository") == config.repository
            else None
        )
        last_error = state.get("last_error") or state.get("retention_error") or storage_error or "No backup errors"
        storage_bytes = state.get("storage_bytes")
        if (
            isinstance(storage_bytes, int)
            and not isinstance(storage_bytes, bool)
            and storage_bytes >= 0
            and config is not None
            and state.get("storage_repository") == config.repository
        ):
            storage_text = f"{_format_storage_size(storage_bytes)} used"
        elif storage_error:
            storage_text = "Unavailable (see Info)"
        elif config and config.repository.startswith("s3:"):
            storage_text = "Checking S3 object usage…"
        else:
            storage_text = "Not an S3 repository"
        node_lines = [f"● {status}"]
        node_lines.extend(_dashboard_field_lines("Source", config.source if config else DEFAULT_SOURCE, inner_width))
        node_lines.extend(_dashboard_field_lines("Repo", config.repository if config else "Not configured", inner_width))
        node_lines.extend(_dashboard_field_lines("Storage", storage_text, inner_width))
        node_lines.extend(_dashboard_field_lines("Last OK", _gui_timestamp_text(state.get("last_success"), timezone_name), inner_width))
        node_lines.extend(_dashboard_field_lines(schedule_label, schedule_time, inner_width))
        node_lines.extend(_dashboard_field_lines("Task", str(task_text), inner_width))
        node_lines.extend(_dashboard_field_lines("Info", str(last_error), inner_width))
        panel_height = max(13, min(len(node_lines) + 2, height - 12))
        node_lines = node_lines[:panel_height - 2]
        _ui_panel(screen, 2, 2, panel_height, left_width, "NODE")
        _ui_panel(screen, 2, right_left, panel_height, right_width, "QUICK ACTIONS")
        for row, line in enumerate(node_lines, start=3):
            _ui_put(
                screen,
                row,
                4,
                line,
                inner_width,
                color | curses.A_BOLD if row == 3 else (
                    curses.color_pair(4) if line.startswith("Info") and (
                        state.get("last_error")
                        or state.get("retention_error")
                        or storage_error
                    )
                    else curses.A_NORMAL
                ),
            )
        if state_error:
            notice = f"State error: {state_error}"
        elif config_error:
            notice = f"Config: {config_error}"
        elif state.get("retention_error"):
            notice = f"Retention: {state['retention_error']}"

        actions = [
            ("r", "Run backup"), ("v", "Verify repo"),
            ("s", "Snapshots"), ("x", "Restore"),
            ("u", "Restore server"),
            ("p", "Retention"), ("i", "Initialize"),
            ("c", "Configure"), ("k", "Cancel task"),
            ("l", "Activity log"), ("q", "Quit"),
        ]
        action_y = 3
        col_width = max(12, (right_width - 4) // 2)
        for index, (shortcut, label) in enumerate(actions):
            row = action_y + index // 2
            column = right_left + 2 + (index % 2) * col_width
            _ui_put(screen, row, column, f"[{shortcut.upper()}] {label}", col_width - 1,
                    curses.color_pair(1) | curses.A_BOLD)

        progress_y = 2 + panel_height
        progress = ""
        if worker is not None and worker.is_alive():
            local_progress = dict(active_control.progress) if active_control else {}
            progress = _progress_text(local_progress) or _progress_text(state.get("progress", {}))
            detail = (active_control.detail if active_control else "") or state.get("active_detail", "")
            current_files = local_progress.get("current_files", [])
            if not progress:
                progress = str(detail)
            if isinstance(current_files, list) and current_files:
                progress += f" · {', '.join(str(path) for path in current_files[:2])}"
        elif worker is not None:
            progress = "Task finished; collecting result…"
        elif task_running:
            progress = _progress_text(state.get("progress", {})) or str(state.get("active_detail") or "")
            current_files = state.get("progress", {}).get("current_files", [])
            if isinstance(current_files, list) and current_files:
                progress += f" · {', '.join(str(path) for path in current_files[:2])}"
        _ui_put(screen, progress_y, 3, f"PROGRESS  {progress or 'Idle'}", width - 6, curses.color_pair(3) if task_running else curses.A_NORMAL)
        _ui_put(screen, progress_y + 1, 2, "-" * (width - 4), width - 4, curses.color_pair(1))

        activity_top = progress_y + 2
        activity_height = max(4, height - activity_top - 4)
        _ui_panel(screen, activity_top, 2, activity_height, width - 4, f"ACTIVITY  {LOG_PATH}")
        log_rows = max(1, activity_height - 2)
        try:
            recent = LOG_PATH.read_text(encoding="utf-8").splitlines()[-log_rows:]
        except FileNotFoundError:
            recent = []
        for index, line in enumerate(recent, start=activity_top + 1):
            _ui_put(screen, index, 4, _localized_log_line(line, timezone_name), width - 8)
        _ui_put(screen, height - 2, 2, notice, width - 4, curses.A_BOLD)
        _ui_footer(screen, "R backup · S snapshots · X restore · U server restore · C configure · Q quit")
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q")):
            if worker is not None and worker.is_alive() and active_action != "run":
                try:
                    confirmation = _tui_prompt(
                        screen,
                        "Cancel this dashboard task and quit? Type YES (other backups keep running)",
                        "",
                    )
                except AppError:
                    continue
                if confirmation != "YES":
                    continue
                if active_control is not None:
                    active_control.cancel()
                worker.join()
            return
        if key in (ord("l"), ord("L")):
            _activity_log_viewer(screen, timezone_name)
            continue
        if key in (ord("k"), ord("K")):
            if worker is not None and worker.is_alive():
                active_control.cancel()
                if active_action == "run":
                    StateStore().update(cancel_requested=True)
                notice = "Cancellation requested; waiting for Restic to stop safely."
            elif task_running:
                StateStore().update(cancel_requested=True)
                notice = "Cancellation requested for the scheduler's running backup."
            else:
                notice = "There is no running task to cancel."
            continue
        if key in (ord("c"), ord("C")):
            try:
                notice = _configure_tui(screen)
            except AppError as exc:
                notice = str(exc)
        elif key in (
            ord("r"), ord("R"), ord("v"), ord("V"), ord("s"), ord("S"),
            ord("p"), ord("P"), ord("i"), ord("I"), ord("x"), ord("X"),
            ord("u"), ord("U"),
        ):
            if worker is not None or task_running:
                notice = "Wait for the current task to finish."
                continue
            if key in (ord("r"), ord("R")):
                action = "run"
            elif key in (ord("v"), ord("V")):
                action = "verify"
            elif key in (ord("s"), ord("S")):
                action = "snapshots"
            elif key in (ord("x"), ord("X")):
                action = "select_restore"
            elif key in (ord("u"), ord("U")):
                action = "select_server_restore"
            elif key in (ord("p"), ord("P")):
                action = "prune"
            elif key in (ord("i"), ord("I")):
                action = "init"
            if action != "run" and not configured:
                notice = "Configure the application first (press c)."
                continue
            action_args: tuple[str, ...] = ()
            if action == "select_server_restore":
                server_uuid = _prompt_server_uuid(screen)
                if server_uuid is None:
                    notice = "Server restore cancelled."
                    continue
                action_args = ("", "", server_uuid)
            if action == "prune":
                try:
                    confirmation = _tui_prompt(screen, "Type PRUNE to apply retention", "")
                except AppError as exc:
                    notice = str(exc)
                    continue
                if confirmation != "PRUNE":
                    notice = "Prune cancelled."
                    continue
            notice = f"Starting {action}…"
            active_control = TaskControl()
            active_action = action
            worker = threading.Thread(
                target=_do_tui_action,
                args=(action, result_queue, active_control, *action_args),
            )
            worker.start()
        try:
            result_item = result_queue.get_nowait()
            worker = None
            active_action = None
            result_type, result = result_item[:2]
            if result_type in ("snapshots", "server_snapshots"):
                action = result_item[2] if result_type == "snapshots" else "select_server_restore"
                server_uuid = result_item[2] if result_type == "server_snapshots" else ""
                selected = _snapshot_browser(
                    screen,
                    result,
                    allow_restore=True,
                    timezone_name=config.timezone_name if config else "UTC",
                    heading=(
                        f"SERVER BACKUPS · {server_uuid}"
                        if server_uuid
                        else "SNAPSHOTS"
                    ),
                    server_uuid=server_uuid,
                )
                if selected is None:
                    notice = "Snapshot selection cancelled."
                else:
                    selected, selection_action = selected
                    if selection_action != "restore":
                        if selection_action != "restore_server":
                            notice = "Snapshot selected."
                            worker = None
                            continue
                    snapshot_id = str(selected.get("id", ""))
                    if selection_action == "restore_server" or action == "select_server_restore":
                        if not server_uuid:
                            server_uuid = _prompt_server_uuid(screen)
                        if server_uuid is None:
                            notice = "Server restore cancelled."
                            worker = None
                            continue
                        if not _confirm_server_restore(screen, snapshot_id, server_uuid):
                            notice = "Server restore cancelled."
                            worker = None
                            continue
                        active_control = TaskControl()
                        worker = threading.Thread(
                            target=_do_tui_action,
                            args=(
                                "restore_server",
                                result_queue,
                                active_control,
                                snapshot_id,
                                "",
                                server_uuid,
                            ),
                        )
                        active_action = "restore_server"
                        notice = f"Restoring server {server_uuid}…"
                        worker.start()
                        continue
                    restore_target = _prompt_restore_target(
                        screen, "/srv/restore", config
                    ) if config else None
                    if restore_target is None:
                        notice = "Restore cancelled."
                        worker = None
                        continue
                    target_path, create_target = restore_target
                    if not _confirm_restore(screen, snapshot_id, target_path, create_target):
                        notice = "Restore cancelled."
                        worker = None
                        continue
                    active_control = TaskControl()
                    worker = threading.Thread(
                        target=_do_tui_action,
                        args=("restore", result_queue, active_control, snapshot_id, str(target_path)),
                    )
                    active_action = "restore"
                    notice = f"Restoring snapshot {snapshot_id[:12]}…"
                    worker.start()
            else:
                notice = result
                if "\n" in notice:
                    _show_output(screen, notice)
        except queue.Empty:
            pass


def run_tui() -> None:
    logger = create_logger()
    stream_handlers = [
        handler for handler in logger.handlers
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
    ]
    for handler in stream_handlers:
        logger.removeHandler(handler)
    try:
        curses.wrapper(_tui)
    except curses.error as exc:
        raise AppError(f"Cannot start terminal UI: {exc}") from exc
    finally:
        for handler in stream_handlers:
            logger.addHandler(handler)


def status_command() -> None:
    state = StateStore().read()
    config = load_config()
    print(f"Source:        {config.source}")
    print(f"Repository:    {config.repository}")
    print(f"Schedule:      every {config.interval_hours} hour(s)")
    print(f"Status:        {state.get('status', 'waiting')}")
    print(f"Last success:  {state.get('last_success', 'Never')}")
    print(f"Next run:      {state.get('next_run', 'When scheduler starts')}")
    if state.get("last_error"):
        print(f"Last error:    {state['last_error']}")
    if state.get("retention_error"):
        print(f"Retention:     {state['retention_error']}")


def check_source() -> None:
    config = load_config()
    source = Path(config.source)
    if not source.is_dir():
        raise AppError(
            f"Backup source {source} does not exist or is not a directory. "
            "Re-run configure and enter the correct source path."
        )
    print(f"Backup source is ready: {source}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="restic-node-backup",
        description="Reliable Restic backups and terminal management for game nodes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("tui", help="open the interactive terminal dashboard")
    subparsers.add_parser("backup", help="open the interactive backup dashboard")
    subparsers.add_parser("configure", help="configure source, S3 credentials and schedule")
    subparsers.add_parser("daemon", help="run the persistent, retrying scheduler")
    subparsers.add_parser("run", help="start an immediate backup that survives terminal closure")
    subparsers.add_parser("run-now", help=argparse.SUPPRESS)
    subparsers.add_parser("init", help="initialize a new Restic repository")
    subparsers.add_parser("ensure-repo", help="initialize the repository only if it is new")
    subparsers.add_parser("snapshots", help="list snapshots as JSON")
    check = subparsers.add_parser("verify", help="check repository indexes and packs")
    check.add_argument("--read-data", action="store_true", help="read all repository data (slow)")
    restore = subparsers.add_parser("restore", help="restore a snapshot to a separate directory")
    restore.add_argument("--snapshot", required=True)
    restore.add_argument("--target", required=True)
    subparsers.add_parser("prune", help="apply keep-last retention and prune unused data")
    subparsers.add_parser("status", help="show scheduler and backup status")
    subparsers.add_parser("check-source", help="confirm the configured source directory exists")
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            configure_cli()
        elif args.command in ("tui", "backup"):
            run_tui()
        elif args.command == "daemon":
            run_scheduler()
        elif args.command == "run":
            print(_start_manual_backup())
        elif args.command == "run-now":
            return 0 if run_backup() else 1
        elif args.command == "init":
            print(initialize_repository())
        elif args.command == "ensure-repo":
            print(ensure_repository())
        elif args.command == "snapshots":
            print(json.dumps(restic_snapshots(), indent=2))
        elif args.command == "verify":
            print(verify_repository(args.read_data))
        elif args.command == "restore":
            print(restore_snapshot(args.snapshot, args.target))
        elif args.command == "prune":
            print(prune_repository())
        elif args.command == "status":
            status_command()
        elif args.command == "check-source":
            check_source()
        return 0
    except (AppError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
