# test_parsers.py — tests for JSON extraction and checklist parsing
import pytest
from runbook import _extract_json_array, parse_checklist_steps


class TestExtractJsonArray:
    def test_plain_json_array(self):
        assert _extract_json_array('["step 1", "step 2"]') == ["step 1", "step 2"]

    def test_strips_markdown_json_fence(self):
        text = '```json\n["step 1", "step 2"]\n```'
        assert _extract_json_array(text) == ["step 1", "step 2"]

    def test_strips_plain_code_fence(self):
        text = '```\n["step 1"]\n```'
        assert _extract_json_array(text) == ["step 1"]

    def test_finds_array_embedded_in_prose(self):
        text = 'Here is my plan:\n["step 1", "step 2"]\nThat is all.'
        assert _extract_json_array(text) == ["step 1", "step 2"]

    def test_single_item_array(self):
        assert _extract_json_array('["only step"]') == ["only step"]

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError):
            _extract_json_array("")

    def test_raises_when_all_lines_too_short(self):
        # Lines under 10 chars don't qualify as prose steps
        with pytest.raises(ValueError):
            _extract_json_array("no")

    def test_prose_falls_back_to_lines(self):
        # LLM refused with prose — strategy 4 extracts lines as steps
        result = _extract_json_array("I cannot produce a plan for this request.")
        assert result == ["I cannot produce a plan for this request."]

    def test_one_array_per_line(self):
        # LLM returned one ["step"] per line instead of a single array
        text = '["Extract key facts"]\n["Summarise findings"]\n["Write report"]'
        assert _extract_json_array(text) == ["Extract key facts", "Summarise findings", "Write report"]

    def test_json_object_falls_back_to_prose(self):
        # A JSON object is not a list — falls through to prose extraction
        result = _extract_json_array('{"step": "do something useful here"}')
        assert len(result) >= 1


class TestParseChecklistSteps:
    def test_returns_only_unchecked_items(self):
        content = "- [ ] First task\n- [ ] Second task\n- [x] Already done"
        steps = parse_checklist_steps(content)
        assert len(steps) == 2

    def test_returns_correct_text(self):
        content = "- [ ] First task\n- [ ] Second task"
        steps = parse_checklist_steps(content)
        assert steps[0][1] == "First task"
        assert steps[1][1] == "Second task"

    def test_skips_checked_items(self):
        content = "- [x] Done\n- [X] Also done\n- [ ] Pending"
        steps = parse_checklist_steps(content)
        assert len(steps) == 1
        assert steps[0][1] == "Pending"

    def test_empty_content_returns_empty_list(self):
        assert parse_checklist_steps("No checklist here") == []

    def test_empty_string_returns_empty_list(self):
        assert parse_checklist_steps("") == []

    def test_all_checked_returns_empty_list(self):
        content = "- [x] Step 1\n- [x] Step 2"
        assert parse_checklist_steps(content) == []

    def test_returns_line_index(self):
        content = "Some intro\n- [ ] Step one\n- [ ] Step two"
        steps = parse_checklist_steps(content)
        # Line indices should be distinct and reflect actual position
        assert steps[0][0] != steps[1][0]
