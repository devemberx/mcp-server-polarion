"""Unit tests for the `validate_pr_merge` hook, loaded by path via importlib
(script lives outside any package). Pure helpers only; `main()` left to e2e.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from tests.conftest import load_module_from_path

HOOKS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "hooks"

merge = load_module_from_path(HOOKS_DIR / "validate_pr_merge.py", "validate_pr_merge")


def argv(cmd: str) -> list[str]:
    return shlex.split(cmd)


class TestIsPrMerge:
    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            ("gh pr merge 72 --squash", True),
            ("gh pr merge --squash --body 'x'", True),
            ("gh pr list", False),
            ("gh pr view 72", False),
            ("git fetch && echo done", False),
            # chained command where an earlier `gh` precedes the merge
            ("gh pr view 72 && gh pr merge 72 --merge", True),
            ("gh pr checkout 72 && gh pr merge 72", True),
            ("FOO=bar gh pr merge 72 --squash", True),
        ],
    )
    def test_detection(self, cmd: str, expected: bool) -> None:
        assert merge.is_pr_merge(argv(cmd)) is expected


class TestMergeFlagDetection:
    @pytest.mark.parametrize(
        "cmd", ["gh pr merge --squash", "gh pr merge -s", "gh pr merge --squash=true"]
    )
    def test_has_squash_true(self, cmd: str) -> None:
        assert merge.has_squash(argv(cmd))

    @pytest.mark.parametrize("cmd", ["gh pr merge --merge", "gh pr merge --rebase"])
    def test_has_squash_false(self, cmd: str) -> None:
        assert not merge.has_squash(argv(cmd))

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh pr merge --subject x",
            "gh pr merge -t x",
            "gh pr merge --subject=x",
            "gh pr merge -tfoo",
        ],
    )
    def test_has_subject_true(self, cmd: str) -> None:
        assert merge.has_subject(argv(cmd))

    def test_has_subject_false(self) -> None:
        assert not merge.has_subject(argv("gh pr merge --squash --body x"))


class TestMergeExtractBody:
    def test_long_flag(self) -> None:
        assert merge.extract_body(argv("gh pr merge --body 'hello'")) == (True, "hello")

    def test_long_flag_equals(self) -> None:
        assert merge.extract_body(argv("gh pr merge --body=hello")) == (True, "hello")

    def test_short_flag_spaced(self) -> None:
        assert merge.extract_body(argv("gh pr merge -b hello")) == (True, "hello")

    def test_short_flag_glued(self) -> None:
        assert merge.extract_body(argv("gh pr merge -bhello")) == (True, "hello")

    def test_no_body(self) -> None:
        assert merge.extract_body(argv("gh pr merge --squash")) == (False, None)

    def test_body_file(self, tmp_path: Path) -> None:
        f = tmp_path / "body.txt"
        f.write_text("from file")
        assert merge.extract_body(argv(f"gh pr merge -F {f}")) == (True, "from file")

    def test_body_file_glued(self, tmp_path: Path) -> None:
        f = tmp_path / "body.txt"
        f.write_text("from file")
        assert merge.extract_body(argv(f"gh pr merge -F{f}")) == (True, "from file")

    def test_body_file_equals(self, tmp_path: Path) -> None:
        f = tmp_path / "body.txt"
        f.write_text("from file")
        assert merge.extract_body(argv(f"gh pr merge --body-file={f}")) == (
            True,
            "from file",
        )

    def test_body_file_unreadable(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.txt"
        assert merge.extract_body(argv(f"gh pr merge --body-file {missing}")) == (
            True,
            None,
        )


CO_AUTHOR = "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"


class TestBodyFormatErrors:
    def test_valid_body(self) -> None:
        good = f"- motivation bullet\n- change bullet\n\n{CO_AUTHOR}"
        assert merge.body_format_errors(good) == []

    def test_wrong_bullet_count(self) -> None:
        errs = merge.body_format_errors(f"- only one bullet\n\n{CO_AUTHOR}")
        assert any("exactly 2 bullets" in e for e in errs)

    def test_comment_lines_ignored(self) -> None:
        text = f"# a comment that starts with dash-ish\n- one\n- two\n\n{CO_AUTHOR}"
        assert merge.body_format_errors(text) == []

    def test_over_limit_bullet(self) -> None:
        long_bullet = "- " + "x" * 130
        errs = merge.body_format_errors(f"{long_bullet}\n- short\n\n{CO_AUTHOR}")
        assert any("limit: 120" in e for e in errs)

    def test_missing_co_author(self) -> None:
        errs = merge.body_format_errors("- one\n- two")
        assert any("co-author" in e.lower() for e in errs)
