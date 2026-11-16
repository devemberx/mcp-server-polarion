"""Body <-> blocks: top-level split + placeholder recognition mirror live
stored form (testdrive 2026-11-15).
"""

from __future__ import annotations

from evals.harness.fake_polarion.body import Block, parse, render, text_of
from evals.harness.fixtures import MACRO_PREFIX, heading_html, work_item_html


class TestParse:
    def test_placeholders_become_member_blocks(self) -> None:
        blocks = parse(heading_html("X-1", level=1) + work_item_html("X-2"))
        assert blocks == [
            Block("heading", "X-1", level=1),
            Block("workitem", "X-2"),
        ]

    def test_heading_level_is_tag_minus_one(self) -> None:
        assert parse(heading_html("X-1", level=0))[0].level == 0
        assert parse(f'<h4 id="{MACRO_PREFIX}X-1"></h4>')[0].level == 3

    def test_plain_heading_awaits_spawn(self) -> None:
        (block,) = parse('<h3 id="anything">Perf <b>goal</b></h3>')
        assert (block.kind, block.ref, block.level) == ("heading", "", 2)
        assert text_of(block.html) == "Perf goal"

    def test_normal_block_keyed_by_own_id_verbatim(self) -> None:
        (block,) = parse('<ul id="b-1"><li>a</li><li>b</li></ul>')
        assert block == Block("normal", "b-1", '<ul id="b-1"><li>a</li><li>b</li></ul>')

    def test_single_quoted_id(self) -> None:
        assert parse("<p id='q-1'>x</p>")[0].ref == "q-1"

    def test_anchorless_block_has_empty_ref(self) -> None:
        assert parse("<p>no id</p>")[0].ref == ""

    def test_nested_id_does_not_anchor_outer_block(self) -> None:
        assert parse('<div><span id="inner">x</span></div>')[0].ref == ""

    def test_text_between_blocks_dropped(self) -> None:
        blocks = parse('stray <p id="a">1</p>\n\n<p id="b">2</p> tail')
        assert [b.ref for b in blocks] == ["a", "b"]

    def test_void_and_self_closing_top_level_tags_are_blocks(self) -> None:
        blocks = parse('<hr id="h"><br/><img id="i" src="x"/>')
        assert [b.html for b in blocks] == [
            '<hr id="h">',
            "<br/>",
            '<img id="i" src="x"/>',
        ]

    def test_comment_and_stray_close_ignored(self) -> None:
        blocks = parse('<!-- <p id="ghost"> --></div><p id="a">x</p>')
        assert [b.ref for b in blocks] == ["a"]

    def test_unclosed_tail_kept_as_one_block(self) -> None:
        blocks = parse('<p id="a">x</p><div id="b"><p>open')
        assert [b.ref for b in blocks] == ["a", "b"]
        assert blocks[1].html == '<div id="b"><p>open'


class TestRender:
    def test_round_trip_of_stored_form(self) -> None:
        body = "\n".join(
            ['<p id="a">x</p>', heading_html("X-1", level=1), work_item_html("X-2")]
        )
        assert render(parse(body)) == body

    def test_part_ids_follow_live_naming(self) -> None:
        blocks = parse(
            f'<p id="a">x</p>{heading_html("X-1", 1)}{work_item_html("X-2")}'
        )
        assert [b.part_id for b in blocks] == ["a", "heading_X-1", "workitem_X-2"]
