"""HTML ↔ Markdown for Polarion rich-text fields: read path HTML→Markdown
(token-efficient for the LLM), write path Markdown→HTML + tag sanitization.
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

# Per-tag attr allowlist (absent tag = none); blocks on* handlers → stored XSS.
ALLOWED_ATTRS: Final[dict[str, frozenset[str]]] = {
    "a": frozenset({"href", "title"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan"}),
}

# Tags removed with their content (JS/CSS source, never visible text).
_DECOMPOSE_TAGS: Final[frozenset[str]] = frozenset({"script", "style"})

# Safe href schemes; others (javascript:, data:, vbscript:) stripped (XSS).
_SAFE_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "mailto"})

# Bounds adversarial colspan*rowspan (1M clones); covers realistic tables.
_MAX_CELLS_PER_MERGE: Final[int] = 10_000

# html_block/html_inline disabled so raw HTML can't bypass sanitization.
_md_renderer: Final[MarkdownIt] = (
    MarkdownIt("commonmark").disable(["html_block", "html_inline"]).enable("table")
)


def html_to_markdown(html: str) -> str:
    """Polarion HTML → Markdown (headings/lists/tables/inline preserved);
    empty/whitespace input → empty string.
    """
    if not html or not html.strip():
        return ""
    expanded = _expand_merged_table_cells(html)
    rewritten = _fill_empty_img_alt(expanded)
    relinked = _render_polarion_rte_links(rewritten)
    result: str = markdownify(relinked, heading_style="ATX")
    return result.strip()


def _render_polarion_rte_links(html: str) -> str:
    """Lift ``span.polarion-rte-link`` (empty span, target on ``data-*``) into
    ``<a href="polarion:...">`` — markdownify drops empty spans, losing the
    link. ``polarion:`` is intentionally absent from ``_SAFE_URL_SCHEMES`` so a
    round-trip through ``sanitize_html`` strips the unresolvable href.
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
    """``(href, label)`` for one rte-link span; ``("", "")`` = unusable, caller
    leaves span untouched. Label pre-escaped for Markdown link syntax.
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
    """Escape ``[``/``]``/``\\`` — unescaped they collapse ``[label](href)``."""
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _fill_empty_img_alt(html: str) -> str:
    """Promote ``<img title>`` (or post-colon ``src`` filename) to ``alt`` —
    Polarion puts attachment filenames on ``title``, so markdownify emits a
    label-less ``![](src)``.
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
        # No src-filename fallback for absolute URLs (post-colon = host+path).
        if not label and "://" not in src:
            _, sep, after = src.partition(":")
            label = after if sep else src
        if label:
            img["alt"] = label
        if "title" in img.attrs:
            del img.attrs["title"]
    return str(soup)


def _expand_merged_table_cells(html: str) -> str:
    """Duplicate ``colspan``/``rowspan`` cells into every covered grid position
    — markdownify 1.2.2 renders colspan as empty cells and drops rowspan,
    breaking GFM cell counts.
    """
    if "<table" not in html.lower():
        return html
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        _rectangularize_table(table)
    return str(soup)


def _rectangularize_table(table: Tag) -> None:
    """Expand one table: ``colspan=N rowspan=M`` cell → ``N*M`` copies;
    reservations from earlier rows shift later cells rightward.
    """
    rows: list[Tag] = [
        tr for tr in table.find_all("tr") if tr.find_parent("table") is table
    ]
    if not rows:
        return

    # reservations[i][col]: clone scheduled by a rowspan above.
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

            # Over cap: drop rowspan first (rows scarcer than columns).
            if colspan * rowspan > _MAX_CELLS_PER_MERGE:
                rowspan = max(1, _MAX_CELLS_PER_MERGE // colspan)

            for attr in ("colspan", "rowspan"):
                if attr in cell.attrs:
                    del cell.attrs[attr]

            # Skip columns reserved by rowspans above — non-contiguous
            # placement, as browsers do.
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

        # clear() drops inter-cell whitespace (markdownify ignores it anyway).
        row.clear()
        for col in sorted(layout):
            row.append(layout[col])


def _get_span(cell: Tag, attr_name: str) -> int:
    """colspan/rowspan as int in ``[1, 1000]`` (mirrors markdownify); invalid → 1."""
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
    """Detached deep copy of ``cell``, span attrs stripped defensively."""
    clone: Tag = deepcopy(cell)
    for attr in ("colspan", "rowspan"):
        if attr in clone.attrs:
            del clone.attrs[attr]
    return clone


def markdown_to_html(text: str) -> str:
    """Markdown (or plain text) → Polarion-compatible HTML (CommonMark + GFM
    tables; plain text wrapped in ``<p>``); empty input → empty string.
    """
    if not text or not text.strip():
        return ""
    result: str = _md_renderer.render(text)
    return result.strip()


def sanitize_html(html: str) -> str:
    """Restrict HTML to ``ALLOWED_TAGS``/``ALLOWED_ATTRS``.

    ``script``/``style`` decomposed (content too — never visible text); other
    disallowed tags unwrapped (children kept). Surviving tags lose non-allowed
    attrs (kills ``on*`` → stored XSS); ``href`` outside ``_SAFE_URL_SCHEMES``
    (``javascript:``, ``data:``) removed.
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Collect first — decompose()/unwrap() mutate the tree in-place.
    disallowed: list[Tag] = [
        tag for tag in soup.find_all(True) if tag.name not in ALLOWED_TAGS
    ]
    for tag in disallowed:
        # Already detached by a parent's decompose().
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
    """Stamp unique ``id="{prefix}_{N}"`` on anchorless ``_BLOCK_TAGS_NEEDING_IDS``
    blocks — an anchorless block saves fine but makes the next ``GET .../parts``
    return HTTP 500. Headings skipped (Polarion rewrites their ids on save).
    Existing non-blank ids preserved; generated ids skip in-document values
    (Polarion 400s on duplicates). Input returned verbatim (not reserialized)
    when nothing needs stamping, so an anchored round-trip body is never
    perturbed.
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
    # str(soup) reserializes (&nbsp; -> \xa0): verbatim input when unstamped.
    return str(soup) if stamped else html


def first_anchorless_block(html: str) -> str | None:
    """Name of the first block lacking a non-empty ``id=`` (whitespace-only
    counts), or ``None``. Defensive counterpart to :func:`stamp_block_ids` —
    document tools run it post-stamping so a stamping regression cannot reach
    Polarion (anchorless block ⇒ ``GET .../parts`` HTTP 500).
    """
    if not html or not html.strip():
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(list(_BLOCK_TAGS_NEEDING_IDS)):
        existing = tag.get("id")
        if not (isinstance(existing, str) and existing.strip()):
            return str(tag.name)
    return None
