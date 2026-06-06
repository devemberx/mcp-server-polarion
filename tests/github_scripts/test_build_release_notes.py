"""Unit tests for the release-notes CI helper in `.github/scripts/`.

The script lives outside the package and shells out to `gh`, so it is loaded by
path via importlib and `_gh` is monkeypatched to feed canned API responses. Only
the pure `tag_highlights` parser is covered; the `main()` stdout path is left to
the workflow itself.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "scripts"
    / "build_release_notes.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_release_notes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


brn = _load()


def _fake_gh(*, ref_object: dict[str, str], message: str | None = None):
    """Return a `_gh` stub: the ref lookup yields `ref_object`; the tag lookup
    (only reached for annotated tags) yields `message`."""

    def _gh(*args: str) -> str:
        url = args[-1]
        if "/git/ref/tags/" in url:
            return json.dumps({"object": ref_object})
        if "/git/tags/" in url:
            assert message is not None
            return json.dumps({"message": message})
        raise AssertionError(f"unexpected gh call: {args}")

    return _gh


class TestTagHighlights:
    def test_annotated_tag_returns_bullets(self, monkeypatch: pytest.MonkeyPatch):
        msg = "Release v1.2.0 (2026-06-05)\n\n- First thing\n- Second thing"
        monkeypatch.setattr(
            brn,
            "_gh",
            _fake_gh(ref_object={"type": "tag", "sha": "abc"}, message=msg),
        )
        assert brn.tag_highlights("o/r", "v1.2.0") == "- First thing\n- Second thing"

    def test_date_only_tag_is_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            brn,
            "_gh",
            _fake_gh(
                ref_object={"type": "tag", "sha": "abc"},
                message="Release v1.2.0 (2026-06-05)",
            ),
        )
        assert brn.tag_highlights("o/r", "v1.2.0") == ""

    def test_lightweight_tag_is_empty(self, monkeypatch: pytest.MonkeyPatch):
        # object.type == "commit" → no second API call, empty highlights.
        monkeypatch.setattr(
            brn,
            "_gh",
            _fake_gh(ref_object={"type": "commit", "sha": "abc"}),
        )
        assert brn.tag_highlights("o/r", "v1.2.0") == ""

    def test_trailing_blank_lines_stripped(self, monkeypatch: pytest.MonkeyPatch):
        msg = "Release v1.2.0 (2026-06-05)\n\n- Only thing\n\n"
        monkeypatch.setattr(
            brn,
            "_gh",
            _fake_gh(ref_object={"type": "tag", "sha": "abc"}, message=msg),
        )
        assert brn.tag_highlights("o/r", "v1.2.0") == "- Only thing"
