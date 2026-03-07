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
    result: str = markdownify(html, heading_style="ATX", strip=["img"])
    return result.strip()


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

    return str(soup)
