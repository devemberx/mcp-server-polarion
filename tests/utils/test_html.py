"""Tests for ``utils/html.py`` — HTML ↔ Markdown conversion edge cases."""

from __future__ import annotations

import pytest

from mcp_server_polarion.utils.html import (
    ALLOWED_ATTRS,
    ALLOWED_TAGS,
    first_anchorless_block,
    html_to_markdown,
    markdown_to_html,
    sanitize_html,
    stamp_block_ids,
)


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
        assert "[this link](https://example.com)" in result

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

    def test_empty_alt_filled_from_title(self) -> None:
        """``<img title="X" src="...">`` (no alt) becomes ``![X](src)``.

        Polarion stores the upload filename in ``title``; promoting it to
        ``alt`` and dropping ``title`` keeps the rendered Markdown to a single
        canonical label without a redundant ``"X"`` inside the parentheses.
        """
        html = '<img src="attachment:1-shot.png" title="shot.png"/>'
        result = html_to_markdown(html)
        assert "![shot.png](attachment:1-shot.png)" in result
        assert '"shot.png"' not in result

    def test_empty_alt_filled_from_src_filename(self) -> None:
        """No alt, no title -> alt derived from the segment after ``:`` in src.

        Real Polarion descriptions occasionally carry attachment imgs without
        a title attribute (e.g. when pasted from the clipboard); the filename
        portion of ``src`` is the only readable label available.
        """
        html = (
            '<img src="attachment:1-screenshot-20260512-142738-1.png" '
            'style="max-width: 650px;"/>'
        )
        result = html_to_markdown(html)
        assert (
            "![1-screenshot-20260512-142738-1.png]"
            "(attachment:1-screenshot-20260512-142738-1.png)" in result
        )

    def test_existing_alt_preserved(self) -> None:
        """An img that already carries a non-empty alt is left alone."""
        html = '<img alt="My picture" src="workitemimg:1-foo.png" title="foo.png"/>'
        result = html_to_markdown(html)
        assert "![My picture](workitemimg:1-foo.png" in result

    def test_non_attachment_img_passes_through(self) -> None:
        """External imgs render verbatim — no attachment-prefix filtering."""
        html = '<p>Before <img src="https://example.com/pic.jpg"/> After</p>'
        result = html_to_markdown(html)
        assert "Before" in result
        assert "After" in result
        # Lock the exact emitted form: src is preserved on a bare ![](src) since
        # the external img carries no alt or title to promote.
        assert "![](https://example.com/pic.jpg)" in result

    def test_nested_formatting(self) -> None:
        html = "<p><strong><em>bold italic</em></strong></p>"
        result = html_to_markdown(html)
        assert "bold italic" in result


class TestHtmlToMarkdownMergedCells:
    """Tables with ``colspan``/``rowspan`` must produce rectangular GFM."""

    @staticmethod
    def _table_rows(markdown: str) -> list[list[str]]:
        rows: list[list[str]] = []
        for line in markdown.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            inner = stripped.strip("|")
            if set(inner.replace("|", "").strip()) <= {"-", " ", ":"}:
                continue  # GFM separator row
            rows.append([cell.strip() for cell in inner.split("|")])
        return rows

    def test_table_colspan_duplicates_value(self) -> None:
        html = (
            "<table><thead><tr><th>A</th><th>B</th><th>C</th></tr></thead>"
            '<tbody><tr><td colspan="2">Merged</td><td>Z</td></tr>'
            "<tr><td>1</td><td>2</td><td>3</td></tr></tbody></table>"
        )
        result = html_to_markdown(html)
        rows = self._table_rows(result)
        assert len(rows) == 3, result
        assert all(len(r) == 3 for r in rows), result
        assert rows[1] == ["Merged", "Merged", "Z"]
        assert rows[2] == ["1", "2", "3"]

    def test_table_rowspan_duplicates_value(self) -> None:
        html = (
            "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
            '<tbody><tr><td rowspan="2">Merged</td><td>X</td></tr>'
            "<tr><td>Y</td></tr></tbody></table>"
        )
        result = html_to_markdown(html)
        rows = self._table_rows(result)
        assert len(rows) == 3, result
        assert all(len(r) == 2 for r in rows), result
        assert rows[1] == ["Merged", "X"]
        assert rows[2] == ["Merged", "Y"]

    def test_table_colspan_and_rowspan(self) -> None:
        html = (
            "<table><thead><tr><th>H1</th><th>H2</th><th>H3</th></tr></thead>"
            "<tbody>"
            '<tr><td colspan="2" rowspan="2">Big</td><td>A</td></tr>'
            "<tr><td>B</td></tr>"
            "<tr><td>1</td><td>2</td><td>3</td></tr>"
            "</tbody></table>"
        )
        result = html_to_markdown(html)
        rows = self._table_rows(result)
        assert len(rows) == 4, result
        assert all(len(r) == 3 for r in rows), result
        assert rows[1] == ["Big", "Big", "A"]
        assert rows[2] == ["Big", "Big", "B"]
        assert rows[3] == ["1", "2", "3"]

    def test_table_rowspan_after_normal_cell(self) -> None:
        html = (
            "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
            "<tbody>"
            '<tr><td>P</td><td rowspan="2">Tall</td></tr>'
            "<tr><td>Q</td></tr>"
            "</tbody></table>"
        )
        result = html_to_markdown(html)
        rows = self._table_rows(result)
        assert len(rows) == 3, result
        assert all(len(r) == 2 for r in rows), result
        assert rows[1] == ["P", "Tall"]
        assert rows[2] == ["Q", "Tall"]

    def test_table_no_merge_unchanged(self) -> None:
        html = (
            "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
            "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
        )
        result = html_to_markdown(html)
        rows = self._table_rows(result)
        assert rows == [["A", "B"], ["1", "2"]], result

    def test_table_invalid_span_falls_back_to_one(self) -> None:
        html = (
            '<table><tbody><tr><td colspan="abc" rowspan="">X</td>'
            "<td>Y</td></tr></tbody></table>"
        )
        result = html_to_markdown(html)
        rows = self._table_rows(result)
        assert rows == [["X", "Y"]], result

    def test_table_rowspan_overflow_silently_clipped(self) -> None:
        # rowspan claims 5 rows but only 2 exist — extra reservations dropped.
        html = (
            "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
            '<tbody><tr><td rowspan="5">Tall</td><td>X</td></tr>'
            "<tr><td>Y</td></tr></tbody></table>"
        )
        result = html_to_markdown(html)
        rows = self._table_rows(result)
        assert len(rows) == 3, result
        assert all(len(r) == 2 for r in rows), result
        assert rows[2] == ["Tall", "Y"]

    def test_table_colspan_skips_rowspan_reservation(self) -> None:
        """A colspan cell pushed past a previous row's rowspan must not
        overwrite the reservation — it should land at non-contiguous columns
        (matching browser rendering)."""
        # Row 0: A | B(rowspan=2) | C
        # Row 1: D(colspan=2) — should occupy cols 0 and 2 (col 1 reserved)
        # Row 2: E | F | G
        html = (
            "<table><tbody>"
            '<tr><td>A</td><td rowspan="2">B</td><td>C</td></tr>'
            '<tr><td colspan="2">D</td></tr>'
            "<tr><td>E</td><td>F</td><td>G</td></tr>"
            "</tbody></table>"
        )
        result = html_to_markdown(html)
        rows = self._table_rows(result)
        assert len(rows) == 3, result
        assert all(len(r) == 3 for r in rows), result
        assert rows[0] == ["A", "B", "C"]
        # B's rowspan clone sits between D and D's duplicate.
        assert rows[1] == ["D", "B", "D"]
        assert rows[2] == ["E", "F", "G"]

    def test_table_merged_cell_preserves_inline_formatting(self) -> None:
        """deepcopy must replicate inline formatting tags into duplicates."""
        html = (
            "<table><tbody>"
            '<tr><td colspan="2"><strong>bold</strong></td><td>X</td></tr>'
            "<tr><td>1</td><td>2</td><td>3</td></tr>"
            "</tbody></table>"
        )
        result = html_to_markdown(html)
        # Markdownify renders <strong> as **bold**; both duplicates carry it.
        assert result.count("**bold**") == 2, result

    def test_nested_table_each_rectangularized(self) -> None:
        """Outer and inner tables expand merges independently."""
        html = (
            "<table><tbody>"
            "<tr><td>"
            "<table><tbody>"
            '<tr><td colspan="2">INNER</td></tr>'
            "<tr><td>x</td><td>y</td></tr>"
            "</tbody></table>"
            "</td>"
            '<td colspan="2">OUTER</td></tr>'
            "<tr><td>p</td><td>q</td><td>r</td></tr>"
            "</tbody></table>"
        )
        result = html_to_markdown(html)
        assert result.count("INNER") >= 2, result
        assert result.count("OUTER") >= 2, result

    def test_table_pathological_span_product_bounded(self) -> None:
        """colspan*rowspan is clamped to keep worst-case allocation bounded."""
        # Per-attr clamp is 1000 each — without the product clamp this would
        # try to materialise 1M tag clones.  With _MAX_CELLS_PER_MERGE=10000
        # it stays bounded; we just assert the call returns in reasonable
        # time and the first row carries the merged value.
        html = (
            '<table><tbody><tr><td colspan="1000" rowspan="1000">M</td>'
            "<td>Z</td></tr></tbody></table>"
        )
        result = html_to_markdown(html)
        rows = self._table_rows(result)
        # Sanity: call returned (would OOM/hang without clamp), first cell is M.
        assert rows, result
        assert rows[0][0] == "M"


class TestHtmlToMarkdownPolarionRteLinks:
    """``polarion-rte-link`` spans must surface as Markdown links."""

    def test_rich_page_link_uses_item_name_label(self) -> None:
        html = (
            '<p>See <span class="polarion-rte-link" data-type="richPage" '
            'data-item-name="Software Requirement Coverage" '
            'data-space-name="Design"></span>.</p>'
        )
        result = html_to_markdown(html)
        assert (
            "[Software Requirement Coverage]"
            "(polarion:Design/Software%20Requirement%20Coverage)"
        ) in result

    def test_rich_page_link_prefers_inner_text(self) -> None:
        html = (
            '<p><span class="polarion-rte-link" data-type="richPage" '
            'data-item-name="Coverage" data-space-name="Design">'
            "Click here</span></p>"
        )
        result = html_to_markdown(html)
        assert "[Click here](polarion:Design/Coverage)" in result

    def test_work_item_link_uses_item_id_label(self) -> None:
        html = (
            '<p>Linked: <span class="polarion-rte-link" '
            'data-item-id="MCPT-7"></span>.</p>'
        )
        result = html_to_markdown(html)
        assert "[MCPT-7](polarion:workitem/MCPT-7)" in result

    def test_work_item_link_with_inner_text(self) -> None:
        html = (
            '<p><span class="polarion-rte-link" data-item-id="MCPT-7">'
            "see ticket</span></p>"
        )
        result = html_to_markdown(html)
        assert "[see ticket](polarion:workitem/MCPT-7)" in result

    def test_korean_item_name_is_url_encoded(self) -> None:
        html = (
            '<p><span class="polarion-rte-link" data-type="richPage" '
            'data-item-name="설계 문서" data-space-name="Design"></span></p>'
        )
        result = html_to_markdown(html)
        # %EC%84%A4%EA%B3%84%20%EB%AC%B8%EC%84%9C == "설계 문서"
        assert "polarion:Design/%EC%84%A4%EA%B3%84%20%EB%AC%B8%EC%84%9C" in result

    def test_span_without_target_metadata_does_not_crash(self) -> None:
        # Span carries no usable target metadata — surrounding text must still render.
        html = '<p>Prefix <span class="polarion-rte-link">visible</span> suffix.</p>'
        result = html_to_markdown(html)
        assert "Prefix" in result
        assert "suffix." in result

    def test_no_rte_link_short_circuits(self) -> None:
        html = '<p><a href="https://example.com">x</a></p>'
        result = html_to_markdown(html)
        assert "[x](https://example.com)" in result

    def test_work_item_link_with_scope_uses_project_segment(self) -> None:
        """``data-scope`` becomes a ``project/<scope>/`` URI segment."""
        html = (
            '<p><span class="polarion-rte-link" '
            'data-item-id="MCPT-7" data-scope="OtherProj"></span></p>'
        )
        result = html_to_markdown(html)
        assert "[MCPT-7](polarion:project/OtherProj/workitem/MCPT-7)" in result

    def test_work_item_link_without_scope_keeps_bare_uri(self) -> None:
        """No ``data-scope`` keeps the bare ``polarion:workitem/<id>`` URI."""
        html = '<p><span class="polarion-rte-link" data-item-id="MCPT-7"></span></p>'
        result = html_to_markdown(html)
        assert "[MCPT-7](polarion:workitem/MCPT-7)" in result
        assert "project/" not in result

    def test_label_brackets_are_md_escaped(self) -> None:
        """``[`` / ``]`` inside the label must not collapse the link syntax."""
        html = (
            '<p><span class="polarion-rte-link" data-item-id="MCPT-1">'
            "see [draft]</span></p>"
        )
        result = html_to_markdown(html)
        assert "[see \\[draft\\]](polarion:workitem/MCPT-1)" in result

    def test_label_backslash_is_md_escaped(self) -> None:
        """A trailing ``\\`` in the label must be doubled to stay literal."""
        html = (
            '<p><span class="polarion-rte-link" data-item-id="MCPT-1">a\\b</span></p>'
        )
        result = html_to_markdown(html)
        assert "[a\\\\b](polarion:workitem/MCPT-1)" in result

    def test_rich_page_label_brackets_escaped(self) -> None:
        """Bracket escaping applies to richPage labels too."""
        html = (
            '<p><span class="polarion-rte-link" data-type="richPage" '
            'data-item-name="Bracket [Doc]" data-space-name="Design"></span></p>'
        )
        result = html_to_markdown(html)
        assert "[Bracket \\[Doc\\]]" in result
        assert "polarion:Design/Bracket%20%5BDoc%5D" in result


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

    def test_decompose_with_nested_disallowed_child_no_error(self) -> None:
        """decompose() removes a script subtree; the loop must not crash when it
        subsequently encounters the already-detached child tag (font inside script)."""
        html = "<p><script><font>nested</font></script>After</p>"
        # Should not raise ValueError; script + its child are silently dropped
        result = sanitize_html(html)
        assert "<script>" not in result
        assert "nested" not in result
        assert "After" in result

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

    def test_event_handler_stripped_from_allowed_tag(self) -> None:
        """on* event handlers must be removed even from allowed tags."""
        html = '<a href="https://example.com" onclick="evil()">Click</a>'
        result = sanitize_html(html)
        assert 'href="https://example.com"' in result  # safe attr kept
        assert "onclick" not in result  # event handler removed

    def test_disallowed_attr_stripped_from_allowed_tag(self) -> None:
        """Arbitrary non-allowlisted attributes are removed from allowed tags."""
        html = '<p class="red" style="color:red">Text</p>'
        result = sanitize_html(html)
        assert "class" not in result
        assert "style" not in result
        assert "Text" in result

    def test_allowed_attrs_constant_covers_a_href(self) -> None:
        """ALLOWED_ATTRS must permit 'href' on anchor tags."""
        assert "href" in ALLOWED_ATTRS.get("a", frozenset())

    def test_table_cell_span_attributes_preserved(self) -> None:
        """colspan/rowspan on td/th must be preserved as they control table layout."""
        html = '<table><tr><td colspan="2">Cell</td></tr></table>'
        result = sanitize_html(html)
        assert 'colspan="2"' in result

    def test_javascript_href_stripped(self) -> None:
        """javascript: URIs in href must be removed to prevent stored XSS."""
        html = "<a href=\"javascript:alert('xss')\">Click</a>"
        result = sanitize_html(html)
        assert "javascript:" not in result
        assert "Click" in result  # anchor text preserved

    def test_data_href_stripped(self) -> None:
        """data: URIs in href must be removed."""
        html = '<a href="data:text/html,<script>evil()</script>">Link</a>'
        result = sanitize_html(html)
        assert "data:" not in result
        assert "Link" in result

    def test_vbscript_href_stripped(self) -> None:
        """vbscript: URIs in href must be removed."""
        html = '<a href="vbscript:MsgBox">Link</a>'
        result = sanitize_html(html)
        assert "vbscript:" not in result

    def test_safe_http_href_preserved(self) -> None:
        """http:// and https:// hrefs must be preserved."""
        html = '<a href="https://example.com">Safe</a>'
        result = sanitize_html(html)
        assert 'href="https://example.com"' in result

    def test_mailto_href_preserved(self) -> None:
        """mailto: hrefs must be preserved."""
        html = '<a href="mailto:user@example.com">Email</a>'
        result = sanitize_html(html)
        assert 'href="mailto:user@example.com"' in result

    def test_relative_href_preserved(self) -> None:
        """Relative URLs (no scheme) must be preserved."""
        html = '<a href="/docs/readme">Docs</a>'
        result = sanitize_html(html)
        assert 'href="/docs/readme"' in result

    def test_multiple_disallowed_tags(self) -> None:
        html = "<font><marquee>Text</marquee></font>"
        result = sanitize_html(html)
        assert "<font>" not in result
        assert "<marquee>" not in result
        assert "Text" in result


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


class TestStampBlockIds:
    """Verify ``stamp_block_ids`` covers exactly the blocks Polarion's
    ``/parts`` derivation requires and leaves headings alone."""

    def test_each_block_tag_gets_unique_sequential_id(self) -> None:
        html = (
            "<p>p</p><ul><li>x</li></ul><ol><li>y</li></ol>"
            "<table><tbody><tr><td>c</td></tr></tbody></table>"
            "<div>d</div><blockquote>q</blockquote><pre>code</pre>"
        )
        result = stamp_block_ids(html)
        for i, tag in enumerate(["p", "ul", "ol", "table", "div", "blockquote", "pre"]):
            assert f'<{tag} id="polarion_mcp_{i}"' in result

    def test_headings_are_not_stamped(self) -> None:
        result = stamp_block_ids("<h1>a</h1><h2>b</h2><h3>c</h3><h4>d</h4>")
        for level in (1, 2, 3, 4):
            assert f"<h{level}>" in result
            assert f"<h{level} id=" not in result

    def test_existing_id_is_preserved(self) -> None:
        html = '<p id="existing">a</p><p>b</p>'
        result = stamp_block_ids(html)
        assert '<p id="existing">a</p>' in result
        # Counter starts at 0 even when the prior block had a caller-provided id.
        assert '<p id="polarion_mcp_0">b</p>' in result

    def test_existing_polarion_mcp_id_avoids_collision(self) -> None:
        """A pre-existing ``polarion_mcp_N`` anchor (e.g. raw HTML embedded
        in Markdown) must not be duplicated by the counter, otherwise
        Polarion rejects the PATCH with HTTP 400."""
        html = (
            '<p id="polarion_mcp_0">manual0</p>'
            "<p>auto-a</p>"
            '<p id="polarion_mcp_2">manual2</p>'
            "<p>auto-b</p>"
        )
        result = stamp_block_ids(html)
        assert '<p id="polarion_mcp_0">manual0</p>' in result
        assert '<p id="polarion_mcp_1">auto-a</p>' in result
        assert '<p id="polarion_mcp_2">manual2</p>' in result
        assert '<p id="polarion_mcp_3">auto-b</p>' in result

    def test_inline_elements_are_not_stamped(self) -> None:
        result = stamp_block_ids("<p>x <span>y</span> <strong>z</strong></p>")
        assert "<span>y</span>" in result
        assert "<strong>z</strong>" in result
        assert "<span id=" not in result
        assert "<strong id=" not in result

    def test_custom_prefix(self) -> None:
        result = stamp_block_ids("<p>x</p><p>y</p>", prefix="anchor")
        assert '<p id="anchor_0">x</p>' in result
        assert '<p id="anchor_1">y</p>' in result

    @pytest.mark.parametrize("value", ["", "   ", "\n\t"])
    def test_empty_or_whitespace_input_returns_empty(self, value: str) -> None:
        assert stamp_block_ids(value) == ""


class TestFirstAnchorlessBlock:
    """``first_anchorless_block`` is the write-side reject predicate; every
    block in ``_BLOCK_TAGS_NEEDING_IDS`` must carry a non-empty id."""

    @pytest.mark.parametrize("value", ["", "   ", "\n\t"])
    def test_empty_or_whitespace_input_is_none(self, value: str) -> None:
        assert first_anchorless_block(value) is None

    def test_headings_are_exempt(self) -> None:
        html = "<h1>a</h1><h2>b</h2><h3>c</h3><h4>d</h4><h5>e</h5><h6>f</h6>"
        assert first_anchorless_block(html) is None

    @pytest.mark.parametrize(
        "tag", ["p", "ul", "ol", "table", "div", "blockquote", "pre"]
    )
    def test_each_block_tag_without_id_is_flagged(self, tag: str) -> None:
        assert first_anchorless_block(f"<{tag}>x</{tag}>") == tag

    def test_block_with_id_passes(self) -> None:
        assert first_anchorless_block('<p id="a">x</p>') is None

    def test_all_blocks_anchored_passes(self) -> None:
        html = '<p id="a">x</p><ul id="b"><li>y</li></ul><div id="c">z</div>'
        assert first_anchorless_block(html) is None

    def test_returns_first_offender_in_document_order(self) -> None:
        # The <p> is anchored; the <ul> is not, so the <ul> is reported.
        html = '<p id="a">x</p><ul><li>y</li></ul>'
        assert first_anchorless_block(html) == "ul"

    def test_empty_id_is_anchorless(self) -> None:
        assert first_anchorless_block('<p id="">x</p>') == "p"

    def test_whitespace_only_id_is_anchorless(self) -> None:
        # Stricter than stamp_block_ids: a blank id does not anchor the block.
        assert first_anchorless_block('<p id="   ">x</p>') == "p"

    def test_nested_anchorless_block_is_caught(self) -> None:
        # An anchored outer block does not excuse an anchorless inner block.
        html = '<div id="outer"><p>inner</p></div>'
        assert first_anchorless_block(html) == "p"

    def test_mixed_anchored_and_anchorless_reports_the_gap(self) -> None:
        html = '<p id="a">ok</p><table><tr><td>x</td></tr></table>'
        assert first_anchorless_block(html) == "table"

    def test_inline_elements_do_not_count(self) -> None:
        # <span>/<strong> are not in the block set, so an anchored <p>
        # wrapping them passes even though the inline tags lack ids.
        assert first_anchorless_block('<p id="a">x <span>y</span></p>') is None
