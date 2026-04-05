"""Tests for runner.py pure-Python helpers.

The run_* functions invoke the Claude CLI subprocess and are not tested here.
We test only the functions that are independently testable without external I/O:
  - _extract_json
  - _fix_json_newlines
  - _render_prompt
"""

from __future__ import annotations

import textwrap

import pytest

from controlplane.runner import _extract_json, _fix_json_newlines, _render_prompt


# ---------------------------------------------------------------------------
# _fix_json_newlines
# ---------------------------------------------------------------------------

class TestFixJsonNewlines:
    def test_no_change_outside_strings(self):
        # Newlines outside string values (structural whitespace) must be preserved
        raw = '{\n  "key": "value"\n}'
        fixed = _fix_json_newlines(raw)
        # The newlines outside the string should survive; the value should be unchanged
        assert '"value"' in fixed

    def test_escapes_newline_inside_string(self):
        raw = '{"key": "line1\nline2"}'
        fixed = _fix_json_newlines(raw)
        assert "\\n" in fixed
        assert "\n" not in fixed.split('"key": "')[1].split('"')[0]

    def test_escapes_carriage_return_inside_string(self):
        raw = '{"key": "a\rb"}'
        fixed = _fix_json_newlines(raw)
        assert "\\r" in fixed

    def test_escapes_tab_inside_string(self):
        raw = '{"key": "a\tb"}'
        fixed = _fix_json_newlines(raw)
        assert "\\t" in fixed

    def test_preserves_already_escaped_backslash(self):
        raw = r'{"key": "path\\to\\file"}'
        fixed = _fix_json_newlines(raw)
        assert fixed == raw

    def test_handles_multiple_strings(self):
        raw = '{"a": "line1\nline2", "b": "no change"}'
        fixed = _fix_json_newlines(raw)
        import json
        parsed = json.loads(fixed)
        assert parsed["a"] == "line1\nline2"
        assert parsed["b"] == "no change"

    def test_empty_string(self):
        assert _fix_json_newlines("") == ""

    def test_no_strings_in_input(self):
        raw = "123"
        assert _fix_json_newlines(raw) == raw


# ---------------------------------------------------------------------------
# _extract_json — clean JSON
# ---------------------------------------------------------------------------

class TestExtractJsonClean:
    def test_parses_clean_json_object(self):
        raw = '{"task_id": "TASK-0001", "status": "done"}'
        result = _extract_json(raw)
        assert result["task_id"] == "TASK-0001"
        assert result["status"] == "done"

    def test_parses_json_with_leading_trailing_whitespace(self):
        raw = '   \n{"x": 1}\n   '
        result = _extract_json(raw)
        assert result["x"] == 1

    def test_parses_nested_json(self):
        raw = '{"outer": {"inner": "value"}, "list": [1, 2, 3]}'
        result = _extract_json(raw)
        assert result["outer"]["inner"] == "value"
        assert result["list"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# _extract_json — preamble before JSON
# ---------------------------------------------------------------------------

class TestExtractJsonWithPreamble:
    def test_skips_text_preamble(self):
        raw = "Here is my answer:\n\n{\"status\": \"done\", \"plan\": \"step 1\"}"
        result = _extract_json(raw)
        assert result["status"] == "done"

    def test_skips_multiline_preamble(self):
        preamble = "I have analysed the repository.\nI am ready.\n\n"
        body = '{"task_id": "TASK-0001", "status": "done", "plan": "x"}'
        result = _extract_json(preamble + body)
        assert result["task_id"] == "TASK-0001"

    def test_prefers_largest_valid_json_object(self):
        # Two JSON-like substrings — the outer one should win
        raw = 'Ignored {"nested": {"a": 1}, "b": 2} trailing'
        result = _extract_json(raw)
        assert result["b"] == 2


# ---------------------------------------------------------------------------
# _extract_json — markdown fences
# ---------------------------------------------------------------------------

class TestExtractJsonMarkdownFences:
    def test_extracts_from_json_fence(self):
        raw = textwrap.dedent("""\
            Here is the result:

            ```json
            {"status": "done", "plan": "implement it"}
            ```
        """)
        result = _extract_json(raw)
        assert result["status"] == "done"

    def test_extracts_from_plain_fence(self):
        raw = textwrap.dedent("""\
            ```
            {"status": "done"}
            ```
        """)
        result = _extract_json(raw)
        assert result["status"] == "done"


# ---------------------------------------------------------------------------
# _extract_json — literal newlines inside strings (tests _fix_json_newlines path)
# ---------------------------------------------------------------------------

class TestExtractJsonLiteralNewlines:
    def test_handles_literal_newlines_in_string_value(self):
        # Simulate an agent that outputs JSON with unescaped newlines inside a string
        raw = '{"plan": "Step 1\nStep 2\nStep 3", "status": "done"}'
        result = _extract_json(raw)
        assert "Step 1" in result["plan"]
        assert "Step 2" in result["plan"]

    def test_handles_literal_tabs_in_string_value(self):
        raw = '{"summary": "col1\tcol2", "status": "done"}'
        result = _extract_json(raw)
        assert result["status"] == "done"


# ---------------------------------------------------------------------------
# _extract_json — garbage input raises ValueError
# ---------------------------------------------------------------------------

class TestExtractJsonErrors:
    def test_raises_on_non_json_input(self):
        with pytest.raises(ValueError, match="Could not extract valid JSON"):
            _extract_json("This is completely plain text with no JSON at all")

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError):
            _extract_json("")

    def test_raises_on_only_braces(self):
        # Lone braces that don't form valid JSON
        with pytest.raises(ValueError):
            _extract_json("{{{")



# ---------------------------------------------------------------------------
# _render_prompt
# ---------------------------------------------------------------------------

class TestRenderPrompt:
    def test_substitutes_all_variables(self):
        rendered = _render_prompt(
            "planner.md",
            feature_id="FEATURE-0001",
            feature_title="My Feature",
            feature_description="Do the thing",
            task_id="TASK-0001",
            repo_path="/repo",
            revision_instructions=" Your job is to plan.",
            revision_context="",
        )
        assert "FEATURE-0001" in rendered
        assert "My Feature" in rendered
        assert "Do the thing" in rendered
        assert "TASK-0001" in rendered
        assert "/repo" in rendered

    def test_revision_instructions_inserted(self):
        rendered = _render_prompt(
            "planner.md",
            feature_id="FEATURE-0001",
            feature_title="T",
            feature_description="D",
            task_id="TASK-0001",
            repo_path="/repo",
            revision_instructions=" REVISED",
            revision_context="## Previous Plan\n\nOld plan\n\n",
        )
        assert "REVISED" in rendered
        assert "Previous Plan" in rendered

    def test_missing_variable_raises(self):
        with pytest.raises(KeyError):
            _render_prompt(
                "planner.md",
                # intentionally omit required variables
                feature_title="T",
            )
