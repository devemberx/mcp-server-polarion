#!/usr/bin/env python3
"""Gate: prove a working-tree edit changed only comments and dev docstrings.

Comments are invisible to the AST, so any executable change — or an edit to a
non-docstring string literal such as ``Field(description=...)`` — survives into a
diff of the two ASTs. Module/class/private-function docstrings are normalized
away (they are this skill's editable surface), but ``@mcp.tool`` docstrings are
NOT normalized: touching one fails the gate, enforcing the shrink-tool-
descriptions boundary.

Usage:
    assert_comment_only.py <path.py> [--against REF]

Exit 0 = comment/docstring-only (or a new file with no baseline to diff).
Exit 1 = code or protected literal changed.
Exit 2 = usage error (path missing/unreadable) or unparseable source/baseline.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys

_PLACEHOLDER = "<docstring>"


def _is_mcp_tool(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
        if isinstance(target, ast.Name) and target.id == "tool":
            return True
    return False


def _normalize_docstrings(tree: ast.AST) -> ast.AST:
    """Replace editable docstrings with a placeholder; leave @mcp.tool ones intact."""
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_mcp_tool(
            node
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0].value.value = _PLACEHOLDER
    return tree


def _dump(src: str) -> str:
    return ast.dump(_normalize_docstrings(ast.parse(src)))


def _git_show(ref: str, path: str) -> str:
    out = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise FileNotFoundError(out.stderr.strip() or f"{ref}:{path} not in git")
    return out.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--against", default="HEAD")
    args = ap.parse_args()

    # Read the working file first: a missing/unreadable/unparseable path is a
    # usage error (exit 2), never a silent "no baseline" pass. Only once it reads
    # cleanly does a ref miss below mean a genuinely new file, not a typo'd path.
    try:
        with open(args.path, encoding="utf-8") as fh:
            new = _dump(fh.read())
    except (OSError, SyntaxError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        old = _dump(_git_show(args.against, args.path))
    except FileNotFoundError as exc:
        print(f"skip: new file, no baseline ({exc})", file=sys.stderr)
        return 0
    except SyntaxError as exc:
        print(f"error: baseline does not parse: {exc}", file=sys.stderr)
        return 2

    if old == new:
        print(f"ok: {args.path} — comment/docstring-only")
        return 0
    print(
        f"FAIL: {args.path} — executable code or a protected literal "
        f"(non-docstring string, @mcp.tool docstring) changed. Revert.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
