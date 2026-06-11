"""Unit tests for the `validate_pr_body` PreToolUse hook in `.claude/hooks/`.

The hook script lives outside any package, so it is loaded by path via importlib.
These tests cover the pure helper functions; the `main()` stdin/exit-code path is
left to manual / e2e checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import load_module_from_path

HOOKS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "hooks"

body = load_module_from_path(HOOKS_DIR / "validate_pr_body.py", "validate_pr_body")


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

    def test_long_flag_equals(self) -> None:
        assert body.extract_body("gh pr create --body=hello") == "hello"

    def test_field_body(self) -> None:
        assert body.extract_body("gh api /x -f body=hello") == "hello"

    def test_field_body_file(self, tmp_path: Path) -> None:
        f = tmp_path / "b.txt"
        f.write_text("filed")
        assert body.extract_body(f"gh api /x -f body=@{f}") == "filed"

    def test_body_file_flag(self, tmp_path: Path) -> None:
        f = tmp_path / "b.txt"
        f.write_text("from file")
        assert body.extract_body(f"gh pr create --body-file {f}") == "from file"

    def test_body_file_equals(self, tmp_path: Path) -> None:
        f = tmp_path / "b.txt"
        f.write_text("from file")
        assert body.extract_body(f"gh pr create --body-file={f}") == "from file"

    def test_body_file_unreadable(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.txt"
        assert body.extract_body(f"gh pr create --body-file {missing}") is None

    def test_no_body(self) -> None:
        assert body.extract_body("gh pr edit --title only") is None


class TestNonAsciiDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "이것은 한국어",  # Korean
            "café résumé",  # Latin accents
            "naïve façade",  # diacritics
            "emoji 🚀 plus 한글",  # emoji allowed but Korean still blocks
        ],
    )
    def test_blocks_non_ascii_letters(self, text: str) -> None:
        assert body.has_disallowed_non_ascii(text)

    @pytest.mark.parametrize(
        "text",
        [
            "plain english only - [x] done!",
            "ship it 🚀",  # lone emoji
            "done ✅ and 🎉",  # symbol + emoji
            "flags 🇰🇷 and family 👨‍👩‍👧 sequences",  # regional indicators + ZWJ
        ],
    )
    def test_allows_ascii_and_emoji(self, text: str) -> None:
        assert not body.has_disallowed_non_ascii(text)


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


class TestChangesFormatErrors:
    def test_valid_two_bullets(self) -> None:
        text = "## Summary\nx\n\n## Changes\n\n- motivation\n- change\n\n## Testing\n"
        assert body.changes_format_errors(text) == []

    def test_section_ends_at_next_header(self) -> None:
        # bullets under a later section must not be counted toward Changes
        text = "## Changes\n\n- one\n- two\n\n## Testing\n\n- not counted\n"
        assert body.changes_format_errors(text) == []

    def test_wrong_count(self) -> None:
        text = "## Changes\n\n- only one\n\n## Testing\n"
        errs = body.changes_format_errors(text)
        assert any("exactly 2" in e for e in errs)

    def test_empty_template_stub_fails(self) -> None:
        # the unfilled "- \n- " stub has no text, so it counts as zero bullets
        text = "## Changes\n\n- \n- \n\n## Testing\n"
        errs = body.changes_format_errors(text)
        assert any("exactly 2" in e for e in errs)

    def test_over_limit_bullet(self) -> None:
        text = f"## Changes\n\n- {'x' * 130}\n- ok\n\n## Testing\n"
        errs = body.changes_format_errors(text)
        assert any("limit: 120" in e for e in errs)

    def test_absent_section_is_rejected(self) -> None:
        errs = body.changes_format_errors("## Summary\nno changes section\n")
        assert any("Changes" in e for e in errs)

    def test_section_at_end_of_body(self) -> None:
        # no trailing section header after Changes
        text = "## Summary\nx\n\n## Changes\n\n- one\n- two\n"
        assert body.changes_format_errors(text) == []
