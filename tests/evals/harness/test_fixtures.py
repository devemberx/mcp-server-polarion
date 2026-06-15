"""Referential-integrity checks on ``SEEDS``: a broken cross-reference would
otherwise surface as a late ``KeyError`` deep in ``FakePolarion`` only when a
case happens to hit the affected entity. Catching it at the data layer keeps
the "add an entity = one table entry" workflow safe.
"""

from __future__ import annotations

from evals.harness.fixtures import PROJECT, SEEDS, SPACE


def test_document_keys_match_names() -> None:
    for key, doc in SEEDS.documents.items():
        assert key == doc.name


def test_parts_reference_existing_work_items() -> None:
    for doc in SEEDS.documents.values():
        for part in doc.parts:
            assert part.work_item_id in SEEDS.work_items


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
