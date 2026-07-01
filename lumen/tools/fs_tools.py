"""Core filesystem tools — Claude Code-style Read / Write / Edit / Glob / Grep.

Prefer these dedicated tools over ``run_command`` for file operations. All paths
are sandboxed to the user's home directory (sensitive folders blocked).
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from ..logging_setup import get_logger
from ..workspace import workspace
from ._format import human_bytes, md_table
from .registry import register_tool

logger = get_logger(__name__)

_MAX_READ_LINES = 2000
_MAX_READ_BYTES = 512_000
_MAX_GREP_MATCHES = 100
_MAX_GLOB_RESULTS = 200


def _numbered_lines(text: str, start: int = 1) -> str:
    lines = text.splitlines()
    width = len(str(start + len(lines) - 1))
    return "\n".join(f"{i:>{width}}|{line}" for i, line in enumerate(lines, start=start))


def read_file(path: str, offset: int = 1, limit: int | None = None) -> str:
    """Read a text file with line numbers (like ``cat -n``).

    Use this instead of shell ``cat/head/tail``. For images or PDFs, use skill
    tools or ask the user to open them manually.

    Args:
        path: File path (``~/Desktop/note.txt`` or absolute).
        offset: 1-based line number to start from.
        limit: Maximum lines to return (default 500).
    """
    resolved = workspace.resolve_read(path)
    if not resolved.exists():
        raise FileNotFoundError(workspace.display(resolved))
    if not resolved.is_file():
        raise ValueError(f"Not a file: {workspace.display(resolved)}")

    raw = resolved.read_bytes()
    if len(raw) > _MAX_READ_BYTES:
        raise ValueError(
            f"File too large ({human_bytes(len(raw))}). "
            "Use offset/limit or grep_files to search inside it."
        )

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, offset)
    end = start + (limit if limit is not None else 500) - 1
    chunk = lines[start - 1 : end]
    numbered = _numbered_lines("\n".join(chunk), start=start)
    total = len(lines)
    header = f"**{workspace.display(resolved)}** — {total:,} lines\n\n"
    if end < total:
        header += f"_Showing lines {start}–{min(end, total)} of {total}. Use offset={end + 1} for more._\n\n"
    return header + numbered


def write_file(path: str, content: str) -> str:
    """Create or overwrite a file inside the home sandbox.

    Prefer ``edit_file`` for small changes to an existing file.

    Args:
        path: Destination path. Parent directories are created automatically.
        content: Full file contents to write.
    """
    target = workspace.resolve_write(path)
    existed = target.exists()
    target.write_text(content, encoding="utf-8")
    nbytes = target.stat().st_size
    action = "Updated" if existed else "Created"
    return f"{action} **{workspace.display(target)}** ({human_bytes(nbytes)}, {content.count(chr(10)) + 1} lines)"


def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Apply an exact search-and-replace edit to a text file.

    ``old_string`` must match exactly (including whitespace). Use ``read_file``
    first to copy the precise text to change.

    Args:
        path: File to edit.
        old_string: Exact text to find.
        new_string: Replacement text.
        replace_all: Replace every occurrence; default is first only.
    """
    if not old_string:
        raise ValueError("old_string must not be empty.")
    target = workspace.resolve_read(path)
    if not target.is_file():
        raise FileNotFoundError(workspace.display(target))
    text = target.read_text(encoding="utf-8")
    count = text.count(old_string)
    if count == 0:
        raise ValueError(
            f"old_string not found in {workspace.display(target)}. "
            "Read the file first and copy the exact snippet."
        )
    if replace_all:
        updated = text.replace(old_string, new_string)
        replaced = count
    else:
        updated = text.replace(old_string, new_string, 1)
        replaced = 1
    target.write_text(updated, encoding="utf-8")
    return f"Edited **{workspace.display(target)}** — {replaced} replacement(s)."


def glob_files(pattern: str, path: str = "~") -> str:
    """Find files by glob pattern (e.g. ``**/*.py``, ``*.csv``).

    Use this instead of shell ``find`` or ``ls``. Patterns follow Python
    ``pathlib`` rules relative to ``path``.

    Args:
        pattern: Glob pattern such as ``**/*.md`` or ``src/*.ts``.
        path: Directory to search (default home).
    """
    root = workspace.resolve_read(path)
    if not root.is_dir():
        root = root.parent
    matches = sorted(root.glob(pattern))[: _MAX_GLOB_RESULTS + 1]
    truncated = len(matches) > _MAX_GLOB_RESULTS
    matches = matches[:_MAX_GLOB_RESULTS]
    if not matches:
        return f"No files matching `{pattern}` under {workspace.display(root)}."
    rows = [[workspace.display(p), human_bytes(p.stat().st_size) if p.is_file() else "—"] for p in matches]
    note = f"\n\n_Showing first {_MAX_GLOB_RESULTS} matches._" if truncated else ""
    return (
        f"**{len(matches)} match(es)** for `{pattern}` under {workspace.display(root)}\n\n"
        + md_table(["path", "size"], rows)
        + note
    )


def grep_files(pattern: str, path: str = "~", glob_pattern: str = "*") -> str:
    """Search file contents with a regex (like ``rg``).

    Use this instead of shell ``grep``. Binary files are skipped.

    Args:
        pattern: Regular expression to search for.
        path: File or directory to search.
        glob_pattern: When searching a directory, limit to matching filenames.
    """
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc

    base = workspace.resolve_read(path)
    files: list[Path]
    if base.is_file():
        files = [base]
    else:
        files = [p for p in base.rglob("*") if p.is_file() and fnmatch.fnmatch(p.name, glob_pattern)]

    hits: list[list[str]] = []
    for fp in sorted(files):
        if len(hits) >= _MAX_GREP_MATCHES:
            break
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                hits.append([workspace.display(fp), str(lineno), line.strip()[:120]])
                if len(hits) >= _MAX_GREP_MATCHES:
                    break

    if not hits:
        return f"No matches for `{pattern}` under {workspace.display(base)}."
    note = f"\n\n_Showing first {_MAX_GREP_MATCHES} matches._" if len(hits) >= _MAX_GREP_MATCHES else ""
    return f"**{len(hits)} match(es)** for `{pattern}`\n\n" + md_table(["file", "line", "text"], hits) + note


def list_directory(path: str, show_hidden: bool = False) -> str:
    """List a directory with sizes and types.

    Args:
        path: Directory to list (e.g. ``~/Desktop``).
        show_hidden: Include dot-files.
    """
    directory = workspace.resolve_dir(path)
    folders, files = [], []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(".") and not show_hidden:
            continue
        (folders if entry.is_dir() else files).append(entry)

    rows = [["📁 " + f.name + "/", "—"] for f in folders]
    rows += [
        [f.name, human_bytes(f.stat().st_size)]
        for f in sorted(files, key=lambda p: p.stat().st_size, reverse=True)
    ]
    total = sum(f.stat().st_size for f in files)
    header = (
        f"**{workspace.display(directory)}** — {len(folders)} folders, "
        f"{len(files)} files, {human_bytes(total)} total\n\n"
    )
    return header + md_table(["name", "size"], rows)


for _fn in (read_file, write_file, edit_file, glob_files, grep_files, list_directory):
    register_tool(_fn, category="fs")
