"""`validate_issue` hook tests, loaded by path via importlib (script live
outside any package). Pure helpers only; `main()` left to e2e.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from tests.conftest import load_module_from_path

HOOKS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "hooks"

hook = load_module_from_path(HOOKS_DIR / "validate_issue.py", "validate_issue")

BUG_FORM = """\
name: Bug report
labels: ["bug"]
body:
  - type: markdown
    attributes:
      value: |
        Preamble text, no label.
  - type: textarea
    id: what-happened
    attributes:
      label: What happened?
    validations:
      required: true
  - type: input
    id: version
    attributes:
      label: Version
    validations:
      required: true
  - type: dropdown
    id: client
    attributes:
      label: MCP client
      options:
        - Claude Code
        - Other
    validations:
      required: true
  - type: textarea
    id: logs
    attributes:
      label: Relevant logs
    validations:
      required: false
"""

FOLLOW_UP_FORM = """\
name: Follow-up
labels: ["follow-up"]
body:
  - type: input
    id: origin
    attributes:
      label: Origin
    validations:
      required: true
  - type: textarea
    id: finding
    attributes:
      label: Finding
    validations:
      required: true
  - type: textarea
    id: suggested-fix
    attributes:
      label: Suggested fix
    validations:
      required: false
"""


@pytest.fixture
def template_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / ".github" / "ISSUE_TEMPLATE"
    d.mkdir(parents=True)
    (d / "bug_report.yml").write_text(BUG_FORM)
    (d / "follow_up.yml").write_text(FOLLOW_UP_FORM)
    # config.yml carry no labels/body — must not become a template entry.
    (d / "config.yml").write_text("blank_issues_enabled: true\n")
    monkeypatch.chdir(tmp_path)
    return d


class TestClassify:
    def test_create(self) -> None:
        assert hook.classify("gh issue create --title x --body y") == "create"

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh issue edit 5 --body y",
            "gh issue comment 5 --body y",
            "gh api -X PATCH /repos/o/r/issues/5 -f body=z",
            # Creation POST end path at /issues — no trailing slash.
            "gh api repos/o/r/issues -f title=x -f body=y",
        ],
    )
    def test_other(self, cmd: str) -> None:
        assert hook.classify(cmd) == "other"

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh issue list --label follow-up",
            "gh pr create --title x --body y",
            "gh pr comment 5 --body y",
            "gh api -X PATCH /repos/o/r/pulls/5 -f body=z",
            "git commit -m x",
        ],
    )
    def test_skip(self, cmd: str) -> None:
        assert hook.classify(cmd) is None


class TestExtractLabels:
    def test_long_flag(self) -> None:
        assert hook.extract_labels("gh issue create --label bug --body y") == ["bug"]

    def test_long_flag_equals(self) -> None:
        assert hook.extract_labels("gh issue create --label=bug") == ["bug"]

    def test_short_flag(self) -> None:
        assert hook.extract_labels("gh issue create -l bug") == ["bug"]

    def test_comma_separated(self) -> None:
        cmd = "gh issue create --label 'bug, follow-up'"
        assert hook.extract_labels(cmd) == ["bug", "follow-up"]

    def test_repeated_flags(self) -> None:
        cmd = "gh issue create -l bug --label 'good first issue'"
        assert hook.extract_labels(cmd) == ["bug", "good first issue"]

    def test_none(self) -> None:
        assert hook.extract_labels("gh issue create --title x --body y") == []


class TestExtractBody:
    def test_long_flag(self) -> None:
        assert hook.extract_body("gh issue create --body 'hello'") == "hello"

    def test_short_flag(self) -> None:
        assert hook.extract_body("gh issue create -b 'hello'") == "hello"

    def test_body_file_flag(self, tmp_path: Path) -> None:
        f = tmp_path / "b.md"
        f.write_text("from file")
        assert hook.extract_body(f"gh issue create --body-file {f}") == "from file"

    def test_short_body_file_flag(self, tmp_path: Path) -> None:
        f = tmp_path / "b.md"
        f.write_text("from file")
        assert hook.extract_body(f"gh issue create -F {f}") == "from file"

    def test_api_field_body(self) -> None:
        cmd = "gh api /repos/o/r/issues/5 -F body=hello"
        assert hook.extract_body(cmd) == "hello"

    def test_api_field_body_file(self, tmp_path: Path) -> None:
        f = tmp_path / "b.md"
        f.write_text("filed")
        assert hook.extract_body(f"gh api /repos/o/r/issues/5 -f body=@{f}") == "filed"

    def test_api_non_body_field_is_not_a_file(self) -> None:
        # Under gh api, -F stay field flag — never body-file shorthand.
        assert hook.extract_body("gh api /repos/o/r/issues/5 -F state=closed") is None

    def test_no_body(self) -> None:
        assert hook.extract_body("gh issue create --title only") is None


class TestExtractTitle:
    def test_long_flag(self) -> None:
        assert hook.extract_title("gh issue create --title 'follow up'") == "follow up"

    def test_long_flag_equals(self) -> None:
        assert hook.extract_title("gh issue create --title=x") == "x"

    def test_short_flag(self) -> None:
        assert hook.extract_title("gh issue create -t 'follow up' -b y") == "follow up"

    def test_absent(self) -> None:
        assert hook.extract_title("gh issue edit 5 --body y") is None


class TestLoadTemplateMap:
    def test_builds_label_to_required_fields(self, template_dir: Path) -> None:
        tmap = hook.load_template_map()
        # Optional fields (required: false) and markdown items stay out.
        assert tmap == {
            "bug": ["What happened?", "Version", "MCP client"],
            "follow-up": ["Origin", "Finding"],
        }

    def test_missing_dir_is_tolerant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)  # no .github/ here
        assert hook.load_template_map() == {}


class TestTemplateErrors:
    TMAP: ClassVar[dict[str, list[str]]] = {
        "bug": ["What happened?", "Version", "MCP client"],
        "follow-up": ["Origin", "Finding"],
    }

    def test_valid_follow_up_body(self) -> None:
        body = "### Origin\n\nPR #178\n\n### Finding\n\nx.py:1 — LOW — gap\n"
        assert hook.template_errors(["follow-up"], body, self.TMAP) == []

    def test_optional_heading_may_be_absent(self) -> None:
        # Suggested fix optional — its absence is fine.
        body = "### Origin\n\nPR #178\n\n### Finding\n\ndetail\n"
        assert hook.template_errors(["follow-up"], body, self.TMAP) == []

    def test_missing_required_heading(self) -> None:
        body = "### Origin\n\nPR #178\n"
        errs = hook.template_errors(["follow-up"], body, self.TMAP)
        assert any("### Finding" in e for e in errs)

    def test_heading_must_be_a_heading_line(self) -> None:
        # Field name inline in prose must not satisfy heading check.
        body = "### Origin\n\nPR #178\n\nsee Finding above\n"
        errs = hook.template_errors(["follow-up"], body, self.TMAP)
        assert any("### Finding" in e for e in errs)

    def test_no_mapped_label_lists_valid_ones(self) -> None:
        errs = hook.template_errors([], "### Origin\n", self.TMAP)
        assert len(errs) == 1
        assert "bug" in errs[0] and "follow-up" in errs[0]

    def test_unmapped_label_only_is_blocked(self) -> None:
        errs = hook.template_errors(["good first issue"], "x", self.TMAP)
        assert len(errs) == 1

    def test_two_mapped_labels_ambiguous(self) -> None:
        errs = hook.template_errors(["bug", "follow-up"], "x", self.TMAP)
        assert any("one" in e for e in errs)

    def test_extra_unmapped_label_is_free(self) -> None:
        body = "### Origin\n\nPR #178\n\n### Finding\n\ndetail\n"
        labels = ["follow-up", "good first issue"]
        assert hook.template_errors(labels, body, self.TMAP) == []

    def test_empty_template_map_is_tolerant(self) -> None:
        assert hook.template_errors([], "anything", {}) == []


class TestTitleErrors:
    def test_scope_and_summary(self) -> None:
        assert hook.title_errors("evals: model meta omission") == []

    def test_subscope(self) -> None:
        assert hook.title_errors("tools(attachments): re-measure upload") == []

    def test_missing_scope(self) -> None:
        errs = hook.title_errors("Re-measure upload on another instance")
        assert any("scope" in e for e in errs)

    def test_uppercase_scope_rejected(self) -> None:
        errs = hook.title_errors("Tools: re-measure upload")
        assert any("scope" in e for e in errs)

    def test_colon_without_space_rejected(self) -> None:
        errs = hook.title_errors("tools:re-measure upload")
        assert any("scope" in e for e in errs)

    def test_empty_summary_rejected(self) -> None:
        errs = hook.title_errors("tools: ")
        assert any("scope" in e for e in errs)

    def test_over_length_rejected(self) -> None:
        errs = hook.title_errors("evals(harness): " + "x" * 60)
        assert any("72" in e for e in errs)

    def test_at_length_limit_allowed(self) -> None:
        title = "evals(harness): " + "x" * (72 - len("evals(harness): "))
        assert len(title) == 72
        assert hook.title_errors(title) == []

    def test_trailing_period_rejected(self) -> None:
        errs = hook.title_errors("tools: re-measure upload.")
        assert any("period" in e for e in errs)

    def test_identifier_in_summary_allowed(self) -> None:
        assert hook.title_errors("evals(cases): loosen TRIG-DOC reject list") == []


class TestNonAsciiDetection:
    def test_blocks_korean(self) -> None:
        assert hook.has_disallowed_non_ascii("후속 작업")

    def test_allows_ascii_emoji_typographic(self) -> None:
        assert not hook.has_disallowed_non_ascii("follow-up 🚀 — id → name")
