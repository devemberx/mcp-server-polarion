"""Tests for ``utils/html.py`` — HTML ↔ Markdown conversion edge cases."""

from __future__ import annotations

import pytest

from mcp_server_polarion.utils.html import (
    ALLOWED_TAGS,
    html_to_markdown,
    markdown_to_html,
    sanitize_html,
)

# ---------------------------------------------------------------------------
# html_to_markdown
# ---------------------------------------------------------------------------


class TestHtmlToMarkdown:
    """Verify HTML → Markdown conversion."""

    def test_simple_paragraph(self) -> None:
        result = html_to_markdown("<p>Hello world</p>")
        assert "Hello world" in result

    def test_multiple_paragraphs(self) -> None:
        html = "<p>First</p><p>Second</p>"
        result = html_to_markdown(html)
        assert "First" in result
        assert "Second" in result

    def test_bold_preserved(self) -> None:
        html = "<p><strong>Bold text</strong></p>"
        result = html_to_markdown(html)
        assert "**Bold text**" in result

    def test_italic_preserved(self) -> None:
        html = "<p><em>italic text</em></p>"
        result = html_to_markdown(html)
        assert "*italic text*" in result

    def test_heading_preserved(self) -> None:
        html = "<h1>Title</h1><p>Body text</p>"
        result = html_to_markdown(html)
        assert "# Title" in result
        assert "Body text" in result

    def test_h2_preserved(self) -> None:
        html = "<h2>Subtitle</h2>"
        result = html_to_markdown(html)
        assert "## Subtitle" in result

    def test_unordered_list_preserved(self) -> None:
        html = "<ul><li>Item 1</li><li>Item 2</li></ul>"
        result = html_to_markdown(html)
        assert "Item 1" in result
        assert "Item 2" in result
        # Should use list markers (*, -, or +)
        lines = [line.strip() for line in result.splitlines() if line.strip()]
        assert any(line.startswith(("* ", "- ", "+ ")) for line in lines)

    def test_ordered_list_preserved(self) -> None:
        html = "<ol><li>First</li><li>Second</li></ol>"
        result = html_to_markdown(html)
        assert "First" in result
        assert "Second" in result

    def test_table_preserved(self) -> None:
        html = (
            "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
            "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
        )
        result = html_to_markdown(html)
        assert "A" in result
        assert "B" in result
        assert "|" in result

    def test_link_preserved(self) -> None:
        html = '<p>See <a href="https://example.com">this link</a></p>'
        result = html_to_markdown(html)
        assert "this link" in result
        assert "https://example.com" in result

    def test_code_preserved(self) -> None:
        html = "<p>Use <code>print()</code> function</p>"
        result = html_to_markdown(html)
        assert "`print()`" in result

    def test_br_tags(self) -> None:
        html = "<p>Line one<br/>Line two</p>"
        result = html_to_markdown(html)
        assert "Line one" in result
        assert "Line two" in result

    def test_empty_string(self) -> None:
        assert html_to_markdown("") == ""

    def test_whitespace_only(self) -> None:
        assert html_to_markdown("   \n\t  ") == ""

    def test_plain_text_passthrough(self) -> None:
        result = html_to_markdown("No HTML here")
        assert "No HTML here" in result

    def test_special_characters_preserved(self) -> None:
        html = "<p>Price: $100 &amp; tax &lt; 10%</p>"
        result = html_to_markdown(html)
        assert "$100" in result
        assert "&" in result

    def test_img_stripped(self) -> None:
        html = '<p>Before <img src="pic.jpg"/> After</p>'
        result = html_to_markdown(html)
        assert "Before" in result
        assert "After" in result
        assert "img" not in result.lower()

    def test_nested_formatting(self) -> None:
        html = "<p><strong><em>bold italic</em></strong></p>"
        result = html_to_markdown(html)
        assert "bold italic" in result


# ---------------------------------------------------------------------------
# markdown_to_html
# ---------------------------------------------------------------------------


class TestMarkdownToHtml:
    """Verify Markdown → HTML conversion."""

    def test_single_paragraph(self) -> None:
        result = markdown_to_html("Hello world")
        assert "<p>Hello world</p>" in result

    def test_multiple_paragraphs(self) -> None:
        text = "First paragraph\n\nSecond paragraph"
        result = markdown_to_html(text)
        assert "<p>First paragraph</p>" in result
        assert "<p>Second paragraph</p>" in result

    def test_heading(self) -> None:
        result = markdown_to_html("# Title")
        assert "<h1>Title</h1>" in result

    def test_h2(self) -> None:
        result = markdown_to_html("## Subtitle")
        assert "<h2>Subtitle</h2>" in result

    def test_bold(self) -> None:
        result = markdown_to_html("**bold text**")
        assert "<strong>bold text</strong>" in result

    def test_italic(self) -> None:
        result = markdown_to_html("*italic text*")
        assert "<em>italic text</em>" in result

    def test_unordered_list(self) -> None:
        text = "- Item 1\n- Item 2"
        result = markdown_to_html(text)
        assert "<ul>" in result
        assert "<li>Item 1</li>" in result
        assert "<li>Item 2</li>" in result

    def test_ordered_list(self) -> None:
        text = "1. First\n2. Second"
        result = markdown_to_html(text)
        assert "<ol>" in result
        assert "<li>First</li>" in result

    def test_nested_list_with_2_space_indent(self) -> None:
        """Critical test: LLMs produce 2-space indented nested lists."""
        text = "- Parent\n  - Child 1\n  - Child 2"
        result = markdown_to_html(text)
        # Must produce nested <ul>, not a flat list
        assert result.count("<ul>") == 2

    def test_table(self) -> None:
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = markdown_to_html(text)
        assert "<table>" in result
        assert "<th>A</th>" in result
        assert "<td>1</td>" in result

    def test_code_inline(self) -> None:
        result = markdown_to_html("Use `print()` function")
        assert "<code>print()</code>" in result

    def test_code_block(self) -> None:
        text = "```\ncode here\n```"
        result = markdown_to_html(text)
        assert "<code>" in result
        assert "code here" in result

    def test_link(self) -> None:
        result = markdown_to_html("[click](https://example.com)")
        assert 'href="https://example.com"' in result
        assert "click" in result

    def test_empty_string(self) -> None:
        assert markdown_to_html("") == ""

    def test_whitespace_only(self) -> None:
        assert markdown_to_html("   \n\t  ") == ""

    def test_plain_text_wrapped_in_p(self) -> None:
        result = markdown_to_html("Just plain text")
        assert "<p>Just plain text</p>" in result

    def test_only_newlines(self) -> None:
        assert markdown_to_html("\n\n\n") == ""

    def test_html_block_not_passed_through(self) -> None:
        """Raw HTML blocks in Markdown must be escaped, not injected as live HTML."""
        text = "<script>alert('xss')</script>\n\nSafe"
        result = markdown_to_html(text)
        # html_block rule is disabled → the tag is HTML-escaped, never a live element
        assert "<script>" not in result  # no executable script tag
        assert "&lt;script&gt;" in result  # tag is safely encoded as text

    def test_html_inline_not_passed_through(self) -> None:
        """Inline raw HTML in Markdown must be escaped, not injected as live HTML."""
        text = 'Click <a onclick="evil()">here</a>'
        result = markdown_to_html(text)
        # html_inline rule is disabled → inline HTML is HTML-escaped, not live
        assert "<a onclick=" not in result  # no executable attribute
        assert "&lt;a" in result  # tag is safely encoded as text


# ---------------------------------------------------------------------------
# sanitize_html
# ---------------------------------------------------------------------------


class TestSanitizeHtml:
    """Verify disallowed tag removal while preserving content."""

    def test_allowed_tags_preserved(self) -> None:
        html = "<p>Hello <strong>world</strong></p>"
        assert sanitize_html(html) == html

    def test_script_tag_and_content_removed(self) -> None:
        """script content (JS code) must be fully discarded, not leaked as text."""
        html = "<p><script>alert('xss')</script>Safe text</p>"
        result = sanitize_html(html)
        assert "<script>" not in result
        assert "alert" not in result  # JS code must not appear as visible text
        assert "Safe text" in result

    def test_style_tag_and_content_removed(self) -> None:
        """style content (CSS) must be fully discarded, not leaked as text."""
        html = "<style>.red{color:red}</style><p>Visible</p>"
        result = sanitize_html(html)
        assert "<style>" not in result
        assert "color:red" not in result  # CSS must not appear as visible text
        assert "Visible" in result

    def test_nested_disallowed_tags(self) -> None:
        html = "<div><section><p>Content</p></section></div>"
        result = sanitize_html(html)
        assert "<section>" not in result
        assert "<div>" in result
        assert "<p>Content</p>" in result

    def test_empty_string(self) -> None:
        assert sanitize_html("") == ""

    def test_whitespace_only(self) -> None:
        assert sanitize_html("   \n\t  ") == ""

    def test_all_allowed_tags_accepted(self) -> None:
        # Self-closing tags like <br> are rendered as <br/> by BS4
        self_closing = {"br"}
        for tag in ALLOWED_TAGS:
            if tag in self_closing:
                html = f"<{tag}/>"
                result = sanitize_html(html)
                assert f"<{tag}/>" in result
            else:
                html = f"<{tag}>content</{tag}>"
                result = sanitize_html(html)
                assert f"<{tag}>" in result

    def test_img_tag_unwrapped(self) -> None:
        html = '<p>Before <img src="pic.jpg"/> After</p>'
        result = sanitize_html(html)
        assert "<img" not in result
        assert "Before" in result
        assert "After" in result

    def test_iframe_tag_unwrapped(self) -> None:
        html = '<iframe src="https://evil.com"></iframe><p>OK</p>'
        result = sanitize_html(html)
        assert "<iframe" not in result
        assert "OK" in result

    def test_preserves_link_attributes(self) -> None:
        html = '<a href="https://example.com">Click</a>'
        result = sanitize_html(html)
        assert 'href="https://example.com"' in result
        assert "Click" in result

    def test_multiple_disallowed_tags(self) -> None:
        html = "<font><marquee>Text</marquee></font>"
        result = sanitize_html(html)
        assert "<font>" not in result
        assert "<marquee>" not in result
        assert "Text" in result


# ---------------------------------------------------------------------------
# Round-trip consistency
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Verify that Markdown → HTML → Markdown round-trips preserve content."""

    @pytest.mark.parametrize(
        "text",
        [
            "Simple line",
            "First paragraph\n\nSecond paragraph",
            "- Item 1\n- Item 2",
            "## Heading\n\nBody text",
        ],
    )
    def test_roundtrip_preserves_content(self, text: str) -> None:
        html = markdown_to_html(text)
        recovered = html_to_markdown(html)
        # All original words must appear in the round-tripped text
        for word in text.split():
            word_clean = word.strip("#-*")
            if word_clean:
                assert word_clean in recovered
