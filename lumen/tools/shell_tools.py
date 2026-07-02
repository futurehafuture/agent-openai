"""Shell execution tool — persistent-style bash within the selected project folder.

Inspired by Claude Code's Bash tool: use for git, package managers, builds, and
anything without a dedicated tool. Prefer ``read_file`` / ``grep_files`` /
``glob_files`` for file operations.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from ..logging_setup import get_logger
from ..workspace import workspace
from .registry import register_tool

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 120
_MAX_OUTPUT = 32_000

# Commands that must never run regardless of path.
_BLOCKED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsudo\b"), "sudo is not allowed"),
    # Block rm -rf / -r on root, home, or any absolute path.
    (re.compile(r"\brm\s+-r[f]?\s+(?:~|/|/[A-Za-z])"), "recursive force-delete of important paths"),
    (re.compile(r"\brm\s+-rf?\s+\.(?:\s|$|/)"), "recursive delete of current directory"),
    (re.compile(r"\b(mkfs|diskutil\s+erase\w*|dd\s+if=)"), "disk formatting/wiping"),
    (re.compile(r"\bchmod\s+[0-7]*777\b"), "world-writable permissions"),
    (re.compile(r"\b(curl|wget)\s+.*\|\s*(ba)?sh\b"), "piping remote scripts to shell"),
    (re.compile(r":\(\s*\)\s*\{"), "fork bomb"),
    (re.compile(r">\s*/dev/(null|zero|sda|disk)"), "device truncation/overwrite"),
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
    _validate_command_paths(command)


def _validate_command_paths(command: str) -> None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for token in tokens:
        for candidate in _path_candidates(token):
            workspace.resolve_read(candidate)


def _path_candidates(token: str) -> list[str]:
    if not token or token.startswith("-"):
        return []
    if _looks_like_url(token):
        return []

    text = token
    if "=" in text and not text.startswith(("=", "==")):
        text = text.rsplit("=", 1)[-1]
    text = text.strip("\"'")
    if not text or _looks_like_url(text):
        return []

    if text.startswith(("/", "~", "../", "..")):
        return [text]
    if "/.." in text:
        return [text]
    return []


def _looks_like_url(text: str) -> bool:
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https", "git", "ssh"} and bool(parsed.netloc)


def _pick_cwd(cwd: str | None) -> Path:
    if cwd:
        path = workspace.resolve_read(cwd)
        if not path.is_dir():
            raise ValueError(f"Working directory is not a directory: {workspace.display(path)}")
        return path
    return workspace.output_dir


def run_command(command: str, cwd: str | None = None, timeout_seconds: int = _DEFAULT_TIMEOUT) -> str:
    """Run a shell command in the user's environment.

    The working directory defaults to the selected project folder. Commands run
    with the app's environment (including ``PATH``). Output is truncated at
    ~32 KB.

    Do **not** use this for reading or searching files — use ``read_file``,
    ``grep_files``, or ``glob_files`` instead.

    Args:
        command: Shell command to execute (single string, may use pipes).
        cwd: Working directory (default: selected project folder).
        timeout_seconds: Max seconds before the command is killed (default 120).
    """
    command = command.strip()
    if not command:
        raise ValueError("command must not be empty.")
    _validate_command(command)

    workdir = _pick_cwd(cwd)
    timeout = max(1, min(int(timeout_seconds), 600))
    env = os.environ.copy()

    if re.search(r"\brm\b", command):
        logger.warning("Shell rm command: %s (cwd=%s)", command[:120], workspace.display(workdir))
    else:
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
