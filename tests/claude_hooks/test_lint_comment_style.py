"""`lint_comment_style` hook tests, loaded by path via importlib (script live
outside any package). Helpers tested directly; ``main()`` driven via stdin.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.conftest import load_module_from_path

HOOKS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "hooks"

lint = load_module_from_path(HOOKS_DIR / "lint_comment_style.py", "lint_comment_style")


class TestScan:
    @pytest.mark.parametrize(
        ("text", "category"),
        [
            ("    x = 1  # increment the counter", "article"),
            ("    # an off-by-one guard", "article"),
            ("    # just a helper", "filler"),
            ("    # really needed here", "filler"),
            ("    # NOTE: watch out", "NOTE/WARNING"),
            ("    # WARNING danger", "NOTE/WARNING"),
            ("    # ====", "banner"),
            ("    # ------------", "banner"),
            ("    # TODO fix later", "stray TODO"),
        ],
    )
    def test_flags(self, text: str, category: str) -> None:
        findings = lint._scan(text)
        assert any(category in f for f in findings)

    @pytest.mark.parametrize(
        "text",
        [
            "    # skip sentinel row",
            "    # retry on 429 rate limit",
            "    # TODO(#142): drop after 2410",
            "    # TODO(devemberx): rework guard",
            "    x = 1  # noqa: E501 the legacy path here",
            "    y = f()  # type: ignore[arg-type] the shim",
            "    code_only = 1",
            "",
        ],
    )
    def test_clean(self, text: str) -> None:
        assert lint._scan(text) == []

    def test_hash_inside_string_skipped(self) -> None:
        # ``#`` sits inside an unbalanced quote → not a comment.
        assert lint._scan('    sep = "the # char"') == []


class TestCommentText:
    def test_no_hash(self) -> None:
        assert lint._comment_text("x = 1") is None

    def test_plain_comment(self) -> None:
        assert lint._comment_text("x = 1  # note") == "# note"

    def test_inside_string_guard(self) -> None:
        assert lint._comment_text('s = "a # b"') is None


class TestEditedText:
    def test_new_string(self) -> None:
        assert "# a" in lint._edited_text({"new_string": "x  # a"})

    def test_content(self) -> None:
        assert "# b" in lint._edited_text({"content": "y  # b"})

    def test_multiedit(self) -> None:
        payload = {"edits": [{"new_string": "# c"}, {"new_string": "# d"}]}
        text = lint._edited_text(payload)
        assert "# c" in text and "# d" in text

    def test_non_dict_edits_ignored(self) -> None:
        assert lint._edited_text({"edits": ["nope", {}]}) == ""


def _run(payload: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return lint.main()


class TestMain:
    def test_violation_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = _run(
            {"tool_input": {"file_path": "x.py", "new_string": "# just the thing"}},
            monkeypatch,
        )
        assert code == 2
        assert "tripwire" in capsys.readouterr().err

    def test_clean_exits_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {"tool_input": {"file_path": "x.py", "new_string": "# skip row"}}
        assert _run(payload, monkeypatch) == 0

    def test_non_py_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {"tool_input": {"file_path": "x.md", "new_string": "# just this"}}
        assert _run(payload, monkeypatch) == 0

    def test_missing_file_path_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _run({"tool_input": {"new_string": "# just this"}}, monkeypatch) == 0

    def test_bad_json_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert lint.main() == 0

    def test_non_dict_tool_input_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _run({"tool_input": "nope"}, monkeypatch) == 0
