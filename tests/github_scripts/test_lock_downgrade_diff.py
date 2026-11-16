"""uv.lock downgrade gate tests, loaded by path via importlib."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tests.conftest import load_module_from_path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "scripts"
    / "lock_downgrade_diff.py"
)

ldd = load_module_from_path(SCRIPT, "lock_downgrade_diff")


def lock_toml(*packages: tuple[str, str | None]) -> str:
    """uv.lock text; `None` version = dynamic-version entry (uv omit key)."""
    out = ["version = 1", "revision = 3", 'requires-python = ">=3.13"', ""]
    for name, version in packages:
        out += ["[[package]]", f'name = "{name}"']
        if version is not None:
            out.append(f'version = "{version}"')
        out += ['source = { registry = "https://pypi.org/simple" }', ""]
    return "\n".join(out)


def lock(*packages: tuple[str, str | None]) -> dict[str, object]:
    return tomllib.loads(lock_toml(*packages))


# PR #312 replay: litellm bump forced strands-agents back, which dragged mcp/fastmcp.
PR312_BASE = (
    ("fastmcp", "4.1.7"),
    ("litellm", "1.103.7"),
    ("mcp", "2.3.0"),
    ("strands-agents", "1.64.0"),
)
PR312_HEAD = (
    ("fastmcp", "4.1.1"),
    ("litellm", "1.109.1"),
    ("mcp", "2.1.1"),
    ("strands-agents", "1.61.1"),
)


def downgraded(base: dict[str, object], head: dict[str, object]) -> list[str]:
    found = ldd.find_downgrades(ldd.load_versions(base), ldd.load_versions(head))
    return [d.package for d in found]


class TestFindDowngrades:
    def test_bump_that_drags_others_down_is_flagged(self) -> None:
        assert downgraded(lock(*PR312_BASE), lock(*PR312_HEAD)) == [
            "fastmcp",
            "mcp",
            "strands-agents",
        ]

    def test_downgrade_carries_both_versions(self) -> None:
        [mcp] = ldd.find_downgrades(
            ldd.load_versions(lock(("mcp", "2.3.0"))),
            ldd.load_versions(lock(("mcp", "2.1.1"))),
        )
        assert (mcp.package, mcp.base, mcp.head) == ("mcp", ("2.3.0",), ("2.1.1",))

    def test_pure_upgrade_passes(self) -> None:
        assert (
            downgraded(lock(("litellm", "1.103.7")), lock(("litellm", "1.109.1"))) == []
        )

    def test_added_and_removed_packages_are_not_downgrades(self) -> None:
        base = lock(("filelock", "4.2.1"), ("mcp", "2.3.0"))
        head = lock(("keyring", "25.7.0"), ("mcp", "2.3.0"))
        assert downgraded(base, head) == []

    @pytest.mark.parametrize(
        ("old", "new", "want"),
        [
            ("1.9.0", "1.10.0", []),  # string compare would call this a downgrade
            ("1.10.0", "1.9.0", ["pkg"]),
            ("2.0.0", "2.0.0rc1", ["pkg"]),  # pre-release sorts before final
            ("2.9.0", "2.9.0.post1", []),
        ],
    )
    def test_versions_compare_pep440(self, old: str, new: str, want: list[str]) -> None:
        assert downgraded(lock(("pkg", old)), lock(("pkg", new))) == want

    def test_fork_split_adding_newer_version_passes(self) -> None:
        # Real #312 tiktoken: one entry became two marker forks, both >= base.
        base = lock(("tiktoken", "0.13.0"))
        head = lock(("tiktoken", "0.13.0"), ("tiktoken", "0.14.0"))
        assert downgraded(base, head) == []

    def test_fork_losing_its_newest_version_is_flagged(self) -> None:
        base = lock(("numpy", "1.26.4"), ("numpy", "2.2.0"))
        head = lock(("numpy", "1.26.4"))
        assert downgraded(base, head) == ["numpy"]

    def test_fork_gaining_older_version_is_flagged(self) -> None:
        # Some marker env now resolve below what every env had on base.
        base = lock(("numpy", "2.2.0"))
        head = lock(("numpy", "1.26.4"), ("numpy", "2.2.0"))
        assert downgraded(base, head) == ["numpy"]

    def test_dynamic_version_entry_is_skipped(self) -> None:
        base = lock(("my-project", None), ("mcp", "2.3.0"))
        head = lock(("my-project", None), ("mcp", "2.3.0"))
        assert downgraded(base, head) == []


class TestLoadVersions:
    @pytest.mark.parametrize("data", [{}, {"package": None}, {"package": {}}])
    def test_missing_package_list_rejected(self, data: dict[str, object]) -> None:
        # Real lock always hold root project entry; none = schema drift.
        with pytest.raises(ValueError, match="package"):
            ldd.load_versions(data)

    def test_invalid_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="not-a-version"):
            ldd.load_versions(lock(("mcp", "not-a-version")))


class TestMain:
    def write(self, path: Path, *packages: tuple[str, str | None]) -> Path:
        path.write_text(lock_toml(*packages))
        return path

    def run(
        self, tmp_path: Path, base: Path, head: Path, *flags: str
    ) -> tuple[int, Path]:
        summary = tmp_path / "summary.md"
        rc = ldd.main([str(base), str(head), "--summary", str(summary), *flags])
        return rc, summary

    def test_enforced_downgrade_exits_1_with_error_annotation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base = self.write(tmp_path / "base.lock", *PR312_BASE)
        head = self.write(tmp_path / "head.lock", *PR312_HEAD)
        rc, summary = self.run(tmp_path, base, head, "--enforce")
        out = capsys.readouterr().out
        assert rc == 1
        assert "::error title=Lock downgrade::mcp 2.3.0 -> 2.1.1" in out
        assert "| strands-agents | 1.64.0 | 1.61.1 |" in summary.read_text()

    def test_unenforced_downgrade_warns_and_exits_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base = self.write(tmp_path / "base.lock", ("mcp", "2.3.0"))
        head = self.write(tmp_path / "head.lock", ("mcp", "2.1.1"))
        rc, summary = self.run(tmp_path, base, head)
        out = capsys.readouterr().out
        assert rc == 0
        assert "::warning title=Lock downgrade::mcp 2.3.0 -> 2.1.1" in out
        assert "::error" not in out
        assert "| mcp | 2.3.0 | 2.1.1 |" in summary.read_text()

    def test_fork_versions_join_in_annotation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base = self.write(tmp_path / "base.lock", ("numpy", "2.2.0"))
        head = self.write(
            tmp_path / "head.lock", ("numpy", "1.26.4"), ("numpy", "2.2.0")
        )
        self.run(tmp_path, base, head, "--enforce")
        assert "numpy 2.2.0 -> 1.26.4, 2.2.0" in capsys.readouterr().out

    def test_no_downgrade_exits_0_even_enforced(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base = self.write(tmp_path / "base.lock", ("litellm", "1.103.7"))
        head = self.write(tmp_path / "head.lock", ("litellm", "1.109.1"))
        rc, summary = self.run(tmp_path, base, head, "--enforce")
        assert rc == 0
        assert "::" not in capsys.readouterr().out
        assert "No package versions go down." in summary.read_text()

    @pytest.mark.parametrize("content", [None, "", "not = [valid toml"])
    def test_unreadable_lock_fails_closed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        content: str | None,
    ) -> None:
        base = self.write(tmp_path / "base.lock", ("mcp", "2.3.0"))
        head = tmp_path / "head.lock"
        if content is not None:
            head.write_text(content)
        rc, summary = self.run(tmp_path, base, head)
        assert rc == 2
        assert (
            "::error title=Lock downgrade::unreadable lock" in capsys.readouterr().out
        )
        assert "Unreadable lock" in summary.read_text()
