"""Gate uv.lock package downgrades between PR base and head (#317).

Dependabot run `uv lock --upgrade-package X==V`; uv treat other locked
versions as preference, not constraint, so forced bump may roll any package
back in any group (#312: litellm up dragged strands-agents, mcp, fastmcp
down). osv audit see only prod CVEs; this compare every locked version.

Exit 0 = pass, 1 = downgrade under `--enforce`, 2 = unreadable lock (fail closed).
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path
from typing import NamedTuple

from packaging.version import InvalidVersion, Version


class Downgrade(NamedTuple):
    package: str
    base: tuple[str, ...]
    head: tuple[str, ...]


def load_versions(data: object) -> dict[str, list[Version]]:
    """Name -> sorted versions; marker forks give one name several entries."""
    packages = data.get("package") if isinstance(data, dict) else None
    if not isinstance(packages, list):
        raise ValueError("lock has no [[package]] list")
    versions: dict[str, list[Version]] = {}
    for pkg in packages:
        if not isinstance(pkg, dict) or "version" not in pkg:
            continue  # dynamic-version project: uv omit key
        raw = str(pkg["version"])
        try:
            versions.setdefault(str(pkg.get("name")), []).append(Version(raw))
        except InvalidVersion as exc:
            raise ValueError(f"invalid version {raw!r}") from exc
    return {name: sorted(vs) for name, vs in versions.items()}


def find_downgrades(
    base: dict[str, list[Version]], head: dict[str, list[Version]]
) -> list[Downgrade]:
    """Shared names whose newest or oldest fork version drop.
    Newest drop = some env lost newer release; oldest drop = some env now
    resolve below every base env. Added/removed packages ignored.
    Blind spot: middle fork of 3+ (base 1.0/2.0 -> head 1.0/1.5/2.0) and env
    swap; entries carry no stable env key across fork-set change.
    """
    found: list[Downgrade] = []
    for name in sorted(base.keys() & head.keys()):
        old, new = base[name], head[name]
        if new[-1] < old[-1] or new[0] < old[0]:
            found.append(Downgrade(name, tuple(map(str, old)), tuple(map(str, new))))
    return found


def _describe(d: Downgrade) -> str:
    return f"{d.package} {', '.join(d.base)} -> {', '.join(d.head)}"


def render(downgrades: list[Downgrade], *, enforce: bool) -> str:
    lines = ["## Lock Downgrades (uv.lock)", ""]
    if not downgrades:
        lines.append("No package versions go down.")
        return "\n".join(lines) + "\n"
    mode = "blocking" if enforce else "warning only"
    lines += [
        f"**{len(downgrades)} package(s) go down** ({mode})",
        "",
        "Floor the package that forced it (a dependency-group floor or "
        "`[tool.uv] constraint-dependencies`), then let Dependabot rebase.",
        "",
        "| Package | Base | Head |",
        "|---|---|---|",
    ]
    lines += [
        f"| {d.package} | {', '.join(d.base)} | {', '.join(d.head)} |"
        for d in downgrades
    ]
    return "\n".join(lines) + "\n"


def _load(path: Path) -> dict[str, list[Version]]:
    return load_versions(tomllib.loads(path.read_text()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate uv.lock downgrades.")
    parser.add_argument("base", type=Path, help="uv.lock of PR base")
    parser.add_argument("head", type=Path, help="uv.lock of PR head")
    parser.add_argument("--enforce", action="store_true", help="fail on downgrade")
    parser.add_argument("--summary", type=Path, default=Path("lock-summary.md"))
    args = parser.parse_args(argv)
    try:
        downgrades = find_downgrades(_load(args.base), _load(args.head))
    except (OSError, ValueError) as exc:  # TOMLDecodeError subclass ValueError
        print(f"::error title=Lock downgrade::unreadable lock: {exc}")
        args.summary.write_text(
            f"## Lock Downgrades (uv.lock)\n\nUnreadable lock: {exc}\n"
        )
        return 2
    args.summary.write_text(render(downgrades, enforce=args.enforce))
    level = "error" if args.enforce else "warning"
    for d in downgrades:
        print(f"::{level} title=Lock downgrade::{_describe(d)}")
    return 1 if downgrades and args.enforce else 0


if __name__ == "__main__":
    raise SystemExit(main())
