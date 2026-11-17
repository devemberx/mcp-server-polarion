"""World write semantics, pinned to live testdrive probes (2026-11-15/16)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from evals.harness.fake_polarion.world import Fault, World
from evals.harness.fixtures import (
    CHILD_REQ_ID,
    DOC,
    DOC_HEADING_ID,
    FLOATING_HEADING_ID,
    FLOATING_TASK_ID,
    MODULE_ID,
    PARENT_DOC,
    PARENT_MODULE_ID,
    PARENT_REQ_ID,
    PROJECT,
    SECTION_A_PART_ID,
    SEEDS,
    SPACE,
    TEST_RUN_ID,
    TEST_RUN_TEMPLATE_ID,
    TESTCASE_ID,
    UNCOVERED_REQ_ID,
    UNRECORDED_TESTCASE_ID,
    Attachment,
    Document,
    heading_html,
    work_item_html,
)


@pytest.fixture
def world() -> World:
    return World.from_seeds(SEEDS)


def _parts(world: World, name: str = DOC) -> list[str]:
    return [b.part_id for b in world.document(SPACE, name).blocks]


def _fault(excinfo: pytest.ExceptionInfo[Fault]) -> tuple[int, str]:
    return excinfo.value.status, excinfo.value.detail


class TestSeeding:
    def test_worlds_are_independent_copies(self) -> None:
        first, second = World.from_seeds(SEEDS), World.from_seeds(SEEDS)
        first.work_item(FLOATING_TASK_ID).attributes["title"] = "changed"
        assert second.work_item(FLOATING_TASK_ID).attributes["title"] == "Floating task"
        assert SEEDS.work_items[FLOATING_TASK_ID].title == "Floating task"

    def test_placeholder_of_non_member_seed_rejected(self) -> None:
        bad = Document("Bad", "Bad", work_item_html(FLOATING_TASK_ID))
        seeds = replace(SEEDS, documents={**SEEDS.documents, "Bad": bad})
        with pytest.raises(ValueError, match="no member"):
            World.from_seeds(seeds)

    def test_unrecorded_test_case_has_no_record_in_any_run(self, world: World) -> None:
        # TRIG-CREATE-TEST-RECORDS rely on it: only create answer the prompt.
        assert all(
            rec.test_case != UNRECORDED_TESTCASE_ID
            for run in world.test_runs.values()
            for rec in run.records
        )

    def test_template_run_has_no_records(self, world: World) -> None:
        assert world.test_run(TEST_RUN_TEMPLATE_ID).records == []


class TestMoves:
    def test_no_anchor_appends(self, world: World) -> None:
        world.move_to_document(FLOATING_TASK_ID, MODULE_ID)
        assert _parts(world)[-1] == f"workitem_{FLOATING_TASK_ID}"
        assert world.work_item(FLOATING_TASK_ID).module == MODULE_ID

    def test_previous_part_inserts_right_after(self, world: World) -> None:
        world.move_to_document(
            FLOATING_TASK_ID, MODULE_ID, previous=f"{MODULE_ID}/{SECTION_A_PART_ID}"
        )
        parts = _parts(world)
        assert (
            parts[parts.index(SECTION_A_PART_ID) + 1] == f"workitem_{FLOATING_TASK_ID}"
        )

    def test_next_part_inserts_right_before(self, world: World) -> None:
        world.move_to_document(
            FLOATING_TASK_ID, MODULE_ID, next_=f"{MODULE_ID}/{SECTION_A_PART_ID}"
        )
        parts = _parts(world)
        assert (
            parts[parts.index(SECTION_A_PART_ID) - 1] == f"workitem_{FLOATING_TASK_ID}"
        )

    def test_unknown_or_bare_anchor_is_400(self, world: World) -> None:
        for anchor in (f"{MODULE_ID}/nope", SECTION_A_PART_ID):
            with pytest.raises(Fault) as excinfo:
                world.move_to_document(FLOATING_TASK_ID, MODULE_ID, previous=anchor)
            assert excinfo.value.status == 400

    def test_member_move_is_409(self, world: World) -> None:
        with pytest.raises(Fault) as excinfo:
            world.move_to_document(CHILD_REQ_ID, MODULE_ID)
        assert _fault(excinfo) == (409, "Work Item already in Document.")

    def test_heading_move_in_or_out_is_400(self, world: World) -> None:
        before = _parts(world)
        for move in (
            lambda: world.move_to_document(FLOATING_HEADING_ID, MODULE_ID),
            lambda: world.move_to_document(DOC_HEADING_ID, MODULE_ID),  # not 409
            lambda: world.move_from_document(DOC_HEADING_ID),
        ):
            with pytest.raises(Fault) as excinfo:
                move()
            assert _fault(excinfo) == (400, "Cannot move headings.")
        assert _parts(world) == before

    def test_cross_document_move_relocates(self, world: World) -> None:
        world.move_to_document(
            CHILD_REQ_ID, PARENT_MODULE_ID, previous=f"{PARENT_MODULE_ID}/pp-1"
        )
        assert f"workitem_{CHILD_REQ_ID}" not in _parts(world)
        assert _parts(world, PARENT_DOC)[1] == f"workitem_{CHILD_REQ_ID}"
        assert world.work_item(CHILD_REQ_ID).module == PARENT_MODULE_ID
        roles = [x.role for x in world.links_of(CHILD_REQ_ID)]
        assert roles == ["satisfies", "verifies"]  # no heading above in target

    def test_bad_anchor_leaves_source_untouched(self, world: World) -> None:
        with pytest.raises(Fault):
            world.move_to_document(
                CHILD_REQ_ID, PARENT_MODULE_ID, previous=f"{PARENT_MODULE_ID}/nope"
            )
        assert f"workitem_{CHILD_REQ_ID}" in _parts(world)
        assert world.work_item(CHILD_REQ_ID).module == MODULE_ID

    def test_orphan_moves_to_other_document(self, world: World) -> None:
        doc = world.document(SPACE, DOC)
        world.update_document(doc, {"homePageContent": {"value": '<p id="p">x</p>'}})
        world.move_to_document(CHILD_REQ_ID, PARENT_MODULE_ID)
        assert _parts(world, PARENT_DOC)[-1] == f"workitem_{CHILD_REQ_ID}"

    def test_move_in_drops_structure_role_links(self, world: World) -> None:
        # Live: move replace item's links of target structureLinkRole --
        # heading above or not; other roles survive.
        world.create_links(
            FLOATING_TASK_ID,
            [_entry("relates_to", CHILD_REQ_ID), _entry("parent", CHILD_REQ_ID)],
        )
        world.move_to_document(FLOATING_TASK_ID, MODULE_ID, previous=f"{MODULE_ID}/p-1")
        assert [x.role for x in world.links_of(FLOATING_TASK_ID)] == ["parent"]

    def test_move_from_detaches(self, world: World) -> None:
        world.move_from_document(CHILD_REQ_ID)
        assert f"workitem_{CHILD_REQ_ID}" not in _parts(world)
        assert world.work_item(CHILD_REQ_ID).module == ""

    def test_move_from_free_floating_is_400(self, world: World) -> None:
        with pytest.raises(Fault) as excinfo:
            world.move_from_document(FLOATING_TASK_ID)
        assert _fault(excinfo) == (400, "Work Item is not in Document.")

    def test_unknown_work_item_or_document_is_404(self, world: World) -> None:
        with pytest.raises(Fault) as excinfo:
            world.move_to_document("MCPT-9999", MODULE_ID)
        assert excinfo.value.status == 404
        with pytest.raises(Fault) as excinfo:
            world.move_to_document(FLOATING_TASK_ID, f"{PROJECT}/{SPACE}/Nope")
        assert excinfo.value.status == 404


class TestStructureLinks:
    def test_member_under_heading_links_heading(self, world: World) -> None:
        links = world.links_of(UNCOVERED_REQ_ID)
        assert [(x.role, x.target) for x in links] == [("relates_to", DOC_HEADING_ID)]

    def test_role_follows_document_structure_link_role(self, world: World) -> None:
        world.document(SPACE, DOC).attributes["structureLinkRole"] = "parent"
        assert world.links_of(UNCOVERED_REQ_ID)[0].role == "parent"

    def test_item_above_any_heading_gets_none(self, world: World) -> None:
        world.move_to_document(FLOATING_TASK_ID, MODULE_ID, previous=f"{MODULE_ID}/p-1")
        assert [x.target for x in world.links_of(FLOATING_TASK_ID)] == []

    def test_headings_get_none(self, world: World) -> None:
        assert world.links_of(DOC_HEADING_ID) == []

    def test_moved_in_then_out_adds_then_drops(self, world: World) -> None:
        world.move_to_document(FLOATING_TASK_ID, MODULE_ID)
        assert DOC_HEADING_ID in [x.target for x in world.links_of(FLOATING_TASK_ID)]
        world.move_from_document(FLOATING_TASK_ID)
        assert world.links_of(FLOATING_TASK_ID) == []

    def test_repeat_of_structure_link_is_409(self, world: World) -> None:
        with pytest.raises(Fault) as excinfo:
            world.create_links(UNCOVERED_REQ_ID, [_entry("relates_to", DOC_HEADING_ID)])
        assert excinfo.value.status == 409
        assert len(world.links_of(UNCOVERED_REQ_ID)) == 1

    def test_member_structure_role_link_is_phantom(self, world: World) -> None:
        # Live: 201 + id, never stored; other roles persist.
        ids = world.create_links(
            UNCOVERED_REQ_ID, [_entry("relates_to"), _entry("satisfies")]
        )
        assert ids[0].endswith(f"/relates_to/{PROJECT}/{FLOATING_TASK_ID}")
        assert [(x.role, x.target) for x in world.links_of(UNCOVERED_REQ_ID)] == [
            ("satisfies", FLOATING_TASK_ID),
            ("relates_to", DOC_HEADING_ID),
        ]

    def test_member_above_heading_still_phantom(self, world: World) -> None:
        world.move_to_document(FLOATING_TASK_ID, MODULE_ID, previous=f"{MODULE_ID}/p-1")
        world.create_links(FLOATING_TASK_ID, [_entry("relates_to", CHILD_REQ_ID)])
        assert world.links_of(FLOATING_TASK_ID) == []

    def test_structure_link_ignores_update_and_delete(self, world: World) -> None:
        # Live: both 204, link unchanged.
        flag = {"suspect": True}
        world.update_link(UNCOVERED_REQ_ID, "relates_to", DOC_HEADING_ID, flag)
        link_id = f"{PROJECT}/{UNCOVERED_REQ_ID}/relates_to/{PROJECT}/{DOC_HEADING_ID}"
        world.delete_links(UNCOVERED_REQ_ID, [link_id])
        (link,) = world.links_of(UNCOVERED_REQ_ID)
        assert (link.target, link.suspect) == (DOC_HEADING_ID, False)

    def test_back_links_include_structure_links(self, world: World) -> None:
        sources = {wi.id for wi in world.linking_to(DOC_HEADING_ID)}
        assert sources == {CHILD_REQ_ID, UNCOVERED_REQ_ID}


def _entry(role: str, target: str = FLOATING_TASK_ID) -> dict[str, object]:
    return {
        "attributes": {"role": role},
        "relationships": {"workItem": {"data": {"id": f"{PROJECT}/{target}"}}},
    }


_LINK_ENTRY = {
    "attributes": {"role": "relates_to", "suspect": True, "revision": "7"},
    "relationships": {"workItem": {"data": {"id": f"{PROJECT}/{PARENT_REQ_ID}"}}},
}


class TestLinks:
    def test_create_update_delete(self, world: World) -> None:
        ids = world.create_links(FLOATING_TASK_ID, [_LINK_ENTRY])
        assert ids == [
            f"{PROJECT}/{FLOATING_TASK_ID}/relates_to/{PROJECT}/{PARENT_REQ_ID}"
        ]
        (link,) = world.links_of(FLOATING_TASK_ID)
        assert (link.suspect, link.revision) == (True, "7")
        world.update_link(
            FLOATING_TASK_ID,
            "relates_to",
            PARENT_REQ_ID,
            {"suspect": False, "revision": "8"},
        )
        assert (link.suspect, link.revision) == (False, "8")
        world.delete_links(FLOATING_TASK_ID, [*ids, "malformed"])
        assert world.links_of(FLOATING_TASK_ID) == []

    def test_repeat_role_target_is_409_whole_batch(self, world: World) -> None:
        world.create_links(FLOATING_TASK_ID, [_entry("relates_to", CHILD_REQ_ID)])
        batches = (
            [_entry("parent", CHILD_REQ_ID), _entry("relates_to", CHILD_REQ_ID)],
            [_entry("verifies", CHILD_REQ_ID), _entry("verifies", CHILD_REQ_ID)],
        )
        for batch in batches:
            with pytest.raises(Fault) as excinfo:
                world.create_links(FLOATING_TASK_ID, batch)
            assert excinfo.value.status == 409
            assert "is already linked with role" in excinfo.value.detail
        assert [x.role for x in world.links_of(FLOATING_TASK_ID)] == ["relates_to"]

    def test_update_unknown_link_is_404(self, world: World) -> None:
        with pytest.raises(Fault) as excinfo:
            world.update_link(FLOATING_TASK_ID, "relates_to", PARENT_REQ_ID, {})
        assert excinfo.value.status == 404

    def test_ghost_target_accepted(self, world: World) -> None:
        # Server validate no target -- guard/ job, fake must not mask it.
        ghost = {
            **_LINK_ENTRY,
            "relationships": {"workItem": {"data": {"id": "X/NOPE"}}},
        }
        world.create_links(FLOATING_TASK_ID, [ghost])
        assert world.links_of(FLOATING_TASK_ID)[0].target == "NOPE"


class TestBody:
    def _patch(self, world: World, *blocks: str) -> None:
        world.update_document(
            world.document(SPACE, DOC),
            {"homePageContent": {"type": "text/html", "value": "\n".join(blocks)}},
        )

    def test_plain_heading_spawns_heading_work_item(self, world: World) -> None:
        self._patch(world, heading_html(DOC_HEADING_ID, 1), "<h3>Perf &amp; scale</h3>")
        block = world.document(SPACE, DOC).blocks[-1]
        spawned = world.work_item(block.ref)
        assert (block.kind, block.level) == ("heading", 2)
        assert spawned.attributes["title"] == "Perf & scale"
        assert spawned.attributes["type"] == "heading"
        assert spawned.module == MODULE_ID

    def test_dropped_member_becomes_orphan(self, world: World) -> None:
        self._patch(world, '<p id="p-1">only intro</p>')
        assert _parts(world) == ["p-1"]
        orphan = world.work_item(CHILD_REQ_ID)
        assert orphan.module == MODULE_ID
        assert world.links_of(CHILD_REQ_ID)[-1].target == TESTCASE_ID  # no structure
        with pytest.raises(Fault) as excinfo:
            world.move_to_document(CHILD_REQ_ID, MODULE_ID)
        assert excinfo.value.status == 409
        world.move_from_document(CHILD_REQ_ID)
        assert orphan.module == ""

    def test_orphan_placeholder_restores_member(self, world: World) -> None:
        self._patch(world, '<p id="p-1">x</p>')
        self._patch(world, '<p id="p-1">x</p>', work_item_html(CHILD_REQ_ID))
        assert _parts(world) == ["p-1", f"workitem_{CHILD_REQ_ID}"]

    def test_non_member_and_repeat_placeholders_dropped(self, world: World) -> None:
        self._patch(
            world,
            work_item_html(CHILD_REQ_ID),
            work_item_html(CHILD_REQ_ID),
            work_item_html(FLOATING_TASK_ID),
            work_item_html("MCPT-9999"),
        )
        assert _parts(world) == [f"workitem_{CHILD_REQ_ID}"]
        assert world.work_item(FLOATING_TASK_ID).module == ""

    def test_duplicate_block_id_rejects_whole_write(self, world: World) -> None:
        before = _parts(world)
        with pytest.raises(Fault) as excinfo:
            self._patch(world, '<p id="d">1</p>', "<h2>New</h2>", '<p id="d">2</p>')
        assert excinfo.value.status == 400
        assert _parts(world) == before
        assert all(wi.attributes["title"] != "New" for wi in world.work_items.values())

    def test_top_level_text_rejects_whole_write(self, world: World) -> None:
        before = _parts(world)
        with pytest.raises(Fault) as excinfo:
            self._patch(world, "<h2>New</h2>", "loose text")
        assert _fault(excinfo) == (
            400,
            "Invalid Home Page HTML. Unexpected top-level text nodes",
        )
        assert _parts(world) == before
        assert all(wi.attributes["title"] != "New" for wi in world.work_items.values())

    def test_other_attributes_merge(self, world: World) -> None:
        doc = world.document(SPACE, DOC)
        world.update_document(doc, {"title": "Renamed", "moduleName": "ignored"})
        assert doc.attributes["title"] == "Renamed"
        assert doc.name == DOC
        assert _parts(world)  # body untouched without homePageContent


class TestDocuments:
    def test_create_defaults_and_body(self, world: World) -> None:
        doc = world.create_document(
            SPACE,
            {
                "attributes": {
                    "moduleName": "New",
                    "title": "New",
                    "homePageContent": {"value": "<h2>Intro</h2>"},
                }
            },
        )
        assert doc.attributes["status"] == "draft"
        assert doc.attributes["structureLinkRole"] == "relates_to"
        assert doc.blocks[0].kind == "heading"
        assert world.document(SPACE, "New") is doc

    def test_create_requires_name(self, world: World) -> None:
        with pytest.raises(Fault) as excinfo:
            world.create_document(SPACE, {"attributes": {}})
        assert excinfo.value.status == 400

    def test_copy_duplicates_members_and_links_originals(self, world: World) -> None:
        source = world.document(SPACE, DOC)
        copy = world.copy_document(source, SPACE, "Copy", link_role="derived_from")
        assert "title" not in copy.attributes
        assert copy.attributes["status"] == "draft"
        assert copy.attributes["structureLinkRole"] == "relates_to"
        members = [b for b in copy.blocks if b.kind != "normal"]
        originals = [b for b in source.blocks if b.kind != "normal"]
        assert [b.kind for b in members] == [b.kind for b in originals]
        assert {b.ref for b in members}.isdisjoint({b.ref for b in originals})
        child_copy = world.work_item(members[1].ref)
        assert child_copy.module == copy.module_id
        assert child_copy.attributes["title"] == "Child requirement"
        targets = {(x.role, x.target) for x in world.links_of(child_copy.id)}
        assert ("derived_from", CHILD_REQ_ID) in targets
        assert ("relates_to", members[0].ref) in targets  # structure re-derived
        assert ("satisfies", PARENT_REQ_ID) in targets  # outgoing kept
        assert copy.blocks[0].html == source.blocks[0].html  # normal verbatim

    def test_copy_remove_outgoing_links(self, world: World) -> None:
        copy = world.copy_document(
            world.document(SPACE, DOC), SPACE, "Copy", remove_outgoing_links=True
        )
        child_copy = next(b.ref for b in copy.blocks if b.kind == "workitem")
        assert [x.role for x in world.links_of(child_copy)] == ["relates_to"]

    def test_copy_onto_existing_is_409(self, world: World) -> None:
        with pytest.raises(Fault) as excinfo:
            world.copy_document(world.document(SPACE, DOC), SPACE, DOC)
        assert excinfo.value.status == 409


class TestWorkItems:
    def test_create_applies_defaults_and_assignees(self, world: World) -> None:
        (new_id,) = world.create_work_items(
            [
                {
                    "attributes": {"title": "T", "type": "task", "priority": "90.0"},
                    "relationships": {"assignee": {"data": [{"id": "u-1"}, {}]}},
                }
            ]
        )
        wi = world.work_item(new_id)
        assert wi.attributes["priority"] == "90.0"
        assert wi.attributes["status"] == "open"
        assert wi.assignees == ["u-1"]

    def test_update_is_atomic(self, world: World) -> None:
        with pytest.raises(Fault):
            world.update_work_items(
                [
                    {
                        "id": f"{PROJECT}/{FLOATING_TASK_ID}",
                        "attributes": {"title": "x"},
                    },
                    {"id": f"{PROJECT}/MCPT-9999", "attributes": {"title": "y"}},
                ]
            )
        assert world.work_item(FLOATING_TASK_ID).attributes["title"] == "Floating task"

    def test_workflow_action_unknown_rejects_batch(self, world: World) -> None:
        entry = {"id": f"{PROJECT}/{FLOATING_TASK_ID}", "attributes": {"title": "x"}}
        with pytest.raises(Fault) as excinfo:
            world.update_work_items([entry], workflow_action="close")
        assert _fault(excinfo) == (400, "The workflow action 'close' was not found.")
        assert world.work_item(FLOATING_TASK_ID).attributes["title"] == "Floating task"

    def test_update_merges_attributes_assignees_and_type(self, world: World) -> None:
        world.update_work_items(
            [
                {
                    "id": f"{PROJECT}/{FLOATING_TASK_ID}",
                    "attributes": {"status": "done"},
                    "relationships": {"assignee": {"data": [{"id": "u-2"}]}},
                }
            ],
            change_type_to="issue",
        )
        wi = world.work_item(FLOATING_TASK_ID)
        assert wi.attributes["status"] == "done"
        assert wi.attributes["title"] == "Floating task"  # omitted = preserved
        assert wi.attributes["type"] == "issue"
        assert wi.assignees == ["u-2"]


class TestComments:
    def test_plain_text_stored_escaped(self, world: World) -> None:
        thread = world.document(SPACE, DOC).comments
        (cid,) = world.create_comments(
            thread,
            [{"attributes": {"text": {"type": "text/plain", "value": "<b>x</b>"}}}],
        )
        assert thread[-1].text == "&lt;b&gt;x&lt;/b&gt;"
        assert cid == "100"

    def test_reply_needs_known_parent(self, world: World) -> None:
        thread = world.work_item(FLOATING_TASK_ID).comments
        reply = {
            "attributes": {"text": {"value": "r"}, "title": "t"},
            "relationships": {"parentComment": {"data": {"id": f"{PROJECT}/X/c-1"}}},
        }
        world.create_comments(thread, [reply])
        assert (thread[-1].parent_id, thread[-1].title) == ("c-1", "t")
        bad = {"relationships": {"parentComment": {"data": {"id": "P/X/nope"}}}}
        with pytest.raises(Fault) as excinfo:
            world.create_comments(thread, [bad])
        assert excinfo.value.status == 400

    def test_resolving_root_resolves_thread(self, world: World) -> None:
        thread = world.document(SPACE, DOC).comments
        world.update_comment(thread, "1", {"resolved": True})
        assert all(c.resolved for c in thread)
        world.update_comment(thread, "2", {})  # no resolved key = no-op
        with pytest.raises(Fault) as excinfo:
            world.update_comment(thread, "nope", {"resolved": True})
        assert excinfo.value.status == 404


class TestAttachments:
    def test_document_clash_is_409(self, world: World) -> None:
        doc = world.document(SPACE, DOC)
        existing = doc.attachments[0].attachment_id
        with pytest.raises(Fault) as excinfo:
            world.add_document_attachments(doc, [Attachment(existing, "", 1, b"x")])
        assert excinfo.value.status == 409

    def test_work_item_ids_counter_prefixed(self, world: World) -> None:
        wi = world.work_item(FLOATING_TASK_ID)
        ids = world.add_work_item_attachments(
            wi, [Attachment("a.png", "", 1, b"a"), Attachment("a.png", "", 1, b"b")]
        )
        assert ids == ["2-a.png", "3-a.png"]

    def test_record_ids_rewritten_and_batch_clash_is_409(self, world: World) -> None:
        record = world.record(TEST_RUN_ID, PROJECT, TESTCASE_ID, "0")
        assert world.add_record_attachments(
            record, [Attachment("n.txt", "", 1, b"x")]
        ) == [f"{TESTCASE_ID}_n.txt"]
        with pytest.raises(Fault):
            world.add_record_attachments(
                record,
                [Attachment("m.txt", "", 1, b""), Attachment("m.txt", "", 1, b"")],
            )


class TestTestRuns:
    def test_create_inherits_template_type(self, world: World) -> None:
        ids = world.create_test_runs(
            [
                {
                    "attributes": {"id": "TR-New"},
                    "relationships": {
                        "template": {
                            "data": {"id": f"{PROJECT}/{TEST_RUN_TEMPLATE_ID}"}
                        }
                    },
                }
            ]
        )
        run = world.test_run("TR-New")
        assert ids == ["TR-New"]
        assert run.attributes["type"] == "manual"
        assert run.is_template is False

    def test_create_duplicate_id_is_409_atomic(self, world: World) -> None:
        with pytest.raises(Fault) as excinfo:
            world.create_test_runs(
                [{"attributes": {"id": "A"}}, {"attributes": {"id": "A"}}]
            )
        assert excinfo.value.status == 409
        assert "A" not in world.test_runs

    def test_update_merges(self, world: World) -> None:
        world.update_test_runs(
            [{"id": f"{PROJECT}/{TEST_RUN_ID}", "attributes": {"status": "x"}}]
        )
        assert world.test_run(TEST_RUN_ID).attributes["status"] == "x"

    def test_record_repeat_test_case_takes_next_iteration(self, world: World) -> None:
        entry = {
            "relationships": {"testCase": {"data": {"id": f"{PROJECT}/{TESTCASE_ID}"}}}
        }
        ids = world.create_test_records(TEST_RUN_ID, [entry, entry])
        assert [i.rsplit("/", 1)[-1] for i in ids] == ["1", "2"]

    def test_record_unknown_test_case_rejects_batch(self, world: World) -> None:
        good = {
            "relationships": {"testCase": {"data": {"id": f"{PROJECT}/{TESTCASE_ID}"}}}
        }
        with pytest.raises(Fault) as excinfo:
            world.create_test_records(TEST_RUN_ID, [good, {"relationships": {}}])
        assert excinfo.value.status == 400
        assert len(world.test_run(TEST_RUN_ID).records) == 1

    def test_record_update_atomic(self, world: World) -> None:
        rid = f"{PROJECT}/{TEST_RUN_ID}/{PROJECT}/{TESTCASE_ID}/0"
        with pytest.raises(Fault):
            world.update_test_records(
                TEST_RUN_ID,
                [{"id": rid, "attributes": {"result": "passed"}}, {"id": "nope"}],
            )
        record = world.record(TEST_RUN_ID, PROJECT, TESTCASE_ID, "0")
        assert record.attributes["result"] == "failed"
        world.update_test_records(
            TEST_RUN_ID, [{"id": rid, "attributes": {"result": "passed"}}]
        )
        assert record.attributes["result"] == "passed"

    def test_record_defect_relationship_stored(self, world: World) -> None:
        defect = {"defect": {"data": {"id": f"{PROJECT}/{FLOATING_TASK_ID}"}}}
        (rid,) = world.create_test_records(
            TEST_RUN_ID,
            [
                {
                    "relationships": {
                        "testCase": {"data": {"id": f"{PROJECT}/{TESTCASE_ID}"}},
                        **defect,
                    }
                }
            ],
        )
        created = world.record(TEST_RUN_ID, PROJECT, TESTCASE_ID, "1")
        assert created.defect == FLOATING_TASK_ID
        seeded = world.record(TEST_RUN_ID, PROJECT, TESTCASE_ID, "0")
        seeded_id = f"{PROJECT}/{TEST_RUN_ID}/{PROJECT}/{TESTCASE_ID}/0"
        world.update_test_records(
            TEST_RUN_ID,
            [{"id": seeded_id, "relationships": defect}, {"id": rid}],
        )
        assert (seeded.defect, created.defect) == (FLOATING_TASK_ID, FLOATING_TASK_ID)
        world.update_test_records(
            TEST_RUN_ID, [{"id": rid, "relationships": {"defect": {"data": None}}}]
        )
        assert created.defect == ""

    def test_record_lookup_misses_are_404(self, world: World) -> None:
        for args in (
            ("Nope", PROJECT, TESTCASE_ID, "0"),
            (TEST_RUN_ID, "Other", TESTCASE_ID, "0"),
            (TEST_RUN_ID, PROJECT, TESTCASE_ID, "1"),
        ):
            with pytest.raises(Fault) as excinfo:
                world.record(*args)
            assert excinfo.value.status == 404
