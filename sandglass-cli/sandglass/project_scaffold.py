"""Scaffolds a new project's Claude Code setup from bundled templates.

Templates live in ``sandglass/templates/``: a ``CLAUDE.md`` file plus
``master_plan/`` and ``prompt_tools/`` directories.

**A template file with content is copied; an empty one is created empty.**
That split is deliberate. Most of these files are the project's own words --
its architecture, its work log, its queue -- and pre-filling them with prose
someone has to delete first is worse than an empty file. But two of them are
scaffolding rather than content: ``Progress.md`` (a dashboard whose shape is
the point) and ``count_blocks.py`` (the script that generates its numbers).
Shipping those as 0-byte files means every project reinvents them, and the
bundled ``CLAUDE.md`` already tells the agent they exist.

``claude-md-update`` overwrites the bundled ``CLAUDE.md`` in place, so the
next ``new-claude-project`` run picks up whatever the current project's
CLAUDE.md says.
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
    """Create CLAUDE.md, AGENTS.md, master_plan/, and prompt_tools/ under ``target_dir``.

    CLAUDE.md and AGENTS.md are both copied from the one bundled template. Under master_plan/ and
    prompt_tools/, a template that has content is copied and one that is
    empty contributes only its filename -- see the module docstring for why
    the split exists. Anything that already exists at the target path is left
    untouched and reported as skipped, so re-running this never overwrites a
    project's own work.
    """
    if not os.path.isfile(TEMPLATE_CLAUDE_MD):
        raise FileNotFoundError(f"Bundled template not found: {TEMPLATE_CLAUDE_MD}")

    result = ScaffoldResult()
    os.makedirs(target_dir, exist_ok=True)

    # One template, written out under both names. Claude Code reads CLAUDE.md
    # and everything on the AGENTS.md convention reads AGENTS.md; neither reads
    # the other's file, so a project scaffolded with only one of them gives half
    # its agents no rules at all. They start identical and the rule inside them
    # says to keep them that way -- `sandglass queue lint` checks it.
    _copy_file(TEMPLATE_CLAUDE_MD, os.path.join(target_dir, "CLAUDE.md"), result)
    _copy_file(TEMPLATE_CLAUDE_MD, os.path.join(target_dir, "AGENTS.md"), result)

    for dirname in TEMPLATE_DIRS:
        src_dir = os.path.join(TEMPLATES_DIR, dirname)
        dest_dir = os.path.join(target_dir, dirname)
        os.makedirs(dest_dir, exist_ok=True)
        for filename in sorted(os.listdir(src_dir)):
            src_path = os.path.join(src_dir, filename)
            dest_path = os.path.join(dest_dir, filename)
            if os.path.exists(dest_path):
                result.skipped.append(dest_path)
                continue
            if os.path.getsize(src_path) > 0:
                shutil.copyfile(src_path, dest_path)
            else:
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
