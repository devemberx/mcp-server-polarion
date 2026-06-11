"""HTML ↔ Markdown conversion for Polarion content fields.

Polarion stores rich-text fields (Work Item descriptions, Document
content) as HTML. Read path converts HTML→Markdown for the LLM
(token-efficient, structure-preserving); write path converts
Markdown→HTML and restricts pre-existing HTML to safe tags.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Final
from urllib.parse import quote

from bs4 import BeautifulSoup, Tag
from markdown_it import MarkdownIt
from markdownify import markdownify

ALLOWED_TAGS: Final[frozenset[str]] = frozenset(
    {
        "p",
        "br",
        "b",
        "i",
        "u",
        "strong",
        "em",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "table",
        "tr",
        "td",
        "th",
        "thead",
        "tbody",
        "a",
        "span",
        "div",
        "pre",
        "code",
    }
)

# Per-tag safe-attribute allowlist; tags absent permit none. Blocks on*
# handlers and other non-presentational attrs that enable stored XSS.
ALLOWED_ATTRS: Final[dict[str, frozenset[str]]] = {
    "a": frozenset({"href", "title"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan"}),
}

# Tags removed with their content (JS/CSS source, never visible text).
_DECOMPOSE_TAGS: Final[frozenset[str]] = frozenset({"script", "style"})

# Safe href schemes; others (javascript:, data:, vbscript:) stripped (XSS).
_SAFE_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "mailto"})

# Cap on cells per merge expansion (colspan*rowspan); bounds adversarial
# colspan=1000 rowspan=1000 (1M clones), covers realistic tables.
_MAX_CELLS_PER_MERGE: Final[int] = 10_000

# CommonMark + GFM tables. html_block/html_inline disabled so raw HTML in
# supplied Markdown can't bypass sanitization.
_md_renderer: Final[MarkdownIt] = (
    MarkdownIt("commonmark").disable(["html_block", "html_inline"]).enable("table")
)


def html_to_markdown(html: str) -> str:
    """Convert Polarion HTML to Markdown for LLM consumption.

    Preserves headings, lists, tables, and inline formatting as Markdown.

    Args:
        html: Raw HTML string from a Polarion content field.

    Returns:
        Markdown text; empty string for empty/whitespace input.
    """
    if not html or not html.strip():
        return ""
    expanded = _expand_merged_table_cells(html)
    rewritten = _fill_empty_img_alt(expanded)
    relinked = _render_polarion_rte_links(rewritten)
    result: str = markdownify(relinked, heading_style="ATX")
    return result.strip()


def _render_polarion_rte_links(html: str) -> str:
    """Convert ``span.polarion-rte-link`` placeholders to ``<a>`` tags.

    Polarion serialises rich-text refs as empty ``<span>`` with the target
    on ``data-*`` attrs; ``markdownify`` drops empty spans, losing the link.
    Lift the target into ``<a href="polarion:...">label</a>``.

    ``polarion:`` is intentionally absent from ``_SAFE_URL_SCHEMES``: if
    this Markdown round-trips through ``sanitize_html`` the href is
    stripped, keeping an unresolvable scheme out of write payloads.
    """
    if "polarion-rte-link" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for span in soup.find_all("span", class_="polarion-rte-link"):
        href, label = _resolve_rte_link(span)
        if not href:
            continue
        anchor = soup.new_tag("a", href=href)
        anchor.string = label
        span.replace_with(anchor)
    return str(soup)


def _resolve_rte_link(span: Tag) -> tuple[str, str]:
    """Return ``(href, label)`` for one ``polarion-rte-link`` span.

    ``("", "")`` when neither richPage nor work-item metadata is usable, so
    the caller leaves the span untouched. ``label`` is pre-escaped so
    ``[``, ``]``, ``\\`` survive Markdown link syntax.
    """
    inner_text = span.get_text(strip=True)
    data_type_raw = span.attrs.get("data-type", "")
    data_type = data_type_raw if isinstance(data_type_raw, str) else ""

    if data_type == "richPage":
        item_raw = span.attrs.get("data-item-name", "")
        space_raw = span.attrs.get("data-space-name", "")
        item_name = item_raw if isinstance(item_raw, str) else ""
        space_name = space_raw if isinstance(space_raw, str) else ""
        if not item_name:
            return ("", "")
        href = f"polarion:{quote(space_name, safe='')}/{quote(item_name, safe='')}"
        return (href, _escape_md_link_label(inner_text or item_name))

    item_id_raw = span.attrs.get("data-item-id", "")
    item_id = item_id_raw if isinstance(item_id_raw, str) else ""
    if item_id:
        scope_raw = span.attrs.get("data-scope", "")
        scope = scope_raw if isinstance(scope_raw, str) else ""
        # Bare ``polarion:workitem/MCPT-7`` resolves against current project;
        # ``polarion:project/Proj/workitem/MCPT-7`` carries cross-project scope.
        if scope:
            href = (
                f"polarion:project/{quote(scope, safe='')}"
                f"/workitem/{quote(item_id, safe='')}"
            )
        else:
            href = f"polarion:workitem/{quote(item_id, safe='')}"
        return (href, _escape_md_link_label(inner_text or item_id))

    return ("", "")


def _escape_md_link_label(text: str) -> str:
    """Backslash-escape characters that break Markdown link syntax.

    Unescaped ``[``/``]`` in anchor text collapses ``[label](href)``; a
    trailing ``\\`` swallows the closing bracket.
    """
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _fill_empty_img_alt(html: str) -> str:
    """Promote ``<img title>`` (or post-colon ``src`` filename) to ``alt``.

    Polarion stores attachments as ``<img src="attachment:NAME"/>`` with
    the filename on ``title``, making markdownify emit a label-less
    ``![](src)``.
    """
    if "<img" not in html.lower():
        return html
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        existing_alt = img.attrs.get("alt", "")
        if isinstance(existing_alt, str) and existing_alt.strip():
            continue
        src_raw = img.attrs.get("src", "")
        src = src_raw if isinstance(src_raw, str) else ""
        title_raw = img.attrs.get("title", "")
        title = title_raw if isinstance(title_raw, str) else ""
        label = title.strip()
        # Skip src-filename fallback for absolute URLs — post-colon segment
        # is host+path, not a filename.
        if not label and "://" not in src:
            _, sep, after = src.partition(":")
            label = after if sep else src
        if label:
            img["alt"] = label
        if "title" in img.attrs:
            del img.attrs["title"]
    return str(soup)


def _expand_merged_table_cells(html: str) -> str:
    """Rectangularize tables by duplicating ``colspan``/``rowspan`` cells.

    ``markdownify`` 1.2.2 renders colspan as empty cells and drops rowspan
    entirely, breaking GFM cell counts. Duplicate each spanned cell into
    every grid position it covers so the table is rectangular and emits
    valid GFM.
    """
    if "<table" not in html.lower():
        return html
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        _rectangularize_table(table)
    return str(soup)


def _rectangularize_table(table: Tag) -> None:
    """Replace one table's span attributes with duplicated cells.

    A cell at (row, col) with ``colspan=N rowspan=M`` becomes ``N*M`` copies
    at ``(row..row+M-1, col..col+N-1)``. Reservations from earlier rows shift
    later rows' cells rightward to keep the column index correct.
    """
    rows: list[Tag] = [
        tr for tr in table.find_all("tr") if tr.find_parent("table") is table
    ]
    if not rows:
        return

    # reservations[i][col]: a clone scheduled for (i, col) from a rowspan above.
    reservations: list[dict[int, Tag]] = [{} for _ in rows]

    for row_idx, row in enumerate(rows):
        original_cells: list[Tag] = [
            cell for cell in row.find_all(["td", "th"]) if cell.find_parent("tr") is row
        ]

        layout: dict[int, Tag] = dict(reservations[row_idx])
        col_idx = 0

        for cell in original_cells:
            colspan = _get_span(cell, "colspan")
            rowspan = _get_span(cell, "rowspan")

            # Bound expansion per merge; drop rowspan first (rows scarcer
            # than columns).
            if colspan * rowspan > _MAX_CELLS_PER_MERGE:
                rowspan = max(1, _MAX_CELLS_PER_MERGE // colspan)

            for attr in ("colspan", "rowspan"):
                if attr in cell.attrs:
                    del cell.attrs[attr]

            # Place original + colspan dupes one column at a time, skipping
            # columns reserved by a rowspan above (cell may land on
            # non-contiguous indices, as browsers do).
            placed_cols: list[int] = []
            for j in range(colspan):
                while col_idx in layout:
                    col_idx += 1
                layout[col_idx] = cell if j == 0 else _clone_cell(cell)
                placed_cols.append(col_idx)
                col_idx += 1

            for k in range(1, rowspan):
                target = row_idx + k
                if target >= len(rows):
                    break
                for placed_col in placed_cols:
                    reservations[target][placed_col] = _clone_cell(cell)

        # Rebuild in column order; clear() also drops inter-cell whitespace
        # (markdownify ignores it anyway).
        row.clear()
        for col in sorted(layout):
            row.append(layout[col])


def _get_span(cell: Tag, attr_name: str) -> int:
    """Return colspan/rowspan as int in ``[1, 1000]`` (mirrors markdownify).

    Missing, non-string, or non-numeric values fall back to 1.
    """
    raw = cell.attrs.get(attr_name)
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    if not isinstance(raw, str):
        return 1
    raw = raw.strip()
    if not raw.isdigit():
        return 1
    return max(1, min(1000, int(raw)))


def _clone_cell(cell: Tag) -> Tag:
    """Return a detached deep copy of ``cell`` with span attributes stripped.

    ``deepcopy`` yields a parent-less subtree attachable elsewhere. Span
    attrs removed defensively (``_rectangularize_table`` already strips them).
    """
    clone: Tag = deepcopy(cell)
    for attr in ("colspan", "rowspan"):
        if attr in clone.attrs:
            del clone.attrs[attr]
    return clone


def markdown_to_html(text: str) -> str:
    """Convert Markdown (or plain text) to Polarion-compatible HTML.

    ``markdown-it-py`` (CommonMark + GFM tables) handles tables, nested
    lists, headings, and inline formatting. Plain text is wrapped in ``<p>``.

    Args:
        text: Markdown or plain text supplied by the user or LLM.

    Returns:
        HTML for a Polarion ``description.value`` field; empty string for
        empty/whitespace input.
    """
    if not text or not text.strip():
        return ""
    result: str = _md_renderer.render(text)
    return result.strip()


def sanitize_html(html: str) -> str:
    """Remove disallowed HTML tags and attributes.

    Two removal strategies:

    * **Decompose** (tag + content): ``script``/``style`` — executable
      code/CSS, never visible text.
    * **Unwrap** (tag removed, content kept): all other disallowed tags
      (e.g. ``section``, ``font``), preserving visible text and children.

    Surviving tags keep only ``ALLOWED_ATTRS`` attributes (drops all
    ``on*`` handlers → stored XSS). ``href`` values are validated against
    ``_SAFE_URL_SCHEMES``; dangerous schemes (``javascript:``, ``data:``)
    have their ``href`` removed.

    Args:
        html: Raw HTML that may contain disallowed tags or attributes.

    Returns:
        HTML with only ``ALLOWED_TAGS``/``ALLOWED_ATTRS``; empty string for
        empty/whitespace input.
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Collect first — both decompose() and unwrap() mutate the tree in-place.
    disallowed: list[Tag] = [
        tag for tag in soup.find_all(True) if tag.name not in ALLOWED_TAGS
    ]
    for tag in disallowed:
        # A parent's decompose() already detached this tag; skip it.
        if tag.parent is None:
            continue
        if tag.name in _DECOMPOSE_TAGS:
            tag.decompose()
        else:
            tag.unwrap()

    # Fresh find_all visits only still-attached tags.
    for tag in soup.find_all(True):
        allowed_attrs: frozenset[str] = ALLOWED_ATTRS.get(tag.name, frozenset())
        for attr in list(tag.attrs):
            if attr not in allowed_attrs:
                del tag.attrs[attr]

    for anchor in soup.find_all("a", href=True):
        raw_href = anchor.get("href", "")
        href = raw_href if isinstance(raw_href, str) else ""
        href = href.strip()
        if ":" in href:
            scheme = href.split(":", maxsplit=1)[0].lower()
            if scheme not in _SAFE_URL_SCHEMES:
                del anchor["href"]

    return str(soup)


_BLOCK_TAGS_NEEDING_IDS: Final[frozenset[str]] = frozenset(
    {"p", "ul", "ol", "table", "div", "blockquote", "pre"}
)


def stamp_block_ids(html: str, prefix: str = "polarion_mcp") -> str:
    """Stamp unique ``id=`` on the block-level elements Polarion's
    ``/parts`` derivation requires.

    Polarion stores ``homePageContent`` verbatim and does not auto-assign
    ids. An anchorless ``<p>`` (or any ``_BLOCK_TAGS_NEEDING_IDS`` tag)
    saves fine but makes the next ``GET .../parts`` return HTTP 500.
    Heading tags (``<h1>..<h6>``) are skipped — Polarion rewrites their ids
    into the ``module-workitem`` macro on save. Existing non-blank ids are
    preserved; generated ids skip values already in the document so the
    PATCH never trips Polarion's HTTP 400 duplicate-id rule. Returns the
    input unchanged (verbatim, not reserialized) when every target block
    already carries a non-blank id, so an anchored round-trip body is never
    perturbed.

    Args:
        html: HTML string (typically ``sanitize_html`` output).
        prefix: Base for generated ids; final form ``"{prefix}_{N}"``.

    Returns:
        HTML with a unique ``id`` on every target block; empty string for
        empty input.
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "html.parser")
    used_ids: set[str] = set()
    for tagged in soup.find_all(id=True):
        existing = tagged.get("id")
        if isinstance(existing, str):
            used_ids.add(existing)

    counter = 0
    stamped = False
    for tag in soup.find_all(list(_BLOCK_TAGS_NEEDING_IDS)):
        existing = tag.get("id")
        if isinstance(existing, str) and existing.strip():
            continue
        while f"{prefix}_{counter}" in used_ids:
            counter += 1
        new_id = f"{prefix}_{counter}"
        tag["id"] = new_id
        used_ids.add(new_id)
        counter += 1
        stamped = True
    # Verbatim when nothing changed: str(soup) reserializes the whole string
    # (e.g. &nbsp; -> \xa0), which would corrupt an already-anchored round-trip body.
    return str(soup) if stamped else html


def first_anchorless_block(html: str) -> str | None:
    """Return the name of the first block lacking a non-empty ``id=``.

    Write-side counterpart to :func:`stamp_block_ids`: rejects anchorless
    blocks in raw ``update_document`` body HTML before they reach Polarion,
    since each makes the next ``GET .../parts`` return HTTP 500. Headings
    are exempt (Polarion rewrites their ids). ``None`` when every
    ``_BLOCK_TAGS_NEEDING_IDS`` block has an id, or input is empty.

    Stricter than ``stamp_block_ids``'s skip test: a whitespace-only ``id``
    counts as anchorless here. Erring toward rejection is the safe direction.
    """
    if not html or not html.strip():
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(list(_BLOCK_TAGS_NEEDING_IDS)):
        existing = tag.get("id")
        if not (isinstance(existing, str) and existing.strip()):
            return str(tag.name)
    return None
