"""Thin wrapper around the systemd user unit that runs the engine."""

from __future__ import annotations

import subprocess

UNIT = "skydimo.service"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True, timeout=20,
    )


def _ok(*args: str) -> bool:
    try:
        return _run(*args).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def exists() -> bool:
    try:
        out = _run("cat", UNIT)
        return out.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def is_active() -> bool:
    return _ok("is-active", "--quiet", UNIT)


def is_enabled() -> bool:
    return _ok("is-enabled", "--quiet", UNIT)


def start() -> bool:
    return _ok("start", UNIT)


def stop() -> bool:
    return _ok("stop", UNIT)


def restart() -> bool:
    return _ok("restart", UNIT)


def enable() -> bool:
    return _ok("enable", UNIT)


def disable() -> bool:
    return _ok("disable", UNIT)


def status_line() -> str:
    if not exists():
        return "not installed"
    bits = ["running" if is_active() else "stopped"]
    if is_enabled():
        bits.append("starts at login")
    return ", ".join(bits)


def apply_change() -> None:
    """Restart the engine if it is running, so structural edits take effect."""
    if is_active():
        restart()
