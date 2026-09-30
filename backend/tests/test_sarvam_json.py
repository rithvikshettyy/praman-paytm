from app.clients import sarvam


def test_parses_fenced_json():
    assert sarvam.parse_json_loose('```json\n{"intent": "question"}\n```') == {"intent": "question"}


def test_parses_json_embedded_in_prose():
    assert sarvam.parse_json_loose('Here you go: {"a": [1, 2]} hope that helps') == {"a": [1, 2]}


def test_salvages_complete_objects_from_a_truncated_reply():
    truncated = '{"findings": [{"id": 1}, {"id": 2}, {"id": 3, "note": "cut o'
    assert sarvam.parse_json_loose(truncated) == {"findings": [{"id": 1}, {"id": 2}]}


def test_empty_or_garbage_is_none():
    assert sarvam.parse_json_loose("") is None
    assert sarvam.parse_json_loose("no json here") is None


def test_chunk_text_respects_limit_on_sentence_boundaries():
    text = "First sentence. Second sentence here. Third one। Fourth."
    chunks = list(sarvam._chunk_text(text, 30))

    assert len(chunks) > 1
    assert all(len(c) <= 30 for c in chunks)
    assert " ".join(chunks) == text


def test_terminal_statuses():
    assert sarvam.is_terminal("Completed")
    assert sarvam.is_terminal("partially_completed")
    assert not sarvam.is_terminal("running")
    assert not sarvam.is_terminal(None)
