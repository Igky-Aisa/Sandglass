import os

import pytest
from typer.testing import CliRunner

from sandglass import project_scaffold
from sandglass.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Run every test in its own directory so .sandglass/ never touches the repo."""
    monkeypatch.chdir(tmp_path)


def test_queue_add_command_reports_success():
    result = runner.invoke(app, ["queue", "add", "Test prompt 1"])

    assert result.exit_code == 0
    assert "Added prompt 1" in result.stdout


def test_queue_list_command_shows_empty_message():
    result = runner.invoke(app, ["queue", "list"])

    assert result.exit_code == 0
    assert "No prompts in queue" in result.stdout


def test_queue_list_command_shows_added_prompt():
    runner.invoke(app, ["queue", "add", "Fix auth bug"])

    result = runner.invoke(app, ["queue", "list"])

    assert result.exit_code == 0
    assert "Fix auth bug" in result.stdout
    assert "Queued Prompts (1)" in result.stdout


def test_queue_remove_then_list_is_empty():
    runner.invoke(app, ["queue", "add", "one-off prompt"])

    remove_result = runner.invoke(app, ["queue", "remove", "1"])
    list_result = runner.invoke(app, ["queue", "list"])

    assert remove_result.exit_code == 0
    assert "No prompts in queue" in list_result.stdout


def test_queue_add_requires_exactly_one_of_text_or_file():
    result = runner.invoke(app, ["queue", "add"])

    assert result.exit_code == 1
    assert "Error" in result.stdout


def test_execute_with_empty_queue_reports_nothing_to_do():
    result = runner.invoke(app, ["execute"])

    assert result.exit_code == 0
    assert "No prompts to execute" in result.stdout


def test_queue_add_with_model_option_shows_model_in_confirmation():
    result = runner.invoke(app, ["queue", "add", "Fix auth bug", "--model", "opus"])

    assert result.exit_code == 0
    assert "model: opus" in result.stdout


def test_queue_list_shows_model_column():
    runner.invoke(app, ["queue", "add", "Fix auth bug", "--model", "opus"])
    runner.invoke(app, ["queue", "add", "Write tests"])

    result = runner.invoke(app, ["queue", "list"])

    assert result.exit_code == 0
    assert "Model" in result.stdout
    assert "opus" in result.stdout
    assert "(default)" in result.stdout


def test_queue_add_parses_model_header_from_text():
    result = runner.invoke(app, ["queue", "add", "model: sonnet\n\nWrite tests"])
    assert result.exit_code == 0
    assert "model: sonnet" in result.stdout

    list_result = runner.invoke(app, ["queue", "list"])
    table_lines = list_result.stdout.splitlines()
    row = next(line for line in table_lines if "Write tests" in line)
    assert "sonnet" in row  # picked up from the header
    assert "model:" not in row  # header line itself is not part of the title


# --- new-claude-project / claude-md-update ----------------------------------


def test_new_claude_project_creates_claude_md_and_template_folders():
    result = runner.invoke(app, ["new-claude-project"])

    assert result.exit_code == 0
    assert os.path.isfile("CLAUDE.md")
    with open(project_scaffold.TEMPLATE_CLAUDE_MD, "r", encoding="utf-8") as fh:
        template_content = fh.read()
    with open("CLAUDE.md", "r", encoding="utf-8") as fh:
        assert fh.read() == template_content

    expected_master_plan = set(os.listdir(os.path.join(project_scaffold.TEMPLATES_DIR, "master_plan")))
    expected_prompt_tools = set(os.listdir(os.path.join(project_scaffold.TEMPLATES_DIR, "prompt_tools")))
    assert set(os.listdir("master_plan")) == expected_master_plan
    assert set(os.listdir("prompt_tools")) == expected_prompt_tools

    # A template with content is copied; an empty one contributes only its filename. Files a
    # project must write in its own words (architecture, system map, manual) stay empty, because
    # prose someone has to delete first is worse than a blank page. Scaffolding whose SHAPE is
    # the deliverable -- the dashboard and the script that generates its numbers -- ships filled
    # in, or every project reinvents it while CLAUDE.md insists it already exists.
    for dirname, filenames in (
        ("master_plan", expected_master_plan),
        ("prompt_tools", expected_prompt_tools),
    ):
        for filename in filenames:
            src = os.path.join(project_scaffold.TEMPLATES_DIR, dirname, filename)
            dest = os.path.join(dirname, filename)
            if os.path.getsize(src) > 0:
                with open(src, encoding="utf-8") as fh:
                    expected = fh.read()
                with open(dest, encoding="utf-8") as fh:
                    assert fh.read() == expected, f"{dest} should carry its template content"
            else:
                assert os.path.getsize(dest) == 0, f"{dest} should be created empty"


def test_new_claude_project_skips_files_that_already_exist():
    os.makedirs("master_plan", exist_ok=True)
    with open("CLAUDE.md", "w", encoding="utf-8") as fh:
        fh.write("my custom project instructions")
    with open(os.path.join("master_plan", "work_log.md"), "w", encoding="utf-8") as fh:
        fh.write("existing log content")

    result = runner.invoke(app, ["new-claude-project"])

    assert result.exit_code == 0
    assert "Skipped" in result.stdout
    with open("CLAUDE.md", "r", encoding="utf-8") as fh:
        assert fh.read() == "my custom project instructions"
    with open(os.path.join("master_plan", "work_log.md"), "r", encoding="utf-8") as fh:
        assert fh.read() == "existing log content"


def test_new_claude_project_accepts_target_path_argument():
    result = runner.invoke(app, ["new-claude-project", "sub-project"])

    assert result.exit_code == 0
    assert os.path.isfile(os.path.join("sub-project", "CLAUDE.md"))
    assert os.path.isdir(os.path.join("sub-project", "master_plan"))
    assert os.path.isdir(os.path.join("sub-project", "prompt_tools"))
    assert not os.path.exists("CLAUDE.md")  # cwd itself untouched


def test_claude_md_update_then_new_claude_project_uses_updated_content(tmp_path, monkeypatch):
    fake_templates_dir = tmp_path / "fake_templates"
    (fake_templates_dir / "master_plan").mkdir(parents=True)
    (fake_templates_dir / "prompt_tools").mkdir(parents=True)
    fake_claude_md = fake_templates_dir / "CLAUDE.md"
    fake_claude_md.write_text("original template", encoding="utf-8")

    monkeypatch.setattr(project_scaffold, "TEMPLATES_DIR", str(fake_templates_dir))
    monkeypatch.setattr(project_scaffold, "TEMPLATE_CLAUDE_MD", str(fake_claude_md))

    with open("CLAUDE.md", "w", encoding="utf-8") as fh:
        fh.write("brand new project rules")

    update_result = runner.invoke(app, ["claude-md-update"])
    assert update_result.exit_code == 0
    assert fake_claude_md.read_text(encoding="utf-8") == "brand new project rules"

    os.remove("CLAUDE.md")
    scaffold_result = runner.invoke(app, ["new-claude-project"])
    assert scaffold_result.exit_code == 0
    with open("CLAUDE.md", "r", encoding="utf-8") as fh:
        assert fh.read() == "brand new project rules"


def test_scaffold_writes_both_agent_files_identically(tmp_path):
    """A project must not ship rules to only half the agents that read it.

    Claude Code reads CLAUDE.md and everything on the AGENTS.md convention reads
    AGENTS.md; neither reads the other's. Both come from one bundled template,
    so they start identical -- `queue lint` is what keeps them that way after.
    """
    from sandglass.project_scaffold import new_claude_project

    target = tmp_path / "proj"
    new_claude_project(str(target))

    claude_md, agents_md = target / "CLAUDE.md", target / "AGENTS.md"
    assert claude_md.exists() and agents_md.exists()
    assert claude_md.read_bytes() == agents_md.read_bytes()
    # The cold-start window ships too, or a non-Claude block has nothing cheap
    # to read when it starts with no session.
    assert (target / "prompt_tools" / "context.md").exists()
