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

# Upper bound on cells materialised for a single merged-cell expansion
# (``colspan * rowspan``).  Per-attribute clamp is 1000, so without this
# total bound an adversarial ``colspan="1000" rowspan="1000"`` would yield
# 1M tag clones for a single cell.  10k cells comfortably accommodates any
# realistic Polarion table while keeping worst-case allocation bounded.
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
    result: str = markdownify(expanded, heading_style="ATX", strip=["img"])
    return result.strip()


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

            # Place the original + colspan duplicates one column at a time,
            # skipping positions already reserved by a rowspan from above.
            # The cell may therefore land at non-contiguous column indices —
            # the same behaviour browsers exhibit when a colspan is pushed
            # past a rowspan reservation.
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
