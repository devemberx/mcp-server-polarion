"""Fake-Polarion tests: ``_dispatch`` is a pure request router, driven with
hand-built requests (no respx). Pin routing table and mutation log.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx

from evals.harness.fake_polarion import FakePolarion
from evals.harness.fixtures import (
    API_PREFIX,
    CHILD_REQ_ID,
    DOC,
    DOC_ATTACHMENT_CONTENT,
    DOC_ATTACHMENT_ID,
    DOC_HEADING_ID,
    DOC_INTRO_PARAGRAPH_ID,
    FLOATING_TASK_HYPERLINK_URI,
    FLOATING_TASK_ID,
    MODULE_ID,
    PARENT_DOC,
    PARENT_REQ_ID,
    POLARION_HOST,
    PROJECT,
    RECORD_ATTACHMENT_ID,
    RECORD_IMAGE_ATTACHMENT_CONTENT,
    RECORD_IMAGE_ATTACHMENT_ID,
    SECTION_A_PART_ID,
    SEEDS,
    SPACE,
    TEST_RUN_ID,
    TEST_RUN_ID_2,
    TEST_RUN_TEMPLATE_ID,
    TESTCASE_ID,
    WORKITEM_ATTACHMENT_CONTENT,
    WORKITEM_ATTACHMENT_ID,
    Attachment,
)

_BASE = f"{POLARION_HOST}{API_PREFIX}"
# Real client Accept header for binary content routes (client.py mirror).
_BYTES_ACCEPT = {"Accept": "application/octet-stream, application/json"}


def _get(
    fake: FakePolarion,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    **params: str,
) -> httpx.Response:
    request = httpx.Request(
        "GET", f"{_BASE}{path}", params=params or None, headers=headers
    )
    return fake._dispatch(request)


def _mutate(
    fake: FakePolarion, method: str, path: str, body: Any = None
) -> httpx.Response:
    content = json.dumps(body).encode() if body is not None else b""
    request = httpx.Request(method, f"{_BASE}{path}", content=content)
    return fake._dispatch(request)


def _attachment_entry(
    file_name: str,
    *,
    resource_type: str = "document_attachments",
    title: str | None = None,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {"fileName": file_name}
    if title is not None:
        attributes["title"] = title
    return {
        "type": resource_type,
        "attributes": attributes,
    }


def _multipart_attachments_request(
    path: str,
    *,
    resource: dict[str, Any],
    files: list[tuple[str, bytes]],
) -> httpx.Request:
    # Real client wire shape: resource = plain form field, ordered file parts.
    return httpx.Request(
        "POST",
        f"{_BASE}{path}",
        data={"resource": json.dumps(resource)},
        files=[
            ("files", (name, payload, "application/octet-stream"))
            for name, payload in files
        ],
    )


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

    def test_attachments_expose_body_reference_id(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments",
        )
        data = _json(response)["data"]
        assert len(data) == 1
        entry = data[0]
        assert entry["type"] == "document_attachments"
        assert entry["id"] == f"{MODULE_ID}/{DOC_ATTACHMENT_ID}"
        assert entry["attributes"]["id"] == DOC_ATTACHMENT_ID
        assert entry["attributes"]["length"] > 0
        assert entry["relationships"]["author"]["data"]["id"]
        assert _json(response)["included"]

    def test_attachments_empty_for_other_document(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{PARENT_DOC}/attachments",
        )
        assert response.status_code == 200
        assert _json(response)["data"] == []
        assert _json(response)["included"] == []

    def test_attachment_content_serves_seeded_bytes(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments/"
            f"{DOC_ATTACHMENT_ID}/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 200
        assert response.content == DOC_ATTACHMENT_CONTENT

    def test_attachment_content_json_only_accept_is_406(self) -> None:
        # Real Polarion 406 on JSON-only Accept; harness falsify same contract.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments/"
            f"{DOC_ATTACHMENT_ID}/content",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 406
        assert _json(response)["errors"]

    def test_attachment_content_unseeded_attachment_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments/"
            "999-not-real.png/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 404

    def test_attachment_content_wrong_space_is_404(self) -> None:
        # Live-verified: wrong space 404 even when doc name exist elsewhere.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/OtherSpace/documents/{DOC}/attachments/"
            f"{DOC_ATTACHMENT_ID}/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 404

    def test_attachment_content_unseeded_document_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/OtherDoc/attachments/"
            f"{DOC_ATTACHMENT_ID}/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 404

    def test_attachments_relationships_author_only(self) -> None:
        # Sparse fieldset drop project rel -- mock must not ship it.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments",
        )
        entry = _json(response)["data"][0]
        assert sorted(entry["relationships"]) == ["author"]

    def test_attachments_omit_meta(self) -> None:
        # Live omit meta on normal page; totalCount only on overshoot.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments",
        )
        assert "meta" not in _json(response)

    def test_attachments_unseeded_document_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/NoSuchDoc/attachments",
        )
        assert response.status_code == 404

    def test_attachments_wrong_space_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/NoSuchSpace/documents/{DOC}/attachments",
        )
        assert response.status_code == 404

    def test_workitem_attachments_expose_body_reference_id(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
        )
        data = _json(response)["data"]
        assert len(data) == 1
        entry = data[0]
        assert entry["type"] == "workitem_attachments"
        assert entry["id"] == f"{PROJECT}/{FLOATING_TASK_ID}/{WORKITEM_ATTACHMENT_ID}"
        assert entry["attributes"]["id"] == WORKITEM_ATTACHMENT_ID
        assert entry["attributes"]["length"] > 0
        assert entry["relationships"]["author"]["data"]["id"]
        assert _json(response)["included"]

    def test_workitem_attachments_empty_for_other_work_item(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/{DOC_HEADING_ID}/attachments",
        )
        assert response.status_code == 200
        assert _json(response)["data"] == []
        assert _json(response)["included"] == []
        assert "meta" not in _json(response)

    def test_workitem_attachments_relationships_author_only(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
        )
        entry = _json(response)["data"][0]
        assert sorted(entry["relationships"]) == ["author"]

    def test_workitem_attachments_single_page_omits_meta(self) -> None:
        # Live rule: single-page collection omit totalCount.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
        )
        assert "meta" not in _json(response)

    def test_workitem_attachments_multi_page_meta_present_every_page(self) -> None:
        # Live rule: >1-page collection serve totalCount on every page --
        # unlike doc route overshoot-only rule.
        wi = SEEDS.work_items[FLOATING_TASK_ID]
        attachments = [Attachment(f"{i}-fake-extra.png", "fake", 10) for i in range(3)]
        seeds = replace(
            SEEDS,
            work_items={
                **SEEDS.work_items,
                FLOATING_TASK_ID: replace(wi, attachments=attachments),
            },
        )
        fake = FakePolarion(seeds=seeds)
        path = f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments"
        page1 = _get(fake, path, **{"page[size]": "2"})
        page2 = _get(fake, path, **{"page[size]": "2", "page[number]": "2"})
        assert _json(page1)["meta"]["totalCount"] == 3
        assert _json(page2)["meta"]["totalCount"] == 3

    def test_workitem_attachments_unseeded_work_item_404(self) -> None:
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/workitems/MCPT-9999/attachments"
        )
        assert response.status_code == 404

    def test_workitem_attachment_content_serves_seeded_bytes(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments/"
            f"{WORKITEM_ATTACHMENT_ID}/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 200
        assert response.content == WORKITEM_ATTACHMENT_CONTENT

    def test_workitem_attachment_content_json_only_accept_is_406(self) -> None:
        # Live-verified 2026-07-21: same 406 contract as doc route.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments/"
            f"{WORKITEM_ATTACHMENT_ID}/content",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 406
        assert _json(response)["errors"]

    def test_workitem_attachment_content_unseeded_attachment_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments/"
            "999-not-real.png/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 404

    def test_workitem_attachment_content_unseeded_work_item_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/workitems/MCPT-9999/attachments/"
            f"{WORKITEM_ATTACHMENT_ID}/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 404

    def test_comments_omit_meta(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/comments",
        )
        assert "meta" not in _json(response)

    def test_comments_empty_for_other_document(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{PARENT_DOC}/comments",
        )
        assert response.status_code == 200
        assert _json(response)["data"] == []
        assert _json(response)["included"] == []
        assert "meta" not in _json(response)

    def test_comments_unseeded_document_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/NoSuchDoc/comments",
        )
        assert response.status_code == 404

    def test_comments_wrong_space_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/spaces/NoSuchSpace/documents/{DOC}/comments",
        )
        assert response.status_code == 404

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


class TestTestRunRouting:
    def test_instances_carry_resource_and_author(self) -> None:
        response = _get(FakePolarion(), f"/projects/{PROJECT}/testruns")
        assert response.status_code == 200
        payload = _json(response)
        run = payload["data"][0]
        assert run["type"] == "testruns"
        assert run["id"].rsplit("/", 1)[-1] == TEST_RUN_ID
        assert run["attributes"]["isTemplate"] is False
        assert payload["included"][0]["attributes"]["name"] == "Fake Author"

    def test_templates_filter_excludes_instances(self) -> None:
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/testruns", templates="true"
        )
        payload = _json(response)
        assert payload["meta"]["totalCount"] == 1
        run = payload["data"][0]
        assert run["id"].rsplit("/", 1)[-1] == TEST_RUN_TEMPLATE_ID
        assert run["attributes"]["isTemplate"] is True

    def test_single_template_serves_is_template(self) -> None:
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/testruns/{TEST_RUN_TEMPLATE_ID}"
        )
        assert response.status_code == 200
        attributes = _json(response)["data"]["attributes"]
        assert attributes["isTemplate"] is True

    def test_single_instance_omits_is_template(self) -> None:
        # Live Polarion omit isTemplate on run instances; template guard
        # rely on absence to reject instances passed as templates.
        response = _get(FakePolarion(), f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}")
        assert response.status_code == 200
        assert "isTemplate" not in _json(response)["data"]["attributes"]

    def test_single_missing_run_is_404(self) -> None:
        response = _get(FakePolarion(), f"/projects/{PROJECT}/testruns/Nope")
        assert response.status_code == 404

    def test_single_instance_serves_detail_attributes(self) -> None:
        # get_test_run consume same endpoint as template guard; serve full
        # resource so detail fields populate.
        response = _get(FakePolarion(), f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}")
        payload = _json(response)
        attributes = payload["data"]["attributes"]
        assert attributes["title"] == "Fake Regression Run"
        assert attributes["status"] == "open"
        included = payload["included"]
        assert included and included[0]["type"] == "users"

    def test_testing_context_enum_resolves(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/enumerations/testing/testrun-type/~",
        )
        assert response.status_code == 200
        ids = [o["id"] for o in _json(response)["data"]["attributes"]["options"]]
        assert ids == ["manual", "automated"]

    def test_wildcard_context_does_not_resolve_testrun_enum(self) -> None:
        # Mirror live Polarion: testrun enums 404 outside testing context.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/enumerations/~/testrun-type/~",
        )
        assert response.status_code == 404


class TestTestRecordRouting:
    def test_records_carry_test_case_and_executed_by(self) -> None:
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}/testrecords"
        )
        assert response.status_code == 200
        payload = _json(response)
        record = payload["data"][0]
        assert record["type"] == "testrecords"
        assert record["attributes"]["result"] == "failed"
        rel = record["relationships"]
        assert rel["testCase"]["data"]["id"] == f"{PROJECT}/{TESTCASE_ID}"
        assert rel["executedBy"]["data"]["type"] == "users"
        assert payload["included"][0]["attributes"]["name"] == "Fake Author"
        # Live endpoint omit meta.totalCount; mirror it so pagination
        # fallback path exercised.
        assert "meta" not in payload

    def test_records_of_missing_run_is_404(self) -> None:
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/testruns/Nope/testrecords"
        )
        assert response.status_code == 404

    def test_result_filter_matches(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}/testrecords",
            testResultId="failed",
        )
        assert len(_json(response)["data"]) == 1

    def test_result_filter_excludes(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}/testrecords",
            testResultId="passed",
        )
        assert _json(response)["data"] == []

    def test_template_run_has_no_records(self) -> None:
        # Blueprints never executed -- mirror live empty page.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_TEMPLATE_ID}/testrecords",
        )
        assert response.status_code == 200
        assert _json(response)["data"] == []

    def test_multi_iteration_run_serves_one_record_per_iteration(self) -> None:
        # TEST_RUN_ID_2 seed iterations=3 -- EFF-BULK-UPDATE-RECORDS need
        # 3 distinct record ids on one run.
        response = _get(
            FakePolarion(), f"/projects/{PROJECT}/testruns/{TEST_RUN_ID_2}/testrecords"
        )
        data = _json(response)["data"]
        ids = [record["id"] for record in data]
        assert ids == [
            f"{PROJECT}/{TEST_RUN_ID_2}/{PROJECT}/{TESTCASE_ID}/{i}" for i in range(3)
        ]
        iterations = [record["attributes"]["iteration"] for record in data]
        assert iterations == [0, 1, 2]


class TestSingleTestRecordRouting:
    def test_found_record_carries_comment_and_revision(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/0",
        )
        assert response.status_code == 200
        payload = _json(response)
        record = payload["data"]
        assert isinstance(record, dict)
        assert record["type"] == "testrecords"
        assert record["id"] == f"{PROJECT}/{TEST_RUN_ID}/{PROJECT}/{TESTCASE_ID}/0"
        attributes = record["attributes"]
        assert attributes["comment"]["type"] == "text/html"
        assert attributes["comment"]["value"]
        assert attributes["testCaseRevision"]
        rel = record["relationships"]
        assert rel["testCase"]["data"]["id"] == f"{PROJECT}/{TESTCASE_ID}"
        assert rel["executedBy"]["data"]["type"] == "users"
        assert payload["included"][0]["attributes"]["name"] == "Fake Author"

    def test_missing_run_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/Nope/testrecords/{PROJECT}/{TESTCASE_ID}/0",
        )
        assert response.status_code == 404

    def test_wrong_test_case_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}/testrecords/{PROJECT}/MCPT-9999/0",
        )
        assert response.status_code == 404

    def test_wrong_iteration_is_404(self) -> None:
        # TEST_RUN_ID seed iterations=1 -- only iteration 0 exist.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/1",
        )
        assert response.status_code == 404

    def test_multi_iteration_run_serves_requested_iteration(self) -> None:
        # Single GET must honor same iteration range as list route.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID_2}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/2",
        )
        assert response.status_code == 200
        record = _json(response)["data"]
        assert record["id"] == f"{PROJECT}/{TEST_RUN_ID_2}/{PROJECT}/{TESTCASE_ID}/2"
        assert record["attributes"]["iteration"] == 2
        assert record["attributes"]["comment"]["value"]

    def test_iteration_beyond_seeded_count_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID_2}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/3",
        )
        assert response.status_code == 404

    def test_template_run_record_is_404(self) -> None:
        # Blueprints never executed -- no record coordinates resolve.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_TEMPLATE_ID}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/0",
        )
        assert response.status_code == 404


class TestTestRecordAttachmentsRouting:
    def test_returns_seeded_attachment_with_six_segment_id(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/0/attachments",
        )
        assert response.status_code == 200
        payload = _json(response)
        data = payload["data"]
        # Seed carry log + image pair on iteration-0 record.
        assert [e["attributes"]["id"] for e in data] == [
            RECORD_ATTACHMENT_ID,
            RECORD_IMAGE_ATTACHMENT_ID,
        ]
        entry = data[0]
        assert entry["type"] == "testrecord_attachments"
        assert entry["id"] == (
            f"{PROJECT}/{TEST_RUN_ID}/{PROJECT}/{TESTCASE_ID}/0/{RECORD_ATTACHMENT_ID}"
        )
        assert payload["included"]

    def test_missing_run_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/Nope"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/0/attachments",
        )
        assert response.status_code == 404
        assert "was not found" in _json(response)["errors"][0]["detail"]

    def test_wrong_test_case_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
            f"/testrecords/{PROJECT}/MCPT-9999/0/attachments",
        )
        assert response.status_code == 404

    def test_wrong_iteration_is_404(self) -> None:
        # TEST_RUN_ID seed iterations=1 -- iteration 1 unseeded.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/1/attachments",
        )
        assert response.status_code == 404

    def test_template_run_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_TEMPLATE_ID}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/0/attachments",
        )
        assert response.status_code == 404

    def test_other_iteration_serves_empty_page(self) -> None:
        # TEST_RUN_ID_2 seed iterations=3, no record_attachments -- every
        # iteration (incl 0) serve empty page -- iteration-0 semantics apply
        # per seed, not blanket pass across runs.
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID_2}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/0/attachments",
        )
        assert response.status_code == 200
        assert _json(response)["data"] == []
        assert _json(response)["included"] == []
        assert "meta" not in _json(response)

    def test_single_page_omits_meta(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/0/attachments",
        )
        assert "meta" not in _json(response)

    def test_multi_page_meta_present_every_page(self) -> None:
        # Live WI rule: totalCount on every page once collection span >1 page.
        tr = SEEDS.test_runs[TEST_RUN_ID]
        attachments = [
            Attachment(f"{TESTCASE_ID}_extra-{i}.txt", "fake", 10) for i in range(3)
        ]
        seeds = replace(
            SEEDS,
            test_runs={
                **SEEDS.test_runs,
                TEST_RUN_ID: replace(tr, record_attachments=attachments),
            },
        )
        fake = FakePolarion(seeds=seeds)
        path = (
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/0/attachments"
        )
        page1 = _get(fake, path, **{"page[size]": "2"})
        page2 = _get(fake, path, **{"page[size]": "2", "page[number]": "2"})
        assert _json(page1)["meta"]["totalCount"] == 3
        assert _json(page2)["meta"]["totalCount"] == 3

    def test_page_slicing_returns_distinct_pages(self) -> None:
        tr = SEEDS.test_runs[TEST_RUN_ID]
        attachments = [
            Attachment(f"{TESTCASE_ID}_extra-{i}.txt", "fake", 10) for i in range(3)
        ]
        seeds = replace(
            SEEDS,
            test_runs={
                **SEEDS.test_runs,
                TEST_RUN_ID: replace(tr, record_attachments=attachments),
            },
        )
        fake = FakePolarion(seeds=seeds)
        path = (
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
            f"/testrecords/{PROJECT}/{TESTCASE_ID}/0/attachments"
        )
        page1 = _get(fake, path, **{"page[size]": "2", "page[number]": "1"})
        page2 = _get(fake, path, **{"page[size]": "2", "page[number]": "2"})
        ids1 = [e["attributes"]["id"] for e in _json(page1)["data"]]
        ids2 = [e["attributes"]["id"] for e in _json(page2)["data"]]
        assert ids1 == [f"{TESTCASE_ID}_extra-0.txt", f"{TESTCASE_ID}_extra-1.txt"]
        assert ids2 == [f"{TESTCASE_ID}_extra-2.txt"]


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

    def test_post_testruns_echoes_submitted_ids(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake,
            "POST",
            f"/projects/{PROJECT}/testruns",
            {
                "data": [
                    {"type": "testruns", "attributes": {"id": "Fake-TR-New"}},
                    {"type": "testruns", "attributes": {"id": "Fake-TR-New2"}},
                ]
            },
        )
        assert response.status_code == 201
        ids = [entry["id"] for entry in _json(response)["data"]]
        assert ids == [f"{PROJECT}/Fake-TR-New", f"{PROJECT}/Fake-TR-New2"]

    def test_post_testruns_without_ids_still_echoes_entries(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake, "POST", f"/projects/{PROJECT}/testruns", {"data": [{}, {}]}
        )
        ids = [entry["id"] for entry in _json(response)["data"]]
        assert len(ids) == 2
        assert len(set(ids)) == 2

    def test_post_testruns_without_body_falls_back_to_one_id(self) -> None:
        fake = FakePolarion()
        response = _mutate(fake, "POST", f"/projects/{PROJECT}/testruns")
        assert response.status_code == 201
        assert len(_json(response)["data"]) == 1

    def test_post_testrecords_composes_five_segment_ids(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake,
            "POST",
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}/testrecords",
            {
                "data": [
                    {
                        "type": "testrecords",
                        "relationships": {
                            "testCase": {
                                "data": {
                                    "type": "workitems",
                                    "id": f"{PROJECT}/{TESTCASE_ID}",
                                }
                            }
                        },
                    },
                    {
                        "type": "testrecords",
                        "relationships": {
                            "testCase": {
                                "data": {
                                    "type": "workitems",
                                    "id": f"{PROJECT}/{CHILD_REQ_ID}",
                                }
                            }
                        },
                    },
                ]
            },
        )
        assert response.status_code == 201
        ids = [entry["id"] for entry in _json(response)["data"]]
        assert ids == [
            f"{PROJECT}/{TEST_RUN_ID}/{PROJECT}/{TESTCASE_ID}/0",
            f"{PROJECT}/{TEST_RUN_ID}/{PROJECT}/{CHILD_REQ_ID}/0",
        ]

    def test_post_testrecords_unknown_test_case_is_400(self) -> None:
        # Live-verified server message; tool relies on it flowing through.
        fake = FakePolarion()
        response = _mutate(
            fake,
            "POST",
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}/testrecords",
            {
                "data": [
                    {
                        "type": "testrecords",
                        "relationships": {
                            "testCase": {
                                "data": {"type": "workitems", "id": f"{PROJECT}/Nope"}
                            }
                        },
                    }
                ]
            },
        )
        assert response.status_code == 400
        detail = _json(response)["errors"][0]["detail"]
        assert detail == "Test Case is missing, or the one specified is invalid."

    def test_post_testrecords_missing_test_case_relationship_is_400(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake,
            "POST",
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}/testrecords",
            {"data": [{"type": "testrecords"}]},
        )
        assert response.status_code == 400

    def test_post_documents_echoes_module_id(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake, "POST", f"/projects/{PROJECT}/spaces/{SPACE}/documents", {"data": []}
        )
        assert _json(response)["data"][0]["id"] == MODULE_ID

    def test_post_copy_action_returns_sparse_dict(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake,
            "POST",
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/actions/copy",
            {"targetDocumentName": "FakeDocCopy"},
        )
        assert response.status_code == 201
        data = _json(response)["data"]
        assert isinstance(data, dict)
        assert data["id"].endswith("/FakeDocCopy")

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

    def test_post_doc_attachments_echoes_ordered_ids(self) -> None:
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments",
                resource={
                    "data": [
                        _attachment_entry("new-diagram.png"),
                        _attachment_entry("new-photo.png"),
                    ]
                },
                files=[("new-diagram.png", b"png-a"), ("new-photo.png", b"png-b")],
            )
        )
        assert response.status_code == 201
        data = _json(response)["data"]
        # Live shape 2026-07-20: list, input order, type/id/links only.
        assert [e["id"] for e in data] == [
            f"{PROJECT}/{SPACE}/{DOC}/new-diagram.png",
            f"{PROJECT}/{SPACE}/{DOC}/new-photo.png",
        ]
        assert all(e["type"] == "document_attachments" for e in data)
        assert all("links" in e and "attributes" not in e for e in data)

    def test_post_doc_attachments_binary_payload_201(self) -> None:
        # Non-UTF-8 bytes (PNG magic) must not leak UnicodeDecodeError from
        # body parse; hang rationale at `_handle_mutation` catch.
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments",
                resource={"data": [_attachment_entry("real.png")]},
                files=[("real.png", b"\x89PNG\r\n\x1a\n\x00\x01\x02")],
            )
        )
        assert response.status_code == 201

    def test_post_doc_attachments_unseeded_document_404(self) -> None:
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/spaces/{SPACE}/documents/NoSuchDoc/attachments",
                resource={"data": [_attachment_entry("a.png")]},
                files=[("a.png", b"x")],
            )
        )
        assert response.status_code == 404

    def test_post_doc_attachments_duplicate_filename_409(self) -> None:
        # Live-verified 2026-07-20: same fileName = 409, batch atomic.
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments",
                resource={
                    "data": [
                        _attachment_entry("fresh.png"),
                        _attachment_entry(DOC_ATTACHMENT_ID),
                    ]
                },
                files=[("fresh.png", b"x"), (DOC_ATTACHMENT_ID, b"y")],
            )
        )
        assert response.status_code == 409
        assert "already exists" in _json(response)["errors"][0]["detail"]

    def test_post_doc_attachments_json_body_415(self) -> None:
        # Live #198: JSON body instead of multipart = 415.
        fake = FakePolarion()
        response = _mutate(
            fake,
            "POST",
            f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments",
            {"data": []},
        )
        assert response.status_code == 415

    def test_post_doc_attachments_missing_resource_400(self) -> None:
        # Live #198 wording: "Resource data not found in request."
        fake = FakePolarion()
        request = httpx.Request(
            "POST",
            f"{_BASE}/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments",
            files=[("files", ("a.png", b"x", "application/octet-stream"))],
        )
        response = fake._dispatch(request)
        assert response.status_code == 400
        assert "Resource data" in _json(response)["errors"][0]["detail"]

    def test_post_doc_attachments_unparsable_resource_400(self) -> None:
        fake = FakePolarion()
        request = httpx.Request(
            "POST",
            f"{_BASE}/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments",
            data={"resource": "{not json"},
            files=[("files", ("a.png", b"x", "application/octet-stream"))],
        )
        response = fake._dispatch(request)
        assert response.status_code == 400

    def test_post_doc_attachments_file_count_mismatch_400(self) -> None:
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/spaces/{SPACE}/documents/{DOC}/attachments",
                resource={
                    "data": [_attachment_entry("a.png"), _attachment_entry("b.png")]
                },
                files=[("a.png", b"x")],
            )
        )
        assert response.status_code == 400
        assert "File data" in _json(response)["errors"][0]["detail"]

    def test_post_workitem_attachments_echoes_ordered_counter_ids(self) -> None:
        # Seed already carries 1-fake-screenshot.png -- counter continues.
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
                resource={
                    "data": [
                        _attachment_entry(
                            "new-diagram.png", resource_type="workitem_attachments"
                        ),
                        _attachment_entry(
                            "new-photo.png", resource_type="workitem_attachments"
                        ),
                    ]
                },
                files=[("new-diagram.png", b"png-a"), ("new-photo.png", b"png-b")],
            )
        )
        assert response.status_code == 201
        data = _json(response)["data"]
        assert [e["id"] for e in data] == [
            f"{PROJECT}/{FLOATING_TASK_ID}/2-new-diagram.png",
            f"{PROJECT}/{FLOATING_TASK_ID}/3-new-photo.png",
        ]
        assert all(e["type"] == "workitem_attachments" for e in data)
        assert all("links" in e and "attributes" not in e for e in data)

    def test_post_workitem_attachments_duplicate_filename_both_succeed(self) -> None:
        # Divergence from doc sibling: no 409 -- fresh counter id each time.
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
                resource={
                    "data": [
                        _attachment_entry(
                            WORKITEM_ATTACHMENT_ID,
                            resource_type="workitem_attachments",
                        ),
                        _attachment_entry(
                            WORKITEM_ATTACHMENT_ID,
                            resource_type="workitem_attachments",
                        ),
                    ]
                },
                files=[(WORKITEM_ATTACHMENT_ID, b"x"), (WORKITEM_ATTACHMENT_ID, b"y")],
            )
        )
        assert response.status_code == 201
        ids = [e["id"] for e in _json(response)["data"]]
        assert ids == [
            f"{PROJECT}/{FLOATING_TASK_ID}/2-{WORKITEM_ATTACHMENT_ID}",
            f"{PROJECT}/{FLOATING_TASK_ID}/3-{WORKITEM_ATTACHMENT_ID}",
        ]
        assert len(set(ids)) == 2

    def test_post_workitem_attachments_visible_in_subsequent_list(self) -> None:
        fake = FakePolarion()
        fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
                resource={
                    "data": [
                        _attachment_entry(
                            "new-diagram.png", resource_type="workitem_attachments"
                        )
                    ]
                },
                files=[("new-diagram.png", b"png-a")],
            )
        )
        response = _get(
            fake, f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments"
        )
        ids = {e["attributes"]["id"] for e in _json(response)["data"]}
        assert ids == {WORKITEM_ATTACHMENT_ID, "2-new-diagram.png"}

    def test_post_workitem_attachments_title_served_in_list(self) -> None:
        # Live: title settable at POST, served on explicit fields.
        fake = FakePolarion()
        fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
                resource={
                    "data": [
                        _attachment_entry(
                            "new-diagram.png",
                            resource_type="workitem_attachments",
                            title="Wiring Diagram",
                        )
                    ]
                },
                files=[("new-diagram.png", b"png-a")],
            )
        )
        response = _get(
            fake, f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments"
        )
        titles = {
            e["attributes"]["id"]: e["attributes"]["title"]
            for e in _json(response)["data"]
        }
        assert titles["2-new-diagram.png"] == "Wiring Diagram"

    def test_post_workitem_attachments_content_served_after_create(self) -> None:
        # Created attachment must be fetchable on content route, not 404.
        fake = FakePolarion()
        fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
                resource={
                    "data": [
                        _attachment_entry(
                            "new-diagram.png", resource_type="workitem_attachments"
                        )
                    ]
                },
                files=[("new-diagram.png", b"png-a")],
            )
        )
        response = _get(
            fake,
            f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments/"
            "2-new-diagram.png/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 200
        assert response.content == WORKITEM_ATTACHMENT_CONTENT

    def test_post_workitem_attachments_unseeded_work_item_404(self) -> None:
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/workitems/MCPT-9999/attachments",
                resource={
                    "data": [
                        _attachment_entry("a.png", resource_type="workitem_attachments")
                    ]
                },
                files=[("a.png", b"x")],
            )
        )
        assert response.status_code == 404

    def test_post_workitem_attachments_json_body_415(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake,
            "POST",
            f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
            {"data": []},
        )
        assert response.status_code == 415

    def test_post_workitem_attachments_missing_resource_400(self) -> None:
        fake = FakePolarion()
        request = httpx.Request(
            "POST",
            f"{_BASE}/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
            files=[("files", ("a.png", b"x", "application/octet-stream"))],
        )
        response = fake._dispatch(request)
        assert response.status_code == 400
        assert "Resource data" in _json(response)["errors"][0]["detail"]

    def test_post_workitem_attachments_unparsable_resource_400(self) -> None:
        fake = FakePolarion()
        request = httpx.Request(
            "POST",
            f"{_BASE}/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
            data={"resource": "{not json"},
            files=[("files", ("a.png", b"x", "application/octet-stream"))],
        )
        response = fake._dispatch(request)
        assert response.status_code == 400

    def test_post_workitem_attachments_file_count_mismatch_400(self) -> None:
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/workitems/{FLOATING_TASK_ID}/attachments",
                resource={
                    "data": [
                        _attachment_entry(
                            "a.png", resource_type="workitem_attachments"
                        ),
                        _attachment_entry(
                            "b.png", resource_type="workitem_attachments"
                        ),
                    ]
                },
                files=[("a.png", b"x")],
            )
        )
        assert response.status_code == 400
        assert "File data" in _json(response)["errors"][0]["detail"]

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


class TestTestRecordMutations:
    _RECORD_ID = f"{PROJECT}/{TEST_RUN_ID}/{PROJECT}/{TESTCASE_ID}/0"
    _UNKNOWN_RECORD_ID = f"{PROJECT}/{TEST_RUN_ID}/{PROJECT}/MCPT-9999/0"

    def test_patch_known_id_returns_204_and_records_mutation(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake,
            "PATCH",
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}/testrecords",
            {
                "data": [
                    {
                        "type": "testrecords",
                        "id": self._RECORD_ID,
                        "attributes": {"result": "passed"},
                    }
                ]
            },
        )
        assert response.status_code == 204
        assert fake.mutations[-1]["path"].endswith("/testrecords")

    def test_patch_unknown_id_is_400(self) -> None:
        fake = FakePolarion()
        response = _mutate(
            fake,
            "PATCH",
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}/testrecords",
            {
                "data": [
                    {
                        "type": "testrecords",
                        "id": self._UNKNOWN_RECORD_ID,
                        "attributes": {"result": "passed"},
                    }
                ]
            },
        )
        assert response.status_code == 400
        detail = _json(response)["errors"][0]["detail"]
        assert self._UNKNOWN_RECORD_ID in detail
        assert "was not found" in detail

    def test_patch_mixed_batch_rejects_whole_batch(self) -> None:
        # One bad id in a multi-item batch still 400s -- atomic, live-verified.
        fake = FakePolarion()
        response = _mutate(
            fake,
            "PATCH",
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}/testrecords",
            {
                "data": [
                    {"id": self._RECORD_ID, "attributes": {"result": "passed"}},
                    {"id": self._UNKNOWN_RECORD_ID, "attributes": {"result": "failed"}},
                ]
            },
        )
        assert response.status_code == 400

    def test_patch_higher_iteration_record_is_204(self) -> None:
        # iterations=3 seed -- every seeded iteration id patchable.
        fake = FakePolarion()
        response = _mutate(
            fake,
            "PATCH",
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID_2}/testrecords",
            {
                "data": [
                    {
                        "type": "testrecords",
                        "id": f"{PROJECT}/{TEST_RUN_ID_2}/{PROJECT}/{TESTCASE_ID}/2",
                        "attributes": {"result": "passed"},
                    }
                ]
            },
        )
        assert response.status_code == 204

    def test_patch_other_runs_record_is_400(self) -> None:
        # Record id valid for TEST_RUN_ID -- PATCH via TEST_RUN_ID_2 path 400s.
        fake = FakePolarion()
        response = _mutate(
            fake,
            "PATCH",
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID_2}/testrecords",
            {
                "data": [
                    {
                        "type": "testrecords",
                        "id": self._RECORD_ID,
                        "attributes": {"result": "passed"},
                    }
                ]
            },
        )
        assert response.status_code == 400
        assert "was not found" in _json(response)["errors"][0]["detail"]


class TestTestRecordAttachmentMutations:
    _PATH = (
        f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
        f"/testrecords/{PROJECT}/{TESTCASE_ID}/0/attachments"
    )

    def test_post_echoes_ordered_ids_with_test_case_prefix(self) -> None:
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                self._PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            "report.txt", resource_type="testrecord_attachments"
                        ),
                        _attachment_entry(
                            "log.txt", resource_type="testrecord_attachments"
                        ),
                    ]
                },
                files=[("report.txt", b"a"), ("log.txt", b"b")],
            )
        )
        assert response.status_code == 201
        data = _json(response)["data"]
        # Live shape 2026-07-21: list, input order, type/id/links only.
        assert [e["id"] for e in data] == [
            f"{PROJECT}/{TEST_RUN_ID}/{PROJECT}/{TESTCASE_ID}/0/"
            f"{TESTCASE_ID}_report.txt",
            f"{PROJECT}/{TEST_RUN_ID}/{PROJECT}/{TESTCASE_ID}/0/{TESTCASE_ID}_log.txt",
        ]
        assert all(e["type"] == "testrecord_attachments" for e in data)
        assert all("links" in e and "attributes" not in e for e in data)

    def test_post_unseeded_run_is_404(self) -> None:
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/testruns/Nope"
                f"/testrecords/{PROJECT}/{TESTCASE_ID}/0/attachments",
                resource={
                    "data": [
                        _attachment_entry(
                            "a.txt", resource_type="testrecord_attachments"
                        )
                    ]
                },
                files=[("a.txt", b"x")],
            )
        )
        assert response.status_code == 404

    def test_post_iteration_beyond_seeded_count_is_404(self) -> None:
        # TEST_RUN_ID seed iterations=1 -- only iteration 0 exists.
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
                f"/testrecords/{PROJECT}/{TESTCASE_ID}/1/attachments",
                resource={
                    "data": [
                        _attachment_entry(
                            "a.txt", resource_type="testrecord_attachments"
                        )
                    ]
                },
                files=[("a.txt", b"x")],
            )
        )
        assert response.status_code == 404

    def test_post_wrong_test_case_is_404(self) -> None:
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
                f"/testrecords/{PROJECT}/MCPT-9999/0/attachments",
                resource={
                    "data": [
                        _attachment_entry(
                            "a.txt", resource_type="testrecord_attachments"
                        )
                    ]
                },
                files=[("a.txt", b"x")],
            )
        )
        assert response.status_code == 404

    def test_post_duplicate_filename_in_batch_is_409(self) -> None:
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                self._PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            "a.txt", resource_type="testrecord_attachments"
                        ),
                        _attachment_entry(
                            "a.txt", resource_type="testrecord_attachments"
                        ),
                    ]
                },
                files=[("a.txt", b"x"), ("a.txt", b"y")],
            )
        )
        assert response.status_code == 409
        assert "already exists" in _json(response)["errors"][0]["detail"]

    def test_post_cross_call_duplicate_is_409(self) -> None:
        fake = FakePolarion()
        first = fake._dispatch(
            _multipart_attachments_request(
                self._PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            "a.txt", resource_type="testrecord_attachments"
                        )
                    ]
                },
                files=[("a.txt", b"x")],
            )
        )
        assert first.status_code == 201
        second = fake._dispatch(
            _multipart_attachments_request(
                self._PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            "a.txt", resource_type="testrecord_attachments"
                        )
                    ]
                },
                files=[("a.txt", b"y")],
            )
        )
        assert second.status_code == 409

    def test_post_conflicting_batch_records_nothing_fresh_name_still_succeeds(
        self,
    ) -> None:
        # Atomic: 409 batch (fresh + dup) records nothing -- fresh name alone
        # must still succeed on retry.
        fake = FakePolarion()
        seeded = fake._dispatch(
            _multipart_attachments_request(
                self._PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            "existing.txt", resource_type="testrecord_attachments"
                        )
                    ]
                },
                files=[("existing.txt", b"x")],
            )
        )
        assert seeded.status_code == 201

        conflicting = fake._dispatch(
            _multipart_attachments_request(
                self._PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            "fresh.txt", resource_type="testrecord_attachments"
                        ),
                        _attachment_entry(
                            "existing.txt", resource_type="testrecord_attachments"
                        ),
                    ]
                },
                files=[("fresh.txt", b"y"), ("existing.txt", b"z")],
            )
        )
        assert conflicting.status_code == 409

        retry = fake._dispatch(
            _multipart_attachments_request(
                self._PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            "fresh.txt", resource_type="testrecord_attachments"
                        )
                    ]
                },
                files=[("fresh.txt", b"y")],
            )
        )
        assert retry.status_code == 201

    def test_post_duplicate_of_seeded_filename_is_409(self) -> None:
        # TEST_RUN_ID iteration 0 seed carry RECORD_ATTACHMENT_ID.
        # Dup check span seed union prior POSTs.
        fake = FakePolarion()
        seed_file_name = RECORD_ATTACHMENT_ID.split("_", 1)[1]
        response = fake._dispatch(
            _multipart_attachments_request(
                self._PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            seed_file_name, resource_type="testrecord_attachments"
                        )
                    ]
                },
                files=[(seed_file_name, b"x")],
            )
        )
        assert response.status_code == 409
        assert "already exists" in _json(response)["errors"][0]["detail"]

    def test_post_json_body_415(self) -> None:
        fake = FakePolarion()
        response = _mutate(fake, "POST", self._PATH, {"data": []})
        assert response.status_code == 415

    def test_post_missing_resource_400(self) -> None:
        fake = FakePolarion()
        request = httpx.Request(
            "POST",
            f"{_BASE}{self._PATH}",
            files=[("files", ("a.txt", b"x", "application/octet-stream"))],
        )
        response = fake._dispatch(request)
        assert response.status_code == 400
        assert "Resource data" in _json(response)["errors"][0]["detail"]

    def test_post_file_count_mismatch_400(self) -> None:
        fake = FakePolarion()
        response = fake._dispatch(
            _multipart_attachments_request(
                self._PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            "a.txt", resource_type="testrecord_attachments"
                        ),
                        _attachment_entry(
                            "b.txt", resource_type="testrecord_attachments"
                        ),
                    ]
                },
                files=[("a.txt", b"x")],
            )
        )
        assert response.status_code == 400
        assert "File data" in _json(response)["errors"][0]["detail"]


class TestTestRecordAttachmentReads:
    """Content route + seed-union-created behavior; list routing basics live
    in TestTestRecordAttachmentsRouting.
    """

    _COLLECTION_PATH = (
        f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
        f"/testrecords/{PROJECT}/{TESTCASE_ID}/0/attachments"
    )
    _CONTENT_PATH = f"{_COLLECTION_PATH}/{RECORD_IMAGE_ATTACHMENT_ID}/content"

    def test_list_relationships_author_only(self) -> None:
        # Sparse fieldset drop project rel -- mock must not ship it.
        response = _get(FakePolarion(), self._COLLECTION_PATH)
        entry = _json(response)["data"][0]
        assert sorted(entry["relationships"]) == ["author"]

    def test_created_upload_appears_in_subsequent_list(self) -> None:
        fake = FakePolarion()
        fake._dispatch(
            _multipart_attachments_request(
                self._COLLECTION_PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            "new-log.txt", resource_type="testrecord_attachments"
                        )
                    ]
                },
                files=[("new-log.txt", b"x")],
            )
        )
        response = _get(fake, self._COLLECTION_PATH)
        ids = {e["attributes"]["id"] for e in _json(response)["data"]}
        assert ids == {
            RECORD_ATTACHMENT_ID,
            RECORD_IMAGE_ATTACHMENT_ID,
            f"{TESTCASE_ID}_new-log.txt",
        }

    def test_content_serves_seeded_bytes(self) -> None:
        response = _get(FakePolarion(), self._CONTENT_PATH, headers=_BYTES_ACCEPT)
        assert response.status_code == 200
        assert response.content == RECORD_IMAGE_ATTACHMENT_CONTENT

    def test_content_served_after_create(self) -> None:
        fake = FakePolarion()
        fake._dispatch(
            _multipart_attachments_request(
                self._COLLECTION_PATH,
                resource={
                    "data": [
                        _attachment_entry(
                            "new-log.txt", resource_type="testrecord_attachments"
                        )
                    ]
                },
                files=[("new-log.txt", b"x")],
            )
        )
        response = _get(
            fake,
            f"{self._COLLECTION_PATH}/{TESTCASE_ID}_new-log.txt/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 200
        assert response.content == RECORD_IMAGE_ATTACHMENT_CONTENT

    def test_content_json_only_accept_is_406(self) -> None:
        response = _get(
            FakePolarion(), self._CONTENT_PATH, headers={"Accept": "application/json"}
        )
        assert response.status_code == 406
        assert _json(response)["errors"]

    def test_content_unseeded_attachment_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"{self._COLLECTION_PATH}/999-not-real.txt/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 404

    def test_content_unknown_test_case_is_404(self) -> None:
        response = _get(
            FakePolarion(),
            f"/projects/{PROJECT}/testruns/{TEST_RUN_ID}"
            f"/testrecords/{PROJECT}/MCPT-9999/0/attachments/"
            f"{RECORD_IMAGE_ATTACHMENT_ID}/content",
            headers=_BYTES_ACCEPT,
        )
        assert response.status_code == 404


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
