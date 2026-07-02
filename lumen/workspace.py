"""Filesystem sandbox for Lumen's tools.

Three zones, enforced centrally so individual tools can't get path handling wrong:

* **Project zone** (``output_dir``, default ``~/Lumen``) — the selected project
  folder. Core filesystem tools, generated artifacts, and shell commands work
  inside this folder by default.
* **System roots** — always off-limits (``/System``, ``/etc``, …).

Paths outside the selected project folder are rejected with an approval-required
error so the UI can keep the user in the loop before anything escapes the project.
"""

from __future__ import annotations

from pathlib import Path

from .logging_setup import get_logger

logger = get_logger(__name__)

# Directory names under $HOME that tools must never touch.
_DENIED_HOME_SUBDIRS = {
    "Library",
    ".ssh",
    ".aws",
    ".gnupg",
    ".config",
    ".lumen",
    ".kube",
    ".docker",
}

# Absolute system roots that are always off-limits.
_DENIED_ROOTS = ("/System", "/Library", "/private/etc", "/etc", "/usr", "/bin", "/sbin")


class WorkspaceError(ValueError):
    """Raised when a path is outside the permitted sandbox."""


class ApprovalRequired(WorkspaceError):
    """Raised when an operation leaves the selected project folder."""


class WorkspaceManager:
    """Resolves and validates paths against the Lumen sandbox."""

    def __init__(self) -> None:
        self._output_dir: Path | None = None
        self._home = Path.home().resolve()

    def configure(self, output_dir: Path | str) -> None:
        """Set (and create) the selected project directory. Idempotent."""
        path = self.validate_project_root(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        self._output_dir = path
        logger.info("Workspace project directory: %s", path)

    def validate_project_root(self, path: Path | str) -> Path:
        """Resolve and validate a candidate project root."""
        resolved = Path(path).expanduser().resolve()
        self._assert_safe_project_root(resolved)
        return resolved

    @property
    def output_dir(self) -> Path:
        if self._output_dir is None:
            self.configure(Path.home() / "Lumen")
        assert self._output_dir is not None
        return self._output_dir

    @property
    def home(self) -> Path:
        return self._home

    # -- project zone (writes) -----------------------------------------------

    def resolve_output(self, name: str) -> Path:
        """Resolve a safe path *inside* the output dir for a generated file.

        ``name`` may include subfolders (e.g. ``"charts/sales.png"``) but may not
        escape the output directory via ``..`` or absolute paths.
        """
        candidate = (self.output_dir / name).resolve()
        if not self._is_within(candidate, self.output_dir):
            raise ApprovalRequired(
                f"Writing outside the selected project folder requires user approval: {name!r}"
            )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def unique_output(self, name: str) -> Path:
        """Like :meth:`resolve_output` but never overwrites — appends ``-2`` etc."""
        target = self.resolve_output(name)
        if not target.exists():
            return target
        stem, suffix, parent = target.stem, target.suffix, target.parent
        i = 2
        while True:
            candidate = parent / f"{stem}-{i}{suffix}"
            if not candidate.exists():
                return candidate
            i += 1

    # -- read / organise zone -----------------------------------------------

    def resolve_read(self, path: str) -> Path:
        """Resolve a user-supplied path for reading/listing, enforcing the sandbox."""
        candidate = self._expand(path)
        self._assert_allowed(candidate)
        return candidate

    def resolve_dir(self, path: str) -> Path:
        """Resolve a path that must be an existing directory inside the sandbox."""
        candidate = self.resolve_read(path)
        if not candidate.exists():
            raise WorkspaceError(f"Directory does not exist: {self.display(candidate)}")
        if not candidate.is_dir():
            raise WorkspaceError(f"Not a directory: {self.display(candidate)}")
        return candidate

    def resolve_write(self, path: str, *, create_parents: bool = True) -> Path:
        """Resolve a path for writing inside the selected project folder."""
        candidate = self._expand(path)
        self._assert_allowed(candidate)
        if create_parents:
            candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    # -- helpers -------------------------------------------------------------

    def display(self, path: Path) -> str:
        """Render a path relative to home (``~/Desktop/x``) for human-friendly output."""
        try:
            return "~/" + str(path.resolve().relative_to(self._home))
        except ValueError:
            return str(path)

    def _expand(self, path: str, *, relative_to_output: bool = True) -> Path:
        text = path.strip()
        p = Path(text).expanduser()
        if not p.is_absolute():
            p = self.output_dir / p
        return p.resolve()

    def _assert_allowed(self, path: Path) -> None:
        self._assert_safe_home_path(path)
        if not self._is_within(path, self.output_dir):
            raise ApprovalRequired(
                "Access outside the selected project folder requires user approval: "
                f"{self.display(path)}"
            )

    def _assert_safe_project_root(self, path: Path) -> None:
        self._assert_safe_home_path(path)

    def _assert_safe_home_path(self, path: Path) -> None:
        s = str(path)
        for root in _DENIED_ROOTS:
            if s == root or s.startswith(root + "/"):
                raise WorkspaceError(f"Access to system path is not allowed: {s}")
        # Must be within the user's home tree.
        if not self._is_within(path, self._home):
            raise WorkspaceError(
                f"Path is outside your home directory and not allowed: {s}"
            )
        # Block sensitive subdirectories of home.
        try:
            rel_parts = path.resolve().relative_to(self._home).parts
        except ValueError:
            rel_parts = ()
        if rel_parts and rel_parts[0] in _DENIED_HOME_SUBDIRS:
            raise WorkspaceError(f"Access to {self.display(path)} is not allowed.")

    @staticmethod
    def _is_within(child: Path, parent: Path) -> bool:
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False


# Process-wide singleton, configured at startup from AppConfig.
workspace = WorkspaceManager()

__all__ = ["workspace", "WorkspaceManager", "WorkspaceError", "ApprovalRequired"]
