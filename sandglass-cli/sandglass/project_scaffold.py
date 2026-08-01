"""Scaffolds a new project's Claude Code setup from bundled templates.

Templates live in ``sandglass/templates/``: a ``CLAUDE.md`` file (copied
verbatim) plus ``master_plan/`` and ``prompt_tools/`` directories whose
filenames are reproduced as empty files in the target project. `claude-md
-update` overwrites the bundled ``CLAUDE.md`` in place, so the next
`new-claude-project` run picks up whatever the current project's CLAUDE.md
says.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
TEMPLATE_CLAUDE_MD = os.path.join(TEMPLATES_DIR, "CLAUDE.md")
TEMPLATE_DIRS = ("master_plan", "prompt_tools")


@dataclass
class ScaffoldResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def new_claude_project(target_dir: str = ".") -> ScaffoldResult:
    """Create CLAUDE.md, master_plan/, and prompt_tools/ under ``target_dir``.

    CLAUDE.md is copied with its template content; files under master_plan/
    and prompt_tools/ are created empty (only their names come from the
    template). Anything that already exists at the target path is left
    untouched and reported as skipped.
    """
    if not os.path.isfile(TEMPLATE_CLAUDE_MD):
        raise FileNotFoundError(f"Bundled template not found: {TEMPLATE_CLAUDE_MD}")

    result = ScaffoldResult()
    os.makedirs(target_dir, exist_ok=True)

    _copy_file(TEMPLATE_CLAUDE_MD, os.path.join(target_dir, "CLAUDE.md"), result)

    for dirname in TEMPLATE_DIRS:
        src_dir = os.path.join(TEMPLATES_DIR, dirname)
        dest_dir = os.path.join(target_dir, dirname)
        os.makedirs(dest_dir, exist_ok=True)
        for filename in sorted(os.listdir(src_dir)):
            dest_path = os.path.join(dest_dir, filename)
            if os.path.exists(dest_path):
                result.skipped.append(dest_path)
                continue
            open(dest_path, "a", encoding="utf-8").close()
            result.created.append(dest_path)

    return result


def _copy_file(src: str, dest: str, result: ScaffoldResult) -> None:
    if os.path.exists(dest):
        result.skipped.append(dest)
        return
    shutil.copyfile(src, dest)
    result.created.append(dest)


def update_claude_md_template(source_path: str = "CLAUDE.md") -> str:
    """Overwrite the bundled CLAUDE.md template with ``source_path``'s content.

    Future `new_claude_project` calls will copy this updated content.
    Returns the path the template was written to.
    """
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"No CLAUDE.md found at {source_path}")

    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    shutil.copyfile(source_path, TEMPLATE_CLAUDE_MD)
    return TEMPLATE_CLAUDE_MD
