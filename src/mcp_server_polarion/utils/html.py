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

# markdown-it-py renderer: CommonMark base + GFM tables.
_md_renderer: Final[MarkdownIt] = MarkdownIt("commonmark").enable("table")


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
    """Remove disallowed HTML tags while preserving their inner content.

    Tags not in ``ALLOWED_TAGS`` are *unwrapped* — the tag itself is
    removed but its children (text and nested elements) are kept in
    place.  This prevents injection of scripts, styles, or other
    dangerous elements into Polarion without losing visible content.

    Args:
        html: Raw HTML string that may contain disallowed tags.

    Returns:
        Sanitized HTML containing only tags from ``ALLOWED_TAGS``.
        Returns an empty string when given empty or whitespace-only input.
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Iterate over all tags; unwrap those that are not allowed.
    # We must collect tags first because unwrap() modifies the tree.
    disallowed: list[Tag] = [
        tag for tag in soup.find_all(True) if tag.name not in ALLOWED_TAGS
    ]
    for tag in disallowed:
        tag.unwrap()

    return str(soup)
