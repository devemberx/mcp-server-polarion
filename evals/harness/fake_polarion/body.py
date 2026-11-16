"""Document body (``homePageContent``) <-> ordered blocks.

Live 2026-11-15: body = whole document; ``/parts`` = 1:1 split of body's
top-level elements in order. Work items sit in body as empty macro
placeholders (heading text = work item title), everything else = ``normal``
part addressed by its own ``id`` attribute.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Literal

from ..fixtures import MACRO_PREFIX, heading_html, work_item_html

BlockKind = Literal["heading", "workitem", "normal"]

# Comment alternative first so ``<!-- <p> -->`` never count as tag.
_TAG = re.compile(r"<!--.*?-->|<(/?)([A-Za-z][A-Za-z0-9]*)\b[^>]*?(/?)>", re.DOTALL)
_ID = re.compile(r"""\bid\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_HEADING_TAG = re.compile(r"h([1-6])")
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"}
    | {"param", "source", "track", "wbr"}
)


@dataclass
class Block:
    """One top-level body element. ``ref`` = work item short id (heading /
    workitem) or element ``id`` (normal; "" = anchorless). Heading parsed from
    plain ``<hN>`` carry ``ref=""`` + source ``html`` until world assign id.
    """

    kind: BlockKind
    ref: str
    html: str = ""
    level: int = 0  # heading only: tag hN -> N - 1

    @property
    def part_id(self) -> str:
        return self.ref if self.kind == "normal" else f"{self.kind}_{self.ref}"

    def render(self) -> str:
        if self.kind == "heading":
            return heading_html(self.ref, self.level)
        if self.kind == "workitem":
            return work_item_html(self.ref)
        return self.html


def parse(body: str) -> list[Block]:
    """Split *body* into blocks; text between top-level elements dropped."""
    blocks: list[Block] = []
    for markup in _top_level(body):
        name = re.match(r"<([A-Za-z][A-Za-z0-9]*)", markup)
        tag = name.group(1).lower() if name else ""
        heading = _HEADING_TAG.fullmatch(tag)
        attr_id = _opening_id(markup)
        if attr_id.startswith(MACRO_PREFIX):
            ref = attr_id.removeprefix(MACRO_PREFIX)
            if heading:
                blocks.append(Block("heading", ref, level=int(heading.group(1)) - 1))
            else:
                blocks.append(Block("workitem", ref))
        elif heading:
            blocks.append(Block("heading", "", markup, int(heading.group(1)) - 1))
        else:
            blocks.append(Block("normal", attr_id, markup))
    return blocks


def render(blocks: list[Block]) -> str:
    return "\n".join(block.render() for block in blocks)


def text_of(markup: str) -> str:
    """Visible text of *markup* (spawned heading title)."""
    return html.unescape(re.sub(r"<[^>]+>", "", markup)).strip()


def _opening_id(markup: str) -> str:
    match = _ID.search(markup[: markup.find(">") + 1])
    if match is None:
        return ""
    return match.group(1) if match.group(1) is not None else match.group(2)


def _top_level(body: str) -> list[str]:
    """Outer markup of each depth-0 element; unclosed tail = one block."""
    out: list[str] = []
    depth = 0
    start = 0
    for match in _TAG.finditer(body):
        closing, name, self_closing = match.groups()
        if name is None:
            continue  # comment
        if closing:
            if depth == 0:
                continue  # stray close tag
            depth -= 1
            if depth == 0:
                out.append(body[start : match.end()])
        elif self_closing or name.lower() in _VOID:
            if depth == 0:
                out.append(match.group(0))
        else:
            if depth == 0:
                start = match.start()
            depth += 1
    if depth:
        out.append(body[start:])
    return out
