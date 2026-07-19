"""`block_sensitive_text` hook tests, loaded by path via importlib (script
live outside any package). Pure helpers only; `main()` left to e2e.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import load_module_from_path

HOOKS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "hooks"

guard = load_module_from_path(
    HOOKS_DIR / "block_sensitive_text.py", "block_sensitive_text"
)


class TestOutward:
    @pytest.mark.parametrize(
        "cmd",
        [
            "gh pr create --title x --body y",
            "gh pr edit 5 --body y",
            "gh pr comment 5 --body y",
            "gh issue create --body y",
            "gh issue comment 5 --body y",
            "gh api -X PATCH /repos/o/r/pulls/5 -f body=z",
            "gh release create v1.0.0 --notes x",
            "gh gist create file.txt",
            "git commit -m 'x'",
            "git tag -a v1 -m x",
        ],
    )
    def test_outward(self, cmd: str) -> None:
        assert guard.outward(cmd)

    @pytest.mark.parametrize(
        "cmd",
        [
            "grep -rn SecretDoc src/",
            "cat notes/SecretDoc.md",
            "uv run pytest tests/",
            "git status",
            "git push origin main",
            "git log --oneline",
        ],
    )
    def test_local(self, cmd: str) -> None:
        assert not guard.outward(cmd)


class TestLoadPatterns:
    def test_missing_file_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(guard, "PATTERN_PATH", tmp_path / "absent")
        assert guard.load_patterns() == []

    def test_comments_and_blanks_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "patterns"
        f.write_text("# comment\n\nSecretDoc\\b\n")
        monkeypatch.setattr(guard, "PATTERN_PATH", f)
        patterns = guard.load_patterns()
        assert [p.pattern for p in patterns] == ["SecretDoc\\b"]

    def test_invalid_regex_kept_as_literal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "patterns"
        f.write_text("Secret(Doc\n")
        monkeypatch.setattr(guard, "PATTERN_PATH", f)
        patterns = guard.load_patterns()
        assert len(patterns) == 1
        assert patterns[0].search("path/Secret(Doc/file")

    def test_unreadable_file_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(guard, "PATTERN_PATH", tmp_path)
        assert guard.load_patterns() is None


class TestScan:
    def _patterns(self) -> list[re.Pattern[str]]:
        return [re.compile(r"SecretDoc\b")]

    def test_match_in_command(self) -> None:
        hits = guard.scan("gh pr create --body 'tested vs SecretDoc'", self._patterns())
        assert hits == ["SecretDoc\\b"]

    def test_no_match(self) -> None:
        cmd = "gh pr create --body 'generic wording'"
        assert guard.scan(cmd, self._patterns()) == []

    def test_match_in_body_file(self, tmp_path: Path) -> None:
        f = tmp_path / "body.md"
        f.write_text("Live test vs SecretDoc round-trip")
        hits = guard.scan(f"gh pr create --body-file {f}", self._patterns())
        assert hits == ["SecretDoc\\b"]

    def test_substring_of_public_id_not_matched(self) -> None:
        """Word-boundary pattern must not hit distinct mixed-case public id."""
        hits = guard.scan(
            "gh pr create --body 'eval fixture SecretDocument_Project'",
            self._patterns(),
        )
        assert hits == []


class TestReferencedFileTexts:
    def test_body_file_flag(self, tmp_path: Path) -> None:
        f = tmp_path / "b.md"
        f.write_text("file text")
        assert guard.referenced_file_texts(f"gh pr create --body-file {f}") == [
            "file text"
        ]

    def test_body_file_equals(self, tmp_path: Path) -> None:
        f = tmp_path / "b.md"
        f.write_text("file text")
        assert guard.referenced_file_texts(f"gh pr edit 5 --body-file={f}") == [
            "file text"
        ]

    def test_gh_api_field_at_file(self, tmp_path: Path) -> None:
        f = tmp_path / "b.md"
        f.write_text("api body")
        assert guard.referenced_file_texts(f"gh api /x -F body=@{f}") == ["api body"]

    def test_git_commit_message_file(self, tmp_path: Path) -> None:
        f = tmp_path / "msg.txt"
        f.write_text("commit msg")
        assert guard.referenced_file_texts(f"git commit -F {f}") == ["commit msg"]

    def test_missing_file_skipped(self) -> None:
        assert guard.referenced_file_texts("gh pr create --body-file /no/such") == []

    def test_plain_field_value_not_treated_as_path(self) -> None:
        assert guard.referenced_file_texts("gh api /x -F body=inline") == []
