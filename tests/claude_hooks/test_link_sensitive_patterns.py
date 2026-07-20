"""`link_sensitive_patterns` hook tests, loaded by path via importlib
(script live outside any package). Pure helpers only; `main()` left to e2e.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import load_module_from_path

HOOKS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "hooks"

linker = load_module_from_path(
    HOOKS_DIR / "link_sensitive_patterns.py", "link_sensitive_patterns"
)


class TestWorktreeMainRoot:
    def test_main_checkout_none(self, tmp_path: Path) -> None:
        """Main checkout `.git` = directory — not a worktree."""
        (tmp_path / ".git").mkdir()
        assert linker.worktree_main_root(tmp_path) is None

    def test_worktree_resolves_main_root(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
        assert linker.worktree_main_root(wt) == main

    def test_submodule_layout_none(self, tmp_path: Path) -> None:
        """Submodule gitdir = .git/modules/... — not a worktree, skip."""
        wt = tmp_path / "sub"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {tmp_path}/.git/modules/sub\n")
        assert linker.worktree_main_root(wt) is None

    def test_no_git_marker_none(self, tmp_path: Path) -> None:
        assert linker.worktree_main_root(tmp_path) is None

    def test_garbage_git_file_none(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("not a gitdir line\n")
        assert linker.worktree_main_root(wt) is None


class TestEnsureLink:
    def _layout(self, tmp_path: Path, with_source: bool = True) -> tuple[Path, Path]:
        main = tmp_path / "main"
        (main / ".claude").mkdir(parents=True)
        if with_source:
            (main / ".claude" / "sensitive-patterns.local").write_text("Secret\\b\n")
        wt = tmp_path / "wt"
        (wt / ".claude").mkdir(parents=True)
        return wt, main

    def test_creates_relative_symlink(self, tmp_path: Path) -> None:
        wt, main = self._layout(tmp_path)
        message = linker.ensure_link(wt, main)
        target = wt / ".claude" / "sensitive-patterns.local"
        assert message is not None
        assert target.is_symlink()
        assert not Path(target.readlink()).is_absolute()
        assert target.read_text() == "Secret\\b\n"

    def test_idempotent_second_call_noop(self, tmp_path: Path) -> None:
        wt, main = self._layout(tmp_path)
        linker.ensure_link(wt, main)
        assert linker.ensure_link(wt, main) is None
        assert (wt / ".claude" / "sensitive-patterns.local").is_symlink()

    def test_no_source_noop(self, tmp_path: Path) -> None:
        wt, main = self._layout(tmp_path, with_source=False)
        assert linker.ensure_link(wt, main) is None
        assert not (wt / ".claude" / "sensitive-patterns.local").exists()

    def test_existing_regular_file_untouched(self, tmp_path: Path) -> None:
        """Worktree-local copy = deliberate — never overwrite."""
        wt, main = self._layout(tmp_path)
        target = wt / ".claude" / "sensitive-patterns.local"
        target.write_text("Local\\b\n")
        assert linker.ensure_link(wt, main) is None
        assert not target.is_symlink()
        assert target.read_text() == "Local\\b\n"

    def test_dangling_symlink_untouched(self, tmp_path: Path) -> None:
        """Dangling link = broken install — guard fail closed, no silent repair."""
        wt, main = self._layout(tmp_path)
        target = wt / ".claude" / "sensitive-patterns.local"
        target.symlink_to(tmp_path / "gone")
        assert linker.ensure_link(wt, main) is None
        assert target.is_symlink()
        assert not target.exists()
