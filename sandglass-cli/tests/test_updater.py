"""Tests for `sandglass update`."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from sandglass import updater


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)


def _repo(tmp_path):
    """A throwaway clone-shaped install: a git worktree with the package in it."""
    root = tmp_path / "repo"
    (root / "sandglass-cli" / "sandglass").mkdir(parents=True)
    _git("init", "-q", ".", cwd=root)
    _git("config", "user.email", "t@t.t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "README.md").write_text("hi", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "initial", cwd=root)
    return root


# --- detection ---------------------------------------------------------------


def test_a_package_inside_a_git_worktree_is_treated_as_a_clone(tmp_path):
    root = _repo(tmp_path)
    pkg = root / "sandglass-cli" / "sandglass" / "__init__.py"
    pkg.write_text("", encoding="utf-8")

    install = updater.detect_install(str(pkg))

    assert install.repo_dir == str(root).replace("\\", "/")
    assert install.editable is True
    assert install.can_pull is True


def test_direct_url_metadata_wins_over_the_guess(tmp_path):
    """A copy install can't be pulled even if it somehow sits under a repo."""
    site = tmp_path / "site-packages"
    (site / "sandglass").mkdir(parents=True)
    dist = site / "sandglass-0.9.0.dist-info"
    dist.mkdir()
    (dist / "direct_url.json").write_text(
        json.dumps({"dir_info": {"editable": False}, "url": "file:///somewhere"}),
        encoding="utf-8",
    )

    install = updater.detect_install(str(site / "sandglass" / "__init__.py"))

    assert install.editable is False
    assert install.can_pull is False


@pytest.mark.parametrize(
    "interpreter, expected",
    [
        (r"C:\Users\x\AppData\Local\pipx\pipx\venvs\sandglass\Scripts\python.exe", "pipx"),
        ("/home/x/.local/share/uv/tools/sandglass/bin/python", "uv"),
        ("/usr/bin/python3", "pip"),
    ],
)
def test_installer_is_inferred_from_the_interpreter(tmp_path, monkeypatch, interpreter, expected):
    monkeypatch.setattr(updater.sys, "executable", interpreter)
    pkg = tmp_path / "sandglass" / "__init__.py"
    pkg.parent.mkdir()
    pkg.write_text("", encoding="utf-8")

    assert updater.detect_install(str(pkg)).installer == expected


# --- safety ------------------------------------------------------------------


def test_it_refuses_to_pull_over_uncommitted_work(tmp_path):
    """The failure mode worth preventing: someone loses an afternoon."""
    root = _repo(tmp_path)
    (root / "work_in_progress.py").write_text("half a feature", encoding="utf-8")
    install = updater.Install(
        package_dir=str(root / "sandglass-cli" / "sandglass"),
        editable=True, repo_dir=str(root), installer="pip", interpreter="python",
    )

    with pytest.raises(updater.UpdateError, match="uncommitted"):
        updater.update(install)

    # And it really didn't touch anything.
    assert (root / "work_in_progress.py").read_text(encoding="utf-8") == "half a feature"


def test_a_clean_clone_pulls_fast_forward_only(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    install = updater.Install(
        package_dir=str(root), editable=True, repo_dir=str(root),
        installer="pip", interpreter="python",
    )
    seen = []

    def fake_run(args, cwd=None, timeout=updater.GIT_TIMEOUT):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(updater, "_run", fake_run)
    updater.update(install)

    pull = next(a for a in seen if "pull" in a)
    # Never a merge commit or a rewrite of the user's history.
    assert "--ff-only" in pull
    assert not any("reset" in a or "--hard" in a for a in seen)


def test_reinstall_never_resolves_the_bare_package_name(tmp_path, monkeypatch):
    """`sandglass` on PyPI is an unrelated project — resolving the bare name
    would silently replace this tool with someone else's package."""
    captured = {}

    def fake_run(args, cwd=None, timeout=updater.GIT_TIMEOUT):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(updater, "_run", fake_run)
    for installer in ("pipx", "uv", "pip"):
        install = updater.Install(
            package_dir=str(tmp_path), editable=False, repo_dir=None,
            installer=installer, interpreter="python",
        )
        updater.update(install)
        target = captured["args"][-1]
        assert target.startswith("git+https://"), f"{installer} resolved {target!r}"
        assert "sandglass-cli" in target, "must point at the package subdirectory"
        # The bare name must never appear as an install target on its own --
        # that is the form pip would resolve against PyPI.
        assert target != "sandglass"


def test_a_copy_install_says_it_cannot_compare(tmp_path):
    install = updater.Install(
        package_dir=str(tmp_path), editable=False, repo_dir=None,
        installer="pipx", interpreter="python",
    )

    status = updater.check(install)

    assert status.detail
    assert "can't tell" in status.detail


def test_an_unreachable_remote_is_an_error_not_a_silent_no_op(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    install = updater.Install(
        package_dir=str(root), editable=True, repo_dir=str(root),
        installer="pip", interpreter="python",
    )

    def fake_run(args, cwd=None, timeout=updater.GIT_TIMEOUT):
        if "fetch" in args:
            return subprocess.CompletedProcess(args, 1, "", "could not resolve host")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(updater, "_run", fake_run)
    with pytest.raises(updater.UpdateError, match="Could not reach the remote"):
        updater.check(install)
