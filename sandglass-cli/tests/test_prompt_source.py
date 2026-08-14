import os

from sandglass import prompt_source
from sandglass.prompt_source import _DELIMITER_RE, history_path_for, throughput


SAMPLE = """model: opus

Refactor the auth module.

====

Write integration tests.
"""


def test_parse_blocks_finds_both_blocks_in_order():
    blocks = prompt_source.parse_blocks(SAMPLE)

    assert len(blocks) == 2
    assert "model: opus" in blocks[0]["text"]
    assert "Refactor the auth module." in blocks[0]["text"]
    assert blocks[1]["text"].strip() == "Write integration tests."


def test_parse_blocks_handles_many_blocks_with_only_separators_between_them():
    blocks = prompt_source.parse_blocks("First\n====\nSecond\n====\nThird")

    assert [b["text"] for b in blocks] == ["First", "Second", "Third"]


def test_parse_blocks_treats_a_delimiter_free_file_as_a_single_lone_block():
    # No delimiter is needed when there's only one prompt -- this is also
    # what the last remaining prompt looks like after every earlier one has
    # already been cut out of the file.
    blocks = prompt_source.parse_blocks("just some text\nwith no delimiters\n")

    assert len(blocks) == 1
    assert blocks[0]["text"] == "just some text\nwith no delimiters"


def test_parse_blocks_returns_empty_list_for_blank_file():
    assert prompt_source.parse_blocks("   \n\n  \n") == []


def test_read_blocks_returns_empty_list_for_missing_file(tmp_path):
    missing = str(tmp_path / "does-not-exist.md")
    assert prompt_source.read_blocks(missing) == []


def test_cut_first_block_removes_only_the_first_block(tmp_path):
    source = tmp_path / "future_prompts.md"
    source.write_text(SAMPLE, encoding="utf-8")

    cut_text = prompt_source.cut_first_block(str(source))

    assert cut_text is not None
    assert "Refactor the auth module." in cut_text
    remaining = prompt_source.read_blocks(str(source))
    assert len(remaining) == 1
    assert "Write integration tests." in remaining[0]["text"]


def test_cut_first_block_leaves_no_stray_leading_delimiter(tmp_path):
    source = tmp_path / "future_prompts.md"
    source.write_text(SAMPLE, encoding="utf-8")

    prompt_source.cut_first_block(str(source))

    remaining_raw = source.read_text(encoding="utf-8")
    assert not _DELIMITER_RE.search(remaining_raw)


def test_cut_first_block_returns_none_when_no_blocks_left(tmp_path):
    source = tmp_path / "empty.md"
    source.write_text("   \n\n  \n", encoding="utf-8")

    assert prompt_source.cut_first_block(str(source)) is None


def test_prepend_to_history_creates_file_with_intro(tmp_path):
    history = tmp_path / "prompt_history.md"

    prompt_source.prepend_to_history(str(history), "Fix auth bug", "opus", "Fix the auth bug.")

    content = history.read_text(encoding="utf-8")
    assert "Executed Prompts (History)" in content
    assert "## [Sandglass work] Fix auth bug" in content
    assert "opus" in content
    assert "Fix the auth bug." in content


def test_prepend_to_history_allows_overriding_via_and_label(tmp_path):
    history = tmp_path / "prompt_history.md"

    prompt_source.prepend_to_history(
        str(history), "Fix auth bug", "sonnet", "Fix the auth bug.",
        via="interactive chat (future prompts)", label=None,
    )

    content = history.read_text(encoding="utf-8")
    assert "## Fix auth bug" in content
    assert "[Sandglass work]" not in content
    assert "via `interactive chat (future prompts)`" in content


def test_append_interruption_note_creates_file_with_intro(tmp_path):
    history = tmp_path / "prompt_history.md"

    prompt_source.append_interruption_note(str(history), "Fix auth bug", "2026-07-23T21:15:00+00:00")

    content = history.read_text(encoding="utf-8")
    assert "Executed Prompts (History)" in content
    assert "INTERRUPTED, will auto-resume" in content
    assert "Fix auth bug" in content
    assert "2026-07-23T21:15:00+00:00" in content


def test_append_interruption_note_works_without_a_known_resume_time(tmp_path):
    history = tmp_path / "prompt_history.md"

    prompt_source.append_interruption_note(str(history), "Fix auth bug", None)

    content = history.read_text(encoding="utf-8")
    assert "INTERRUPTED" in content
    assert "expected around" not in content


def test_append_interruption_note_does_not_touch_the_source_file(tmp_path):
    source = tmp_path / "future_prompts.md"
    source.write_text(SAMPLE, encoding="utf-8")
    history = tmp_path / "prompt_history.md"

    prompt_source.append_interruption_note(str(history), "Refactor the auth module.", "later")

    # Only the history file changes -- the block stays queued, untouched.
    remaining = prompt_source.read_blocks(str(source))
    assert len(remaining) == 2


def test_prepend_to_history_puts_newest_entry_first(tmp_path):
    history = tmp_path / "prompt_history.md"

    prompt_source.prepend_to_history(str(history), "First done", "opus", "first block")
    prompt_source.prepend_to_history(str(history), "Second done", "sonnet", "second block")

    content = history.read_text(encoding="utf-8")
    assert content.index("Second done") < content.index("First done")


def test_history_path_for_is_a_sibling_of_the_source_file():
    path = prompt_source.history_path_for(os.path.join("prompt_tools", "future_prompts.md"))
    assert path == os.path.join("prompt_tools", "prompt_history.md")


# --- throughput() -- the done/remaining/total/pct count behind Progress.md's
# "Prompt throughput" section and the 5%-milestone notifications built on it.


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_throughput_is_none_when_neither_file_has_content(tmp_path):
    source = str(tmp_path / "future_prompts.md")
    assert throughput(source) is None


def test_throughput_basic_count(tmp_path):
    source = _write(tmp_path / "future_prompts.md", "First block\n\n====\n\nSecond block\n")
    _write(tmp_path / "prompt_history.md", "# History\n\n---\n\n## First done\n\nstuff\n")

    assert throughput(source) == (1, 2, 3, 33)  # 1 done, 2 remaining, 3 total, 33% (rounds down)


def test_throughput_all_done_is_100_percent(tmp_path):
    source = str(tmp_path / "future_prompts.md")  # empty/missing -- nothing queued
    _write(tmp_path / "prompt_history.md", "# History\n\n---\n\n## One\n\n## Two\n")

    assert throughput(source) == (2, 0, 2, 100)


def test_throughput_nothing_done_is_zero_percent(tmp_path):
    source = _write(tmp_path / "future_prompts.md", "Only block\n")
    assert throughput(source) == (0, 1, 1, 0)


def test_throughput_ignores_a_quoted_heading_inside_a_fenced_block(tmp_path):
    """The exact bug templates/prompt_tools/count_blocks.py documents: a
    completion entry legitimately quotes the block it ran inside a fenced
    code block, and that quoted text can itself contain a `## ` line."""
    source = str(tmp_path / "future_prompts.md")
    _write(
        tmp_path / "prompt_history.md",
        "# History\n\n---\n\n"
        "## Real entry\n\n"
        "**Executed:** 2026-08-14\n\n"
        "```\n"
        "============================\n\n"
        "## This looks like a heading but it is quoted prompt text\n\n"
        "============================\n"
        "```\n",
    )
    assert throughput(source) == (1, 0, 1, 100)  # not 2


def test_throughput_pct_rounds_down(tmp_path):
    """1/3 = 33.33...%, must read 33 not 34 -- a milestone notification should
    announce a percentage once it's actually reached, never early."""
    source = _write(tmp_path / "future_prompts.md", "A\n\n====\n\nB\n")
    _write(tmp_path / "prompt_history.md", "# History\n\n---\n\n## Done one\n")

    done, remaining, total, pct = throughput(source)
    assert (done, remaining, total) == (1, 2, 3)
    assert pct == 33


def test_throughput_missing_history_file_counts_as_zero_done(tmp_path):
    source = _write(tmp_path / "future_prompts.md", "Only block\n")
    # No prompt_history.md written at all.
    assert throughput(source) == (0, 1, 1, 0)
