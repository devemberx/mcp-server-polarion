"""Referential-integrity checks on ``SEEDS``: a broken cross-reference would
otherwise surface as a late ``KeyError`` deep in ``FakePolarion`` only when a
case happens to hit the affected entity. Catching it at the data layer keeps
the "add an entity = one table entry" workflow safe.
"""

from __future__ import annotations

from evals.harness.fake_polarion.body import parse
from evals.harness.fixtures import PROJECT, SEEDS, SPACE


def test_document_keys_match_names() -> None:
    for key, doc in SEEDS.documents.items():
        assert key == doc.name


def test_body_placeholders_reference_member_work_items() -> None:
    for doc in SEEDS.documents.values():
        module_id = f"{PROJECT}/{SPACE}/{doc.name}"
        for block in parse(doc.body_html):
            if block.kind != "normal":
                assert SEEDS.work_items[block.ref].module_id == module_id


def test_document_members_sit_in_body() -> None:
    # Member absent from body = orphan; seeds start orphan-free.
    placed = {
        block.ref
        for doc in SEEDS.documents.values()
        for block in parse(doc.body_html)
        if block.kind != "normal"
    }
    for wi in SEEDS.work_items.values():
        if wi.module_id:
            assert wi.short_id in placed


def test_normal_blocks_carry_unique_ids() -> None:
    # Anchorless block break live /parts; duplicate id reject whole write.
    for doc in SEEDS.documents.values():
        ids = [b.ref for b in parse(doc.body_html) if b.kind == "normal"]
        assert all(ids)
        assert len(set(ids)) == len(ids)


def test_links_reference_existing_work_items() -> None:
    for source, targets in SEEDS.links.items():
        assert source in SEEDS.work_items
        for _role, target in targets:
            assert target in SEEDS.work_items


def test_comment_parents_reference_sibling_comments() -> None:
    for doc in SEEDS.documents.values():
        ids = {c.comment_id for c in doc.comments}
        for comment in doc.comments:
            if comment.parent_id is not None:
                assert comment.parent_id in ids


def test_work_item_modules_reference_existing_documents() -> None:
    module_ids = {f"{PROJECT}/{SPACE}/{doc.name}" for doc in SEEDS.documents.values()}
    for wi in SEEDS.work_items.values():
        if wi.module_id:
            assert wi.module_id in module_ids
