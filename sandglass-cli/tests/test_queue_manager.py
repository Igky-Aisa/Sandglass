import pytest

from sandglass.queue_manager import QueueManager
from sandglass.storage import StorageService


@pytest.fixture
def qm(tmp_path):
    return QueueManager(storage=StorageService(base_path=str(tmp_path / ".sandglass")))


def test_add_prompt_adds_text_prompt_to_queue(qm):
    prompt_id = qm.add_prompt(text="Fix auth bug")

    assert prompt_id == "001"
    queue = qm.get_all_prompts()
    assert len(queue) == 1
    assert queue[0].text == "Fix auth bug"
    assert queue[0].source == "text"


def test_add_prompt_from_file(qm, tmp_path):
    file_path = tmp_path / "task.txt"
    file_path.write_text("Write integration tests", encoding="utf-8")

    prompt_id = qm.add_prompt(file_path=str(file_path))

    prompt = qm.get_prompt(1)
    assert prompt_id == "001"
    assert prompt.source == "file"
    assert prompt.text == "Write integration tests"


def test_add_prompt_requires_exactly_one_of_text_or_file(qm):
    with pytest.raises(ValueError):
        qm.add_prompt()
    with pytest.raises(ValueError):
        qm.add_prompt(text="a", file_path="b.txt")


def test_add_prompt_missing_file_raises(qm):
    with pytest.raises(FileNotFoundError):
        qm.add_prompt(file_path="does-not-exist.txt")


def test_ids_increment_sequentially(qm):
    qm.add_prompt(text="first")
    qm.add_prompt(text="second")
    third_id = qm.add_prompt(text="third")

    assert third_id == "003"
    assert len(qm.get_all_prompts()) == 3


def test_remove_prompt_removes_and_returns_it(qm):
    qm.add_prompt(text="keep me")
    qm.add_prompt(text="remove me")

    removed = qm.remove_prompt(2)

    assert removed.text == "remove me"
    remaining = qm.get_all_prompts()
    assert len(remaining) == 1
    assert remaining[0].text == "keep me"


def test_remove_prompt_out_of_range_raises(qm):
    qm.add_prompt(text="only one")

    with pytest.raises(IndexError):
        qm.remove_prompt(5)


def test_clear_queue_empties_it(qm):
    qm.add_prompt(text="a")
    qm.add_prompt(text="b")
    qm.add_prompt(text="c")

    qm.clear_queue()

    assert qm.get_all_prompts() == []


def test_load_save_roundtrip_preserves_data(qm):
    qm.add_prompt(text="persisted prompt")

    reloaded = QueueManager(storage=qm.storage)
    prompts = reloaded.get_all_prompts()

    assert len(prompts) == 1
    assert prompts[0].text == "persisted prompt"


def test_load_queue_returns_empty_list_when_missing(tmp_path):
    qm = QueueManager(storage=StorageService(base_path=str(tmp_path / ".sandglass")))
    assert qm.load_queue() == []


def test_add_prompt_with_explicit_model(qm):
    qm.add_prompt(text="Fix auth bug", model="opus")

    assert qm.get_prompt(1).model == "opus"


def test_add_prompt_with_no_model_defaults_to_none(qm):
    qm.add_prompt(text="Fix auth bug")

    assert qm.get_prompt(1).model is None


def test_model_header_is_parsed_and_stripped_from_text(qm):
    qm.add_prompt(text="model: opus\n\nFix the auth bug in auth.dart")

    prompt = qm.get_prompt(1)
    assert prompt.model == "opus"
    assert prompt.text == "Fix the auth bug in auth.dart"
    assert prompt.title == "Fix the auth bug in auth.dart"


def test_model_header_parsed_from_file(qm, tmp_path):
    file_path = tmp_path / "task.txt"
    file_path.write_text("model: Sonnet\n\nWrite integration tests", encoding="utf-8")

    qm.add_prompt(file_path=str(file_path))

    prompt = qm.get_prompt(1)
    assert prompt.model == "Sonnet"
    assert prompt.text == "Write integration tests"


def test_explicit_model_overrides_header(qm):
    qm.add_prompt(text="model: opus\n\nFix the auth bug", model="sonnet")

    assert qm.get_prompt(1).model == "sonnet"


def test_model_header_requires_blank_line_to_avoid_false_positives(qm):
    # No blank line after "model: X" -> not a header, whole text is literal.
    qm.add_prompt(text="model: this is actually just what I want to say")

    prompt = qm.get_prompt(1)
    assert prompt.model is None
    assert prompt.text == "model: this is actually just what I want to say"


def test_import_from_markdown_adds_each_block_tagged_with_origin(qm, tmp_path):
    source = tmp_path / "future_prompts.md"
    source.write_text(
        "First prompt\n\n====\n\nmodel: opus\n\nSecond prompt\n",
        encoding="utf-8",
    )

    added = qm.import_from_markdown(str(source))

    assert added == 2
    prompts = qm.get_all_prompts()
    assert prompts[0].text == "First prompt"
    assert prompts[0].origin_file == str(source)
    assert prompts[1].text == "Second prompt"
    assert prompts[1].model == "opus"
    assert prompts[1].origin_file == str(source)


def test_import_from_markdown_returns_zero_for_missing_file(qm, tmp_path):
    missing = str(tmp_path / "nope.md")

    assert qm.import_from_markdown(missing) == 0
    assert qm.get_all_prompts() == []


def test_manually_added_prompts_have_no_origin_file(qm):
    qm.add_prompt(text="a normal prompt")

    assert qm.get_prompt(1).origin_file is None
