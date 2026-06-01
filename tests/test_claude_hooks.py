"""Unit tests for the tracked Claude Code PreToolUse hooks in `.claude/hooks/`.

The hook scripts use hyphenated filenames (not importable as normal modules), so
each is loaded by path via importlib. These tests cover the pure helper functions;
the `main()` stdin/exit-code path is left to manual / e2e checks.
"""

from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path
from types import ModuleType

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "hooks"


def _load(filename: str, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, HOOKS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge = _load("validate-pr-merge.py", "validate_pr_merge")
body = _load("validate-pr-body.py", "validate_pr_body")


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


class TestBodyClassify:
    @pytest.mark.parametrize(
        "cmd",
        [
            "gh pr create --title x --body y",
            "gh pr edit 5 --body y",
            "gh api -X PATCH /repos/o/r/pulls/5 -f body=z",
        ],
    )
    def test_pr(self, cmd: str) -> None:
        assert body.classify(cmd) == "pr"

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh pr comment 5 --body y",
            "gh issue create --body y",
            "gh issue comment 5 --body y",
            "gh api -X PATCH /repos/o/r/issues/5 -f body=z",
        ],
    )
    def test_other(self, cmd: str) -> None:
        assert body.classify(cmd) == "other"

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh pr list",
            "git commit -m x",
            # PR review/comment API endpoints are not full-body PR edits
            "gh api /repos/o/r/pulls/5/comments -f body=z",
            "gh api /repos/o/r/pulls/5/reviews -f body=z",
        ],
    )
    def test_skip(self, cmd: str) -> None:
        assert body.classify(cmd) is None


class TestBodyExtractBody:
    def test_long_flag(self) -> None:
        assert body.extract_body("gh pr create --body 'hello'") == "hello"

    def test_field_body(self) -> None:
        assert body.extract_body("gh api /x -f body=hello") == "hello"

    def test_field_body_file(self, tmp_path: Path) -> None:
        f = tmp_path / "b.txt"
        f.write_text("filed")
        assert body.extract_body(f"gh api /x -f body=@{f}") == "filed"

    def test_no_body(self) -> None:
        assert body.extract_body("gh pr edit --title only") is None


class TestHangulDetection:
    def test_matches_korean(self) -> None:
        assert body.HANGUL_RE.search("이것은 한국어")

    def test_ignores_english(self) -> None:
        assert body.HANGUL_RE.search("plain english only") is None


class TestMissingTemplateBoxes:
    def _template(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "PULL_REQUEST_TEMPLATE.md").write_text(
            "## Type\n- [ ] Bug fix\n- [ ] New feature\n- [x] CI / tooling\n"
        )
        monkeypatch.chdir(tmp_path)

    def test_all_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._template(tmp_path, monkeypatch)
        # state flipped, all labels present
        full = "- [x] Bug fix\n- [ ] New feature\n- [ ] CI / tooling\n"
        assert body.missing_template_boxes(full) == []

    def test_reports_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._template(tmp_path, monkeypatch)
        partial = "- [x] Bug fix\n"
        assert body.missing_template_boxes(partial) == ["CI / tooling", "New feature"]

    def test_no_template_is_tolerant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)  # no .github/ here
        assert body.missing_template_boxes("anything") == []
