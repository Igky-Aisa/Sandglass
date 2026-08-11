"""Self-update: work out how Sandglass got onto this machine, then update it
the way that install actually wants to be updated.

There is no single right answer, because "update" means two different things:

- **Editable / clone install** (`pip install -e`, `pipx install -e`) — the
  installed package *is* the working tree. Reinstalling would do nothing; the
  update is `git pull`. This is also the shape where an update can destroy
  work, so it refuses to touch a dirty tree.
- **Copy install** (`pipx install git+…`, `pip install git+…`) — the package is
  a snapshot. `git pull` is meaningless; the update is a reinstall from the
  remote, and which command does that depends on the installer.

⚠️ **Never resolve `sandglass` from PyPI.** The name is taken on PyPI by an
unrelated project (0.0.1 at time of writing) and this tool has never been
published there. A plain `pip install --upgrade sandglass` would silently
replace this tool with a stranger's package, so every install path here is
pinned to the git remote and the bare package name is never used.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Where updates come from when there is no local clone to pull. Kept here
# rather than read from the installed metadata because a copy install may have
# come from a zip or a path that no longer exists.
DEFAULT_REPO_URL = "https://github.com/Igky-Aisa/Sandglass.git"
DEFAULT_BRANCH = "main"
# The package lives in a subdirectory of the repo, which pip needs told.
PACKAGE_SUBDIR = "sandglass-cli"

GIT_TIMEOUT = 120
INSTALL_TIMEOUT = 600


class UpdateError(RuntimeError):
    """A self-update could not be completed safely."""


@dataclass
class Install:
    """How Sandglass is installed on this machine."""

    package_dir: str          # .../sandglass
    editable: bool
    repo_dir: Optional[str]   # git worktree root, if the source is a clone
    installer: str            # "pipx" | "uv" | "pip"
    interpreter: str          # python that owns the install

    @property
    def can_pull(self) -> bool:
        """Whether updating means `git pull` rather than a reinstall."""
        return bool(self.repo_dir) and self.editable

    def describe(self) -> str:
        kind = "editable clone" if self.can_pull else f"{self.installer} copy"
        where = self.repo_dir or self.package_dir
        return f"{kind} at {where}"


def _run(args: list[str], cwd: Optional[str] = None, timeout: int = GIT_TIMEOUT):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _git_root(path: str) -> Optional[str]:
    try:
        result = _run(["git", "-C", path, "rev-parse", "--show-toplevel"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _dist_info_editable(package_dir: str) -> Optional[bool]:
    """Read ``direct_url.json`` next to the package for the editable flag.

    Returns ``None`` when the metadata isn't there — a missing answer, which
    the caller must not read as "not editable".
    """
    site_dir = os.path.dirname(package_dir)
    try:
        entries = os.listdir(site_dir)
    except OSError:
        return None
    for entry in entries:
        if not (entry.startswith("sandglass-") and entry.endswith(".dist-info")):
            continue
        direct_url = os.path.join(site_dir, entry, "direct_url.json")
        try:
            with open(direct_url, "r", encoding="utf-8") as fh:
                return bool(json.load(fh).get("dir_info", {}).get("editable"))
        except (OSError, ValueError):
            return None
    return None


def detect_install(package_file: Optional[str] = None) -> Install:
    """Work out how this copy of Sandglass is installed."""
    package_dir = os.path.dirname(os.path.abspath(package_file or __file__))
    interpreter = sys.executable or "python"

    lowered = interpreter.replace("\\", "/").lower()
    if "/pipx/" in lowered:
        installer = "pipx"
    elif "/uv/tools/" in lowered:
        installer = "uv"
    else:
        installer = "pip"

    repo_dir = _git_root(package_dir)
    editable = _dist_info_editable(package_dir)
    if editable is None:
        # No metadata to consult. A package sitting inside a git worktree is an
        # editable/clone install in every practical case -- a copy install puts
        # files in site-packages, which is not a checkout of anything.
        editable = repo_dir is not None

    return Install(
        package_dir=package_dir,
        editable=editable,
        repo_dir=repo_dir,
        installer=installer,
        interpreter=interpreter,
    )


@dataclass
class UpdateStatus:
    """What an update would do, without doing it."""

    install: Install
    behind: int = 0
    ahead: int = 0
    dirty_files: int = 0
    current: str = ""
    latest: str = ""
    branch: str = DEFAULT_BRANCH
    detail: str = ""

    @property
    def up_to_date(self) -> bool:
        return self.behind == 0


def check(install: Optional[Install] = None) -> UpdateStatus:
    """Report whether an update is available. Never changes anything.

    For a clone this is exact: fetch, then count commits. For a copy install
    there is no local history to compare against, so it reports honestly that
    it cannot tell rather than inventing a comparison.
    """
    install = install or detect_install()
    status = UpdateStatus(install=install)

    if not install.repo_dir:
        status.detail = (
            "Installed as a copy, with no local clone to compare against — "
            "Sandglass can't tell whether the remote is newer. Updating "
            "reinstalls from the remote regardless."
        )
        return status

    repo = install.repo_dir
    fetch = _run(["git", "-C", repo, "fetch", "--quiet", "origin", DEFAULT_BRANCH])
    if fetch.returncode != 0:
        raise UpdateError(
            f"Could not reach the remote: {fetch.stderr.strip() or 'git fetch failed'}"
        )

    status.branch = (
        _run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        or DEFAULT_BRANCH
    )
    status.current = _run(["git", "-C", repo, "rev-parse", "--short", "HEAD"]).stdout.strip()
    status.latest = _run(
        ["git", "-C", repo, "rev-parse", "--short", f"origin/{DEFAULT_BRANCH}"]
    ).stdout.strip()

    counts = _run(
        ["git", "-C", repo, "rev-list", "--left-right", "--count",
         f"HEAD...origin/{DEFAULT_BRANCH}"]
    ).stdout.split()
    if len(counts) == 2:
        status.ahead, status.behind = int(counts[0]), int(counts[1])

    porcelain = _run(["git", "-C", repo, "status", "--porcelain"]).stdout.strip()
    status.dirty_files = len(porcelain.splitlines()) if porcelain else 0
    return status


def pending_commits(install: Install, limit: int = 10) -> list[str]:
    """One-line summaries of what an update would bring in."""
    if not install.repo_dir:
        return []
    out = _run(
        ["git", "-C", install.repo_dir, "log", "--oneline", f"-{limit}",
         f"HEAD..origin/{DEFAULT_BRANCH}"]
    ).stdout.strip()
    return out.splitlines() if out else []


def update(install: Optional[Install] = None, *, force: bool = False) -> str:
    """Bring this machine's Sandglass up to date. Returns what was done."""
    install = install or detect_install()

    if install.can_pull:
        return _update_clone(install, force=force)
    return _reinstall(install)


def _update_clone(install: Install, *, force: bool) -> str:
    repo = install.repo_dir
    assert repo is not None

    dirty = _run(["git", "-C", repo, "status", "--porcelain"]).stdout.strip()
    if dirty and not force:
        # Refusing is the whole point. This is a developer's working tree, and
        # a pull over uncommitted work is how someone loses an afternoon --
        # `--force` only skips the check, it never discards anything.
        raise UpdateError(
            f"{len(dirty.splitlines())} uncommitted change(s) in {repo}. "
            "Commit or stash them first — Sandglass will not pull over "
            "uncommitted work. Re-run with --force to attempt the pull anyway "
            "(it still won't discard anything; git will refuse on conflict)."
        )

    result = _run(["git", "-C", repo, "pull", "--ff-only", "origin", DEFAULT_BRANCH])
    if result.returncode != 0:
        raise UpdateError(
            "git pull failed: "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}\n"
            "A fast-forward wasn't possible — this clone has diverged from the "
            "remote. Reconcile it by hand; Sandglass won't rewrite your history."
        )
    return (
        f"Pulled into {repo}.\n"
        "This is an editable install, so the new code is already live — "
        "nothing to reinstall."
    )


def _reinstall(install: Install) -> str:
    """Reinstall a copy install from the git remote.

    Always spells out the full git URL. Resolving the bare name would hit
    PyPI, where `sandglass` is a different project entirely.
    """
    target = f"git+{DEFAULT_REPO_URL}@{DEFAULT_BRANCH}#subdirectory={PACKAGE_SUBDIR}"

    if install.installer == "pipx":
        cmd = ["pipx", "install", "--force", target]
    elif install.installer == "uv":
        cmd = ["uv", "tool", "install", "--force", target]
    else:
        cmd = [install.interpreter, "-m", "pip", "install", "--upgrade", target]

    result = _run(cmd, timeout=INSTALL_TIMEOUT)
    if result.returncode != 0:
        raise UpdateError(
            f"`{' '.join(cmd)}` failed:\n"
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
        )
    return f"Reinstalled from {DEFAULT_REPO_URL} ({DEFAULT_BRANCH}) via {install.installer}."
