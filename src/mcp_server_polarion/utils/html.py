"""HTML ↔ Markdown conversion for Polarion content fields.

Polarion stores all rich-text fields (e.g. Work Item descriptions,
Document content) as HTML.  These utilities ensure:

* **Read path** — raw HTML is converted to Markdown before the LLM sees
  it (``html_to_markdown``).  Markdown preserves structural information
  (headings, lists, tables) while being far more token-efficient than
  raw HTML.
* **Write path** — LLM-generated Markdown is converted to
  Polarion-compatible HTML (``markdown_to_html``), and any pre-existing
  HTML is restricted to safe tags only (``sanitize_html``).
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

# Per-tag allowlist of safe attributes.  Tags absent from this map permit NO
# attributes.  This blocks on* event handlers (onclick, onerror, etc.) and any
# other non-presentational attributes that could enable stored XSS when Polarion
# renders the content in a browser.
ALLOWED_ATTRS: Final[dict[str, frozenset[str]]] = {
    "a": frozenset({"href", "title"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan"}),
}

# Tags whose entire content (text + children) must be removed, not just unwrapped.
# JS and CSS source code is never meaningful as visible Polarion text.
_DECOMPOSE_TAGS: Final[frozenset[str]] = frozenset({"script", "style"})

# URL schemes considered safe for href attributes.  Any other scheme
# (javascript:, data:, vbscript:, etc.) is stripped to prevent stored XSS.
_SAFE_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "mailto"})

# Upper bound on cells materialised per merged-cell expansion (colspan*rowspan).
# Bounds an adversarial colspan="1000" rowspan="1000" (1M clones) while still
# covering any realistic Polarion table.
_MAX_CELLS_PER_MERGE: Final[int] = 10_000

# markdown-it-py renderer: CommonMark base + GFM tables.
# html_block and html_inline are disabled so that raw HTML embedded in
# LLM/user-supplied Markdown cannot bypass sanitization.
_md_renderer: Final[MarkdownIt] = (
    MarkdownIt("commonmark").disable(["html_block", "html_inline"]).enable("table")
)


def html_to_markdown(html: str) -> str:
    """Convert Polarion HTML to Markdown for LLM consumption.

    Uses ``markdownify`` to translate HTML structure into Markdown
    syntax.  Headings, lists, tables, and inline formatting are
    preserved as Markdown equivalents, giving the LLM both semantic
    structure and token efficiency.

    Args:
        html: Raw HTML string from a Polarion content field.

    Returns:
        Markdown text with structural elements preserved.
        Returns an empty string when given empty or whitespace-only input.
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

    Polarion serialises rich-text references (to other documents or work
    items) as empty ``<span>`` elements whose target lives on ``data-*``
    attributes. ``markdownify`` drops empty spans entirely, silently losing
    the link. Lift the target into ``<a href="polarion:...">label</a>``
    so the downstream conversion emits ``[label](polarion:...)``.

    The ``polarion:`` scheme is intentionally absent from
    ``_SAFE_URL_SCHEMES``: if this synthesised Markdown ever round-trips
    through ``sanitize_html`` the href is stripped, preventing an
    unresolvable scheme from leaking into a write payload.
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

    Returns ``("", "")`` when neither richPage nor work-item metadata is
    usable, so the caller can leave the span untouched. The returned
    ``label`` is pre-escaped so that ``[``, ``]``, ``\\`` survive Markdown
    link syntax without collapsing the surrounding brackets.
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
        # Two distinct URI shapes keep the project segment unambiguous:
        # bare ``polarion:workitem/MCPT-7`` resolves against the current
        # project, while ``polarion:project/Proj/workitem/MCPT-7`` carries
        # the scope for cross-project references.
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
    """Backslash-escape characters that would break Markdown link syntax.

    ``markdownify`` writes anchor text verbatim into ``[label](href)``;
    unescaped ``[`` / ``]`` in the label collapses the link or invites a
    different reference, and a trailing ``\\`` swallows the closing bracket.
    """
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _fill_empty_img_alt(html: str) -> str:
    """Promote ``<img title>`` (or the post-colon ``src`` filename) to ``alt``.

    Polarion stores attachments as ``<img src="attachment:NAME"/>`` with
    the filename on ``title`` rather than ``alt``, which makes markdownify
    emit a label-less ``![](src)``. Lift the value into ``alt`` so the
    Markdown carries a readable label.
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
        # Skip the src-filename fallback for absolute URLs — the
        # post-colon segment is host+path, not a filename.
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

    ``markdownify`` 1.2.2 renders ``colspan`` extra columns as empty cells
    (losing the merged value's association with those columns) and silently
    drops ``rowspan`` entirely (producing GFM rows whose cell count disagrees
    with the header — a structurally broken table).  Pre-process the HTML so
    that every spanned cell is duplicated into each grid position it covered;
    the resulting table is rectangular and ``markdownify`` emits valid GFM.
    """
    if "<table" not in html.lower():
        return html
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        _rectangularize_table(table)
    return str(soup)


def _rectangularize_table(table: Tag) -> None:
    """Walk one ``<table>`` and replace span attributes with duplicated cells.

    The cell at logical position (row, col) carrying ``colspan=N rowspan=M``
    is replaced with ``N*M`` copies at consecutive positions
    ``(row..row+M-1, col..col+N-1)``.  Reservations from earlier rows shift
    later rows' fresh cells rightward so the column index stays correct.
    """
    rows: list[Tag] = [
        tr for tr in table.find_all("tr") if tr.find_parent("table") is table
    ]
    if not rows:
        return

    # reservations[i][col] holds a clone scheduled to occupy (i, col),
    # propagated from a rowspan in an earlier row.
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

            # Bound total expansion per merge to keep worst-case allocation
            # proportional to realistic table sizes.  Drop rowspan first
            # (rows are typically scarcer than columns in Polarion content).
            if colspan * rowspan > _MAX_CELLS_PER_MERGE:
                rowspan = max(1, _MAX_CELLS_PER_MERGE // colspan)

            for attr in ("colspan", "rowspan"):
                if attr in cell.attrs:
                    del cell.attrs[attr]

            # Place original + colspan duplicates one column at a time, skipping
            # columns already reserved by a rowspan above (so a cell may land on
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

        # Rebuild the row in column order; row.clear() drops original cells
        # plus any inter-cell whitespace, which markdownify ignores anyway.
        row.clear()
        for col in sorted(layout):
            row.append(layout[col])


def _get_span(cell: Tag, attr_name: str) -> int:
    """Return the colspan/rowspan as an int in ``[1, 1000]``.

    Mirrors ``markdownify``'s own clamp.  Missing, non-string, or
    non-numeric values fall back to 1.
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

    ``deepcopy`` on a ``bs4.Tag`` yields a fresh subtree (children, text,
    inline formatting) without a parent reference, so the copy can be
    attached elsewhere in the soup.  Span attributes are removed defensively
    even though ``_rectangularize_table`` already strips them on the source.
    """
    clone: Tag = deepcopy(cell)
    for attr in ("colspan", "rowspan"):
        if attr in clone.attrs:
            del clone.attrs[attr]
    return clone


def markdown_to_html(text: str) -> str:
    """Convert Markdown (or plain text) to Polarion-compatible HTML.

    Uses ``markdown-it-py`` (CommonMark + GFM tables) so that
    LLM-generated Markdown — including tables, nested lists with
    2-space indentation, headings, and inline formatting — is
    faithfully converted to HTML that Polarion can store and render.

    Plain text without any Markdown syntax is wrapped in ``<p>`` tags
    automatically.

    Args:
        text: Markdown or plain text supplied by the user or LLM.

    Returns:
        HTML string suitable for a Polarion ``description.value`` field.
        Returns an empty string when given empty or whitespace-only input.
    """
    if not text or not text.strip():
        return ""
    result: str = _md_renderer.render(text)
    return result.strip()


def sanitize_html(html: str) -> str:
    """Remove disallowed HTML tags and attributes from HTML.

    Two tag-removal strategies are applied:

    * **Decompose** (tag + all content removed): ``script`` and ``style`` tags.
      Their text content is executable code or CSS — never meaningful as visible
      Polarion text — so it must be discarded entirely.
    * **Unwrap** (tag removed, content kept): all other disallowed tags.
      Structural or presentational tags (e.g. ``section``, ``font``) are
      stripped while preserving their visible text and nested children.

    Additionally, attributes on surviving tags are restricted to the
    ``ALLOWED_ATTRS`` allowlist.  Any attribute not explicitly permitted
    (including all ``on*`` event handlers such as ``onclick`` and ``onerror``)
    is removed to prevent stored XSS when Polarion renders the content.

    ``href`` values are validated against a safe-protocol allowlist
    (``http``, ``https``, ``mailto``).  Links with dangerous schemes such
    as ``javascript:`` or ``data:`` have their ``href`` attribute removed.

    Args:
        html: Raw HTML string that may contain disallowed tags or attributes.

    Returns:
        Sanitized HTML containing only tags from ``ALLOWED_TAGS`` with only
        attributes from ``ALLOWED_ATTRS``.  Returns an empty string when given
        empty or whitespace-only input.
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Collect first — both decompose() and unwrap() mutate the tree in-place.
    disallowed: list[Tag] = [
        tag for tag in soup.find_all(True) if tag.name not in ALLOWED_TAGS
    ]
    for tag in disallowed:
        # A parent's decompose() removes the element and all its descendants
        # from the tree.  Skip tags that were already detached this way.
        if tag.parent is None:
            continue
        if tag.name in _DECOMPOSE_TAGS:
            tag.decompose()
        else:
            tag.unwrap()

    # Strip disallowed attributes from every surviving tag.
    # Iterating over a fresh find_all after the tag loop ensures we only visit
    # tags that are still attached to the tree.
    for tag in soup.find_all(True):
        allowed_attrs: frozenset[str] = ALLOWED_ATTRS.get(tag.name, frozenset())
        for attr in list(tag.attrs):
            if attr not in allowed_attrs:
                del tag.attrs[attr]

    # Validate href URLs against safe-protocol allowlist.
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
    """Stamp unique ``id=`` attributes on the block-level elements that
    Polarion's ``/parts`` derivation requires.

    Polarion's REST API stores ``homePageContent`` HTML verbatim and does
    not auto-assign ids the way the web editor does. An anchorless ``<p>``
    (or any other tag in ``_BLOCK_TAGS_NEEDING_IDS``) saves successfully
    but makes the next ``GET .../parts`` return HTTP 500. All heading
    tags (``<h1>..<h6>``) are skipped: Polarion rewrites their ids into
    the ``polarion_wiki macro name=module-workitem;params=id=MCPT-N``
    macro form on save anyway. Existing non-empty ``id=`` attributes are
    preserved so callers can pre-anchor specific blocks; generated ids
    skip any value already present in the document (including
    ``{prefix}_N`` ids the caller embedded via raw HTML in Markdown) so
    the PATCH never trips Polarion's HTTP 400 duplicate-id rule.

    Args:
        html: HTML string (typically the output of ``sanitize_html``).
        prefix: Base for generated ids; final form is ``"{prefix}_{N}"``.

    Returns:
        HTML with a unique ``id`` on every block-level element from the
        target set. Returns an empty string when given empty input.
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
    for tag in soup.find_all(list(_BLOCK_TAGS_NEEDING_IDS)):
        if tag.get("id"):
            continue
        while f"{prefix}_{counter}" in used_ids:
            counter += 1
        new_id = f"{prefix}_{counter}"
        tag["id"] = new_id
        used_ids.add(new_id)
        counter += 1
    return str(soup)


def first_anchorless_block(html: str) -> str | None:
    """Return the name of the first block element lacking a non-empty ``id=``.

    The write-side counterpart to :func:`stamp_block_ids`: the write side
    calls this on raw ``update_document`` body HTML to reject anchorless
    blocks before they reach Polarion, since each one makes the next
    ``GET .../parts`` return HTTP 500. Heading tags are exempt (Polarion
    rewrites their ids on save). Returns ``None`` when every block in
    ``_BLOCK_TAGS_NEEDING_IDS`` carries an id, or the input is empty.

    Deliberately stricter than the ``stamp_block_ids`` skip test: a
    whitespace-only ``id`` counts as anchorless here (it does not anchor the
    block for Polarion either), whereas ``stamp_block_ids`` leaves any truthy
    id untouched. The guard erring toward rejection is the safe direction.
    """
    if not html or not html.strip():
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(list(_BLOCK_TAGS_NEEDING_IDS)):
        existing = tag.get("id")
        if not (isinstance(existing, str) and existing.strip()):
            return str(tag.name)
    return None
