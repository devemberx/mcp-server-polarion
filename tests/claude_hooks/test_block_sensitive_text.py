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


class TestPatternPath:
    def test_resolves_under_project_dir_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hook cwd not guaranteed repo root — env root must win."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/repo/root")
        assert guard.pattern_path() == Path(
            "/repo/root/.claude/sensitive-patterns.local"
        )

    def test_no_env_falls_back_to_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        assert guard.pattern_path() == Path(".claude/sensitive-patterns.local")


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
            "gh repo edit o/r --description 'demo server'",
            "gh repo create demo --public --description 'demo server'",
            "gh label create leaked --description 'demo'",
            "gh workflow run deploy -f env=prod",
            "git commit -m 'x'",
            "git tag -a v1 -m x",
            "git push -u origin feat/private-slug",
            "git -C /some/worktree commit -m 'x'",
            "git -c user.email=a@b commit -m 'x'",
            "git -C /some/worktree push origin feat/x",
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
            "git log --oneline",
            "git -C /some/worktree status",
        ],
    )
    def test_local(self, cmd: str) -> None:
        assert not guard.outward(cmd)

    def test_env_prefix_git_commit_outward(self) -> None:
        assert guard.outward("env GIT_TRACE=1 git commit -m x")

    def test_pathological_options_linear_time(self) -> None:
        """CodeQL py/redos: dash-leading option runs must not backtrack —
        `-C -!` shape let value double as standalone option."""
        assert not guard.outward("git " + "-C -! " * 40 + "status")

    def test_unparseable_falls_back_to_regex(self) -> None:
        """Unbalanced quote break shlex — conservative regex approximation."""
        assert guard.outward("gh pr create --body 'unterminated")
        assert not guard.outward("grep 'unterminated")

    def test_separator_attached_outward(self) -> None:
        """Separator glued to neighbor token (`hi;gh`) must not hide command."""
        assert guard.outward("echo hi;gh pr create --body x")
        assert guard.outward("true &&gh pr create --body x")

    def test_quoted_separator_text_not_outward(self) -> None:
        """Quoted mention = data, not command."""
        assert not guard.outward("echo 'hi;gh pr create'")

    def test_wrapper_quoted_script_not_detected(self) -> None:
        """Known limitation (module docstring) — wrapper-quoted script =
        single data token, inner gh invisible."""
        assert not guard.outward("bash -c 'gh pr create --body x'")

    def test_xargs_unquoted_wrapper_detected(self) -> None:
        """xargs pass args as bare tokens — gh stay visible, unlike bash -c."""
        assert guard.outward("cat files.txt | xargs gh pr create --body x")


class TestLoadPatterns:
    def test_missing_file_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(guard, "pattern_path", lambda: tmp_path / "absent")
        assert guard.load_patterns() == []

    def test_comments_and_blanks_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "patterns"
        f.write_text("# comment\n\nSecretDoc\\b\n")
        monkeypatch.setattr(guard, "pattern_path", lambda: f)
        patterns = guard.load_patterns()
        assert [p.pattern for p in patterns] == ["SecretDoc\\b"]

    def test_invalid_regex_kept_as_literal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "patterns"
        f.write_text("Secret(Doc\n")
        monkeypatch.setattr(guard, "pattern_path", lambda: f)
        patterns = guard.load_patterns()
        assert len(patterns) == 1
        assert patterns[0].search("path/Secret(Doc/file")

    def test_unreadable_file_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(guard, "pattern_path", lambda: tmp_path)
        assert guard.load_patterns() is None

    def test_dangling_symlink_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard installed then link target moved — fail closed, not allow-all."""
        link = tmp_path / "patterns"
        link.symlink_to(tmp_path / "gone")
        monkeypatch.setattr(guard, "pattern_path", lambda: link)
        assert guard.load_patterns() is None

    def test_non_utf8_file_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Undecodable pattern file = broken install — fail closed, no crash."""
        f = tmp_path / "patterns"
        f.write_bytes(b"Secret\xff\xfe\n")
        monkeypatch.setattr(guard, "pattern_path", lambda: f)
        assert guard.load_patterns() is None


class TestFailClosedReason:
    def test_dangling_symlink_remedy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dangling link ≠ permission problem — remedy must say re-link."""
        link = tmp_path / "patterns"
        link.symlink_to(tmp_path / "gone")
        monkeypatch.setattr(guard, "pattern_path", lambda: link)
        assert "dangling symlink" in guard.fail_closed_reason()

    def test_unreadable_remedy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(guard, "pattern_path", lambda: tmp_path)
        assert "unreadable" in guard.fail_closed_reason()


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

    def test_pattern_in_command_and_file_reported_once(self, tmp_path: Path) -> None:
        f = tmp_path / "body.md"
        f.write_text("SecretDoc body")
        hits = guard.scan(
            f"gh pr create --title 'vs SecretDoc' --body-file {f}", self._patterns()
        )
        assert hits == ["SecretDoc\\b"]

    def test_substring_of_public_id_not_matched(self) -> None:
        """Word-boundary pattern must not hit distinct mixed-case public id."""
        hits = guard.scan(
            "gh pr create --body 'eval fixture SecretDocument_Project'",
            self._patterns(),
        )
        assert hits == []

    def test_empty_patterns_skip_file_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No patterns = allow-all — referenced-file I/O must be skipped."""

        def boom(cmd: str, cwd: str | None = None) -> list[str]:
            raise AssertionError("referenced_file_texts called")

        monkeypatch.setattr(guard, "referenced_file_texts", boom)
        assert guard.scan("gh pr create --body-file big.md", []) == []


class TestMask:
    def test_masked_preview(self) -> None:
        """Block message must not echo full private pattern back into context."""
        assert guard.mask("SecretDoc\\b") == "Se…(11 chars)"

    def test_inline_flag_prefix_stripped(self) -> None:
        """(?i)-style prefix carries no name chars — preview must skip it."""
        assert guard.mask("(?i)secretdoc\\b") == "se…(15 chars)"

    def test_short_pattern_fully_hidden(self) -> None:
        assert guard.mask("ab") == "…(2 chars)"


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

    def test_body_file_path_containing_equals(self, tmp_path: Path) -> None:
        """`=` in filename is still a path for long flags — only gh api
        -F/--field carries field=value syntax."""
        f = tmp_path / "a=b.md"
        f.write_text("eq path")
        assert guard.referenced_file_texts(f"gh pr create --body-file {f}") == [
            "eq path"
        ]

    def test_gh_api_field_at_file(self, tmp_path: Path) -> None:
        f = tmp_path / "b.md"
        f.write_text("api body")
        assert guard.referenced_file_texts(f"gh api /x -F body=@{f}") == ["api body"]

    def test_gh_api_field_long_form(self, tmp_path: Path) -> None:
        """--field = documented long spelling of -F, identical @file semantics."""
        f = tmp_path / "b.md"
        f.write_text("api body")
        assert guard.referenced_file_texts(f"gh api /x --field body=@{f}") == [
            "api body"
        ]

    def test_gh_api_field_long_form_equals(self, tmp_path: Path) -> None:
        f = tmp_path / "b.md"
        f.write_text("api body")
        assert guard.referenced_file_texts(f"gh api /x --field=body=@{f}") == [
            "api body"
        ]

    def test_gh_api_attached_short_form(self, tmp_path: Path) -> None:
        f = tmp_path / "b.md"
        f.write_text("api body")
        assert guard.referenced_file_texts(f"gh api /x -Fbody=@{f}") == ["api body"]

    def test_gh_api_bracket_key(self, tmp_path: Path) -> None:
        """gh api nested field syntax — canonical gist-via-API spelling."""
        f = tmp_path / "b.md"
        f.write_text("api body")
        cmd = f"gh api gists -F files[a.md][content]=@{f}"
        assert guard.referenced_file_texts(cmd) == ["api body"]

    def test_gh_api_input_file(self, tmp_path: Path) -> None:
        f = tmp_path / "req.json"
        f.write_text("api input")
        assert guard.referenced_file_texts(f"gh api /x --input {f}") == ["api input"]

    def test_git_commit_message_file(self, tmp_path: Path) -> None:
        f = tmp_path / "msg.txt"
        f.write_text("commit msg")
        assert guard.referenced_file_texts(f"git commit -F {f}") == ["commit msg"]

    def test_git_commit_attached_message_file(self, tmp_path: Path) -> None:
        f = tmp_path / "msg.txt"
        f.write_text("commit msg")
        assert guard.referenced_file_texts(f"git commit -F{f}") == ["commit msg"]

    def test_git_commit_file_path_containing_equals(self, tmp_path: Path) -> None:
        f = tmp_path / "v=1.txt"
        f.write_text("commit msg")
        assert guard.referenced_file_texts(f"git commit -F {f}") == ["commit msg"]

    def test_compound_commit_file_next_to_gh_api(self, tmp_path: Path) -> None:
        """Flags resolve per shell segment — gh api in a later segment must
        not swallow a git commit -F file in an earlier one."""
        f = tmp_path / "msg.txt"
        f.write_text("commit msg")
        cmd = f"git commit -F {f} && gh api /x -f body=z"
        assert guard.referenced_file_texts(cmd) == ["commit msg"]

    def test_gist_create_positional_files(self, tmp_path: Path) -> None:
        """gist publish file contents themselves — positional args are files."""
        a = tmp_path / "a.md"
        a.write_text("gist a")
        b = tmp_path / "b.md"
        b.write_text("gist b")
        assert guard.referenced_file_texts(f"gh gist create {a} {b}") == [
            "gist a",
            "gist b",
        ]

    @pytest.mark.parametrize("flag", ["-d", "--desc", "-f", "--filename"])
    def test_gist_create_value_flag_not_read(self, tmp_path: Path, flag: str) -> None:
        decoy = tmp_path / "decoy.md"
        decoy.write_text("decoy")
        f = tmp_path / "real.md"
        f.write_text("real")
        cmd = f"gh gist create {flag} {decoy} {f}"
        assert guard.referenced_file_texts(cmd) == ["real"]

    def test_gist_create_boolean_flag_not_consuming(self, tmp_path: Path) -> None:
        f = tmp_path / "a.md"
        f.write_text("gist a")
        assert guard.referenced_file_texts(f"gh gist create --public {f}") == ["gist a"]

    def test_gist_create_stops_at_separator(self, tmp_path: Path) -> None:
        """Local cat after && must stay unscanned — docstring promise."""
        a = tmp_path / "pub.md"
        a.write_text("gist a")
        b = tmp_path / "local.md"
        b.write_text("local notes")
        assert guard.referenced_file_texts(f"gh gist create {a} && cat {b}") == [
            "gist a"
        ]

    def test_gist_create_skips_redirect_target(self, tmp_path: Path) -> None:
        a = tmp_path / "pub.md"
        a.write_text("gist a")
        b = tmp_path / "out.txt"
        b.write_text("old output")
        assert guard.referenced_file_texts(f"gh gist create {a} > {b}") == ["gist a"]

    def test_gist_create_stdin_alone_reads_nothing(self) -> None:
        assert guard.referenced_file_texts("gh gist create -") == []

    def test_gist_create_stdin_redirect_read(self, tmp_path: Path) -> None:
        f = tmp_path / "pub.md"
        f.write_text("gist body")
        assert guard.referenced_file_texts(f"gh gist create - < {f}") == ["gist body"]

    def test_gist_create_implicit_stdin_redirect_read(self, tmp_path: Path) -> None:
        """gh read stdin when zero file args — `< file` publish without `-`."""
        f = tmp_path / "pub.md"
        f.write_text("gist body")
        assert guard.referenced_file_texts(f"gh gist create < {f}") == ["gist body"]

    def test_gh_api_field_stdin_at_redirect_read(self, tmp_path: Path) -> None:
        """`-F key=@-` = documented gh api stdin form — redirect must be scanned."""
        f = tmp_path / "secret.md"
        f.write_text("api body")
        cmd = f"gh api gists -F files[a.md][content]=@- < {f}"
        assert guard.referenced_file_texts(cmd) == ["api body"]

    def test_binary_referenced_file_scanned_lossy(self, tmp_path: Path) -> None:
        """Non-UTF8 asset must not crash hook (crash = exit 1 = fail open)."""
        f = tmp_path / "app.bin"
        f.write_bytes(b"\xff\xfe SecretDoc \xff")
        texts = guard.referenced_file_texts(f"gh release upload v1.0.0 {f}")
        assert len(texts) == 1
        assert "SecretDoc" in texts[0]

    def test_relative_path_resolved_against_payload_cwd(self, tmp_path: Path) -> None:
        """Hook cwd ≠ Bash session cwd — payload cwd must anchor relative paths."""
        f = tmp_path / "b.md"
        f.write_text("file text")
        texts = guard.referenced_file_texts(
            "gh pr create --body-file b.md", cwd=str(tmp_path)
        )
        assert texts == ["file text"]

    def test_tilde_path_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        f = tmp_path / "b.md"
        f.write_text("file text")
        assert guard.referenced_file_texts("gh pr create --body-file ~/b.md") == [
            "file text"
        ]

    def test_gist_edit_add_file(self, tmp_path: Path) -> None:
        f = tmp_path / "add.md"
        f.write_text("added")
        assert guard.referenced_file_texts(f"gh gist edit abc123 --add {f}") == [
            "added"
        ]

    def test_gist_edit_short_add_file(self, tmp_path: Path) -> None:
        f = tmp_path / "add.md"
        f.write_text("added")
        assert guard.referenced_file_texts(f"gh gist edit abc123 -a {f}") == ["added"]

    def test_git_tag_a_value_not_read(self, tmp_path: Path) -> None:
        """-a is gist-edit-scoped — git tag -a <name> must not read <name>."""
        f = tmp_path / "v1"
        f.write_text("tag target")
        assert guard.referenced_file_texts(f"git tag -a {f} -m x") == []

    def test_release_create_positional_asset(self, tmp_path: Path) -> None:
        """Release assets publish like gist files — text asset must be scanned."""
        f = tmp_path / "notes.md"
        f.write_text("asset text")
        cmd = f"gh release create v1.0.0 {f} --notes ok"
        assert guard.referenced_file_texts(cmd) == ["asset text"]

    def test_release_upload_positional_asset(self, tmp_path: Path) -> None:
        f = tmp_path / "report.txt"
        f.write_text("asset text")
        assert guard.referenced_file_texts(f"gh release upload v1.0.0 {f}") == [
            "asset text"
        ]

    def test_release_tag_and_flag_values_not_read(self, tmp_path: Path) -> None:
        """First release positional = tag name; value-flag args = metadata."""
        tag = tmp_path / "v1.0.0"
        tag.write_text("tag decoy")
        decoy = tmp_path / "title.md"
        decoy.write_text("title decoy")
        cmd = f"gh release create {tag} --title {decoy}"
        assert guard.referenced_file_texts(cmd) == []

    def test_stdin_body_file_redirect_read(self, tmp_path: Path) -> None:
        """`--body-file -` + `< file` ship the redirected file outward."""
        f = tmp_path / "body.md"
        f.write_text("redirected body")
        cmd = f"gh pr create --title x --body-file - < {f}"
        assert guard.referenced_file_texts(cmd) == ["redirected body"]

    def test_stdin_input_redirect_read(self, tmp_path: Path) -> None:
        f = tmp_path / "req.json"
        f.write_text("api input")
        assert guard.referenced_file_texts(f"gh api /x --input - < {f}") == [
            "api input"
        ]

    def test_missing_file_skipped(self) -> None:
        assert guard.referenced_file_texts("gh pr create --body-file /no/such") == []

    def test_plain_field_value_not_treated_as_path(self) -> None:
        assert guard.referenced_file_texts("gh api /x -F body=inline") == []

    def test_newline_compound_commit_file_next_to_gh_api(self, tmp_path: Path) -> None:
        """Newline = separator too — gh api on next line must not swallow
        git commit -F file above as api field."""
        f = tmp_path / "msg.txt"
        f.write_text("commit msg")
        cmd = f"git commit -F {f}\ngh api /x -f body=z"
        assert guard.referenced_file_texts(cmd) == ["commit msg"]

    def test_gist_create_attached_stdin_redirect_read(self, tmp_path: Path) -> None:
        """`<file` glued redirect publish same as `< file`."""
        f = tmp_path / "pub.md"
        f.write_text("gist body")
        assert guard.referenced_file_texts(f"gh gist create <{f}") == ["gist body"]

    def test_gist_create_attached_separator_stops_segment(self, tmp_path: Path) -> None:
        """`&&cat` glued separator must still end gist segment."""
        a = tmp_path / "pub.md"
        a.write_text("gist a")
        b = tmp_path / "local.md"
        b.write_text("local notes")
        cmd = f"gh gist create {a} &&cat {b}"
        assert guard.referenced_file_texts(cmd) == ["gist a"]

    def test_workflow_run_field_at_file(self, tmp_path: Path) -> None:
        """gh workflow run -F share gh api @file syntax — dispatch inputs
        publish on the run page."""
        f = tmp_path / "inputs.md"
        f.write_text("dispatch input")
        cmd = f"gh workflow run deploy -F notes=@{f}"
        assert guard.referenced_file_texts(cmd) == ["dispatch input"]

    def test_workflow_run_plain_field_not_path(self) -> None:
        assert guard.referenced_file_texts("gh workflow run deploy -F env=prod") == []

    def test_workflow_run_json_stdin_redirect_read(self, tmp_path: Path) -> None:
        """--json read dispatch inputs from stdin — redirect source scanned."""
        f = tmp_path / "inputs.json"
        f.write_text("dispatch input")
        cmd = f"gh workflow run deploy --json < {f}"
        assert guard.referenced_file_texts(cmd) == ["dispatch input"]

    def test_oversized_file_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unbounded read of huge release asset = hook stall — cap read."""
        monkeypatch.setattr(guard, "MAX_SCAN_BYTES", 8, raising=False)
        f = tmp_path / "big.bin"
        f.write_bytes(b"AAAAAAAA tail beyond cap")
        assert guard.referenced_file_texts(f"gh release upload v1.0.0 {f}") == [
            "AAAAAAAA"
        ]
