#!/usr/bin/env python3
"""Gate: prove a working-tree edit changed only comments and dev docstrings.

Diffs two ASTs (comments are invisible to them). Editable docstrings
(module/class/private-fn) are normalized away; @mcp.tool docstrings and
non-docstring literals like Field(description=...) are NOT — touching either
fails the gate, enforcing the shrink-mcp-tool-docs boundary.

Usage: assert_comment_only.py <path.py> [--against REF]

Exit 0 = comment/docstring-only (or new file, no baseline). 1 = code/protected
literal changed. 2 = usage error or unparseable source/baseline.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

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
        check=False,
    )
    if out.returncode != 0:
        raise FileNotFoundError(out.stderr.strip() or f"{ref}:{path} not in git")
    return out.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--against", default="HEAD")
    args = ap.parse_args()

    # Read working file first so a bad path is a usage error (exit 2), not a
    # silent "new file" pass; only then does a ref miss below mean a real new file.
    try:
        with Path(args.path).open(encoding="utf-8") as fh:
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
