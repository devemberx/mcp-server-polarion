"""Fake-Polarion tests: ``_dispatch`` is a pure request router, driven with
hand-built requests (no respx). Pins the routing table and the mutation log.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from evals.harness.fake_polarion import FakePolarion
from evals.harness.fixtures import (
    API_PREFIX,
    CHILD_REQ_ID,
    DOC,
    DOC_HEADING_ID,
    DOC_INTRO_PARAGRAPH_ID,
    FLOATING_TASK_HYPERLINK_URI,
    FLOATING_TASK_ID,
    MODULE_ID,
    PARENT_DOC,
    PARENT_REQ_ID,
    POLARION_HOST,
    PROJECT,
    SECTION_A_PART_ID,
    SEEDS,
    SPACE,
    TESTCASE_ID,
)

_BASE = f"{POLARION_HOST}{API_PREFIX}"


def _get(fake: FakePolarion, path: str, **params: str) -> httpx.Response:
    request = httpx.Request("GET", f"{_BASE}{path}", params=params or None)
    return fake._dispatch(request)


def _mutate(
    fake: FakePolarion, method: str, path: str, body: Any = None
) -> httpx.Response:
    content = json.dumps(body).encode() if body is not None else b""
    request = httpx.Request(method, f"{_BASE}{path}", content=content)
    return fake._dispatch(request)


def _json(response: httpx.Response) -> Any:
    return json.loads(response.content)


class TestReadRouting:
    def test_projects_list(self) -> None:
        response = _get(FakePolarion(), "/projects")
        assert response.status_code == 200
        data = _json(response)["data"]
        assert data[0]["id"] == PROJECT

    def test_enum_options_carry_default_flag(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/fields/type/actions/getAvailableOptions",
        )
        assert response.status_code == 200
        options = _json(response)["data"]
        ids = {o["id"] for o in options}
        assert "task" in ids
        defaults = [o["id"] for o in options if o["default"]]
        assert defaults == ["task"]

    def test_single_work_item_found(self) -> None:
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/workitems/{DOC_HEADING_ID}"
        )
        assert response.status_code == 200
        assert _json(response)["data"]["id"] == f"{PROJECT}/{DOC_HEADING_ID}"

    def test_single_work_item_missing_is_404(self) -> None:
        response = _get(FakePolarion(), f"/projects/{PROJECT}/workitems/MCPT-9999")
        assert response.status_code == 404

    def test_work_item_list_returns_all(self) -> None:
        response = _get(FakePolarion(), f"/projects/{PROJECT}/workitems")
        assert response.status_code == 200
        assert _json(response)["meta"]["totalCount"] == len(SEEDS.work_items)

    def test_work_item_list_filters_headings(self) -> None:
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/workitems", query="type:heading"
        )
        items = _json(response)["data"]
        assert all(i["attributes"]["type"] == "heading" for i in items)
        assert len(items) == 2

    def test_linked_work_items_empty_when_unlinked(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/{DOC_HEADING_ID}/linkedworkitems",
        )
        assert _json(response)["meta"]["totalCount"] == 0

    def test_parts_seeded_for_fakedoc(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/parts",
        )
        data = _json(response)["data"]
        ids = [p["id"].rsplit("/", 1)[-1] for p in data]
        assert SECTION_A_PART_ID in ids

    def test_parts_empty_for_other_document(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{PARENT_DOC}/parts",
        )
        assert _json(response)["meta"]["totalCount"] == 0

    def test_comments_thread_shape(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/comments",
        )
        data = _json(response)["data"]
        assert len(data) == 2
        root = next(
            c for c in data if c["relationships"]["parentComment"]["data"] is None
        )
        assert root["relationships"]["childComments"]["data"]

    def test_single_document_exact_match(self) -> None:
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}"
        )
        assert response.status_code == 200
        assert _json(response)["data"]["id"] == MODULE_ID

    def test_single_document_other_name_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/OtherDoc",
        )
        assert response.status_code == 404

    def test_document_body_has_anchored_intro_paragraph(self) -> None:
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}"
        )
        body = _json(response)["data"]["attributes"]["homePageContent"]["value"]
        assert f'id="{DOC_INTRO_PARAGRAPH_ID}"' in body

    def test_floating_task_carries_seed_hyperlink(self) -> None:
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}"
        )
        hyperlinks = _json(response)["data"]["attributes"]["hyperlinks"]
        assert hyperlinks == [{"role": "ref_ext", "uri": FLOATING_TASK_HYPERLINK_URI}]

    def test_project_enum_hyperlink_role_is_dict_shaped(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/enumerations/~/hyperlink-role/~",
        )
        assert response.status_code == 200
        data = _json(response)["data"]
        assert isinstance(data, dict)
        ids = [o["id"] for o in data["attributes"]["options"]]
        assert ids == ["ref_int", "ref_ext"]

    def test_unknown_project_enum_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/enumerations/~/not-a-real-enum/~",
        )
        assert response.status_code == 404


class TestWorkItemResource:
    def test_module_relationship_only_for_module_items(self) -> None:
        fake = FakePolarion()
        heading = _json(_get(fake, f"/projects/{PROJECT}/workitems/{DOC_HEADING_ID}"))[
            "data"
        ]
        assert "module" in heading["relationships"]

        floating = _json(_get(fake, f"/projects/{PROJECT}/workitems/MCPT-200"))["data"]
        assert "module" not in floating["relationships"]


class TestMutations:
    def test_post_workitems_echoes_id(self) -> None:
        fake = FakePolarion()
        response = _mutate(fake, "POST", f"/projects/{PROJECT}/workitems", {"data": []})
        assert response.status_code == 201
        assert _json(response)["data"][0]["type"] == "workitems"

    def test_post_workitems_echoes_one_id_per_submitted_entry(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake,
            "POST",
            f"/projects/{PROJECT}/workitems",
            {"data": [{"x": 1}, {"x": 2}, {"x": 3}]},
        )
        ids = [entry["id"] for entry in _json(response)["data"]]
        assert len(ids) == 3
        assert len(set(ids)) == 3

    def test_post_documents_echoes_module_id(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake, "POST", f"/projects/{PROJECT}/spaces/{SPACE}/documents", {"data": []}
        )
        assert _json(response)["data"][0]["id"] == MODULE_ID

    def test_post_comments_echoes_id(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake,
            "POST",
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/comments",
            {"data": []},
        )
        assert _json(response)["data"][0]["type"] == "document_comments"

    def test_post_linked_work_items_echoes_id(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake,
            "POST",
            f"/projects/{PROJECT}/workitems/{DOC_HEADING_ID}/linkedworkitems",
            {"data": []},
        )
        assert _json(response)["data"][0]["type"] == "linkedworkitems"

    def test_patch_and_delete_return_204(self) -> None:
        fake = FakePolarion()
        patch = _mutate(
            fake,
            "PATCH",
            f"/projects/{PROJECT}/workitems/{DOC_HEADING_ID}",
            {"data": {}},
        )
        delete = _mutate(
            fake, "DELETE", f"/projects/{PROJECT}/workitems/{DOC_HEADING_ID}"
        )
        assert patch.status_code == 204
        assert delete.status_code == 204

    def test_every_mutation_is_recorded(self) -> None:
        fake = FakePolarion()
        _mutate(fake, "POST", f"/projects/{PROJECT}/workitems", {"data": [{"x": 1}]})
        _mutate(
            fake, "PATCH", f"/projects/{PROJECT}/workitems/{DOC_HEADING_ID}", {"a": 2}
        )
        assert len(fake.mutations) == 2
        assert fake.mutations[0]["method"] == "POST"
        assert fake.mutations[0]["json"] == {"data": [{"x": 1}]}
        assert fake.mutations[1]["method"] == "PATCH"

    def test_reads_are_not_recorded(self) -> None:
        fake = FakePolarion()
        _get(fake, "/projects")
        assert fake.mutations == []


class TestOrchestrationSeeding:
    def test_parent_document_resolves(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{PARENT_DOC}",
        )
        assert response.status_code == 200
        assert _json(response)["data"]["attributes"]["moduleName"] == PARENT_DOC

    def test_forward_links_carry_role_and_included_target(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/{CHILD_REQ_ID}/linkedworkitems",
        )
        payload = _json(response)
        roles = {item["attributes"]["role"] for item in payload["data"]}
        assert roles == {"satisfies", "verifies"}
        target_ids = {item["id"] for item in payload["included"]}
        assert f"{PROJECT}/{PARENT_REQ_ID}" in target_ids
        assert f"{PROJECT}/{TESTCASE_ID}" in target_ids

    def test_uncovered_requirement_has_no_links(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/MCPT-301/linkedworkitems",
        )
        assert _json(response)["meta"]["totalCount"] == 0

    def test_back_direction_query_finds_source(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems",
            query=f"linkedWorkItems:{PARENT_REQ_ID}",
        )
        ids = {i["id"].rsplit("/", 1)[-1] for i in _json(response)["data"]}
        assert ids == {CHILD_REQ_ID}

    def test_workitem_link_role_enum_resolves(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/enumerations/~/workitem-link-role/~",
        )
        assert response.status_code == 200
        ids = [o["id"] for o in _json(response)["data"]["attributes"]["options"]]
        assert "relates_to" in ids
