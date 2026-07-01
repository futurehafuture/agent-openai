"""Shell execution tool — persistent-style bash within the home sandbox.

Inspired by Claude Code's Bash tool: use for git, package managers, builds, and
anything without a dedicated tool. Prefer ``read_file`` / ``grep_files`` /
``glob_files`` for file operations.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ..logging_setup import get_logger
from ..workspace import workspace
from .registry import register_tool

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 120
_MAX_OUTPUT = 32_000

# Commands that must never run regardless of path.
_BLOCKED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsudo\b"), "sudo is not allowed"),
    (re.compile(r"\brm\s+-rf\s+/(?:\s|$)"), "recursive delete of filesystem root"),
    (re.compile(r"\b(mkfs|diskutil\s+erase|dd\s+if=)\b"), "disk formatting/wiping"),
    (re.compile(r"\bchmod\s+[0-7]*777\b"), "world-writable permissions"),
    (re.compile(r"\b(curl|wget)\s+.*\|\s*(ba)?sh\b"), "piping remote scripts to shell"),
]

# Read-only commands that skip extra path checks when used alone.
_READ_ONLY_PREFIXES = (
    "ls",
    "cat",
    "head",
    "tail",
    "pwd",
    "echo",
    "which",
    "wc",
    "file",
    "stat",
    "du",
    "df",
    "git status",
    "git log",
    "git diff",
    "git branch",
    "git show",
    "python --version",
    "python3 --version",
    "node --version",
    "npm --version",
    "uv --version",
)


def _validate_command(command: str) -> None:
    lowered = command.lower().strip()
    for pattern, reason in _BLOCKED_PATTERNS:
        if pattern.search(lowered):
            raise ValueError(f"Blocked command ({reason}).")


def _pick_cwd(cwd: str | None) -> Path:
    if cwd:
        path = workspace.resolve_read(cwd)
        if not path.is_dir():
            raise ValueError(f"Working directory is not a directory: {workspace.display(path)}")
        return path
    return workspace.output_dir


def run_command(command: str, cwd: str | None = None, timeout_seconds: int = _DEFAULT_TIMEOUT) -> str:
    """Run a shell command in the user's environment.

    The working directory defaults to the Lumen workspace folder. Commands run
    with the app's environment (including ``PATH``). Output is truncated at
    ~32 KB.

    Do **not** use this for reading or searching files — use ``read_file``,
    ``grep_files``, or ``glob_files`` instead.

    Args:
        command: Shell command to execute (single string, may use pipes).
        cwd: Working directory (default: workspace folder ``~/Lumen``).
        timeout_seconds: Max seconds before the command is killed (default 120).
    """
    command = command.strip()
    if not command:
        raise ValueError("command must not be empty.")
    _validate_command(command)

    workdir = _pick_cwd(cwd)
    timeout = max(1, min(int(timeout_seconds), 600))
    env = os.environ.copy()

    logger.info("Shell: %s (cwd=%s)", command[:120], workspace.display(workdir))
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"⚠️ Command timed out after {timeout}s:\n```\n{command}\n```"

    parts: list[str] = [
        f"**exit {proc.returncode}** · cwd `{workspace.display(workdir)}`",
        f"```bash\n{command}\n```",
    ]
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stdout:
        parts.append("**stdout**\n```\n" + _truncate(stdout) + "\n```")
    if stderr:
        parts.append("**stderr**\n```\n" + _truncate(stderr) + "\n```")
    if not stdout and not stderr:
        parts.append("_(no output)_")
    return "\n\n".join(parts)


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n… [truncated, {len(text):,} chars total]"


register_tool(run_command, category="shell")
