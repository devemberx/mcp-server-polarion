"""Attachment-ref guard core: scheme extraction, create-time reject,
update-time id-set verify, live id fetch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.exceptions import PolarionAuthError, PolarionError
from mcp_server_polarion.tools._shared.guard._attachment_refs import (
    _append_ref,
    check_refs_against_ids,
    extract_scheme_refs,
    fetch_attachment_ids,
    reject_any_scheme_refs,
)
from mcp_server_polarion.tools._shared.guard._http import GUARD_PAGE_SIZE
from tests.mcp_server_polarion.tools._shared.guard._builders import attachments_response


class TestExtractSchemeRefs:
    def test_img_and_a_mixed(self) -> None:
        html = (
            '<p><img src="attachment:1-file.txt"/>'
            '<a href="workitemimg:2-diagram.svg">link</a></p>'
        )

        assert extract_scheme_refs(html) == [
            ("attachment", "1-file.txt"),
            ("workitemimg", "2-diagram.svg"),
        ]

    def test_case_insensitive_prefix(self) -> None:
        html = '<img src="Attachment:1-x.png"/><a href="WORKITEMIMG:2-y.svg">l</a>'

        assert extract_scheme_refs(html) == [
            ("attachment", "1-x.png"),
            ("workitemimg", "2-y.svg"),
        ]

    def test_url_encoded_token_preserved_raw(self) -> None:
        html = '<img src="workitemimg:1-test%20file.txt"/>'

        assert extract_scheme_refs(html) == [("workitemimg", "1-test%20file.txt")]

    def test_non_image_extension_extracted(self) -> None:
        html = '<img src="attachment:1-slides.pptx"/>'

        assert extract_scheme_refs(html) == [("attachment", "1-slides.pptx")]

    def test_empty_token_extracted(self) -> None:
        html = '<img src="attachment:"/>'

        assert extract_scheme_refs(html) == [("attachment", "")]

    def test_plain_http_and_relative_src_ignored(self) -> None:
        html = '<img src="https://example.com/x.png"/><a href="/relative/path">l</a>'

        assert extract_scheme_refs(html) == []

    def test_non_ref_html_returns_empty_list(self) -> None:
        assert extract_scheme_refs("<p>hello <b>world</b></p>") == []
        assert extract_scheme_refs("") == []

    def test_malformed_html_tolerated(self) -> None:
        html = '<p><img src="attachment:1-x.png"><div>unclosed'

        assert extract_scheme_refs(html) == [("attachment", "1-x.png")]

    def test_append_ref_ignores_non_str_value(self) -> None:
        # BeautifulSoup may hand back non-str attr value (multi-valued
        # attribute = list) -- must no-op, not crash.
        refs: list[tuple[str, str]] = []
        _append_ref(["attachment:1-x.png"], refs)
        assert refs == []


class TestRejectAnySchemeRefs:
    def test_attachment_scheme_rejects(self) -> None:
        with pytest.raises(ValueError, match="created"):
            reject_any_scheme_refs(['<img src="attachment:1-x.png"/>'], "document")

    def test_workitemimg_scheme_rejects(self) -> None:
        with pytest.raises(ValueError, match="created"):
            reject_any_scheme_refs(['<img src="workitemimg:1-x.png"/>'], "work item")

    def test_clean_htmls_pass(self) -> None:
        reject_any_scheme_refs(["<p>hello</p>", ""], "document")  # must not raise

    def test_message_has_no_upload_tool_names(self) -> None:
        with pytest.raises(ValueError) as exc:
            reject_any_scheme_refs(['<img src="attachment:1-x.png"/>'], "document")

        msg = str(exc.value)
        assert "create_document_attachments" not in msg
        assert "create_work_item_attachments" not in msg


class TestCheckRefsAgainstIds:
    def test_raw_match_passes(self) -> None:
        check_refs_against_ids(
            [("attachment", "1-x.png")],
            frozenset({"1-x.png"}),
            expected_scheme="attachment",
            list_tool="list_document_attachments",
            what="Document 'S/D'",
        )  # must not raise

    def test_url_encoded_token_matches_raw_id_via_unquote(self) -> None:
        check_refs_against_ids(
            [("workitemimg", "1-test%20file.txt")],
            frozenset({"1-test file.txt"}),
            expected_scheme="workitemimg",
            list_tool="list_work_item_attachments",
            what="Work item 'WI-1'",
        )  # must not raise

    def test_wrong_scheme_rejects_document_direction(self) -> None:
        with pytest.raises(ValueError, match="attachment") as exc:
            check_refs_against_ids(
                [("workitemimg", "1-x.png")],
                frozenset(),
                expected_scheme="attachment",
                list_tool="list_document_attachments",
                what="Document 'S/D'",
            )

        assert "workitemimg" in str(exc.value)

    def test_wrong_scheme_rejects_work_item_direction(self) -> None:
        with pytest.raises(ValueError, match="workitemimg") as exc:
            check_refs_against_ids(
                [("attachment", "1-x.png")],
                frozenset(),
                expected_scheme="workitemimg",
                list_tool="list_work_item_attachments",
                what="Work item 'WI-1'",
            )

        assert "attachment" in str(exc.value)

    def test_empty_token_rejects(self) -> None:
        with pytest.raises(ValueError, match="list_document_attachments"):
            check_refs_against_ids(
                [("attachment", "")],
                frozenset({"1-x.png"}),
                expected_scheme="attachment",
                list_tool="list_document_attachments",
                what="Document 'S/D'",
            )

    def test_unmatched_tokens_lists_tokens_and_names_list_tool(self) -> None:
        with pytest.raises(ValueError) as exc:
            check_refs_against_ids(
                [("attachment", "1-ghost.png")],
                frozenset({"1-real.png"}),
                expected_scheme="attachment",
                list_tool="list_document_attachments",
                what="Document 'S/D'",
            )

        msg = str(exc.value)
        assert "1-ghost.png" in msg
        assert "list_document_attachments" in msg

    def test_empty_valid_ids_with_refs_rejects(self) -> None:
        with pytest.raises(ValueError, match="1-x") as exc:
            check_refs_against_ids(
                [("attachment", "1-x.png")],
                frozenset(),
                expected_scheme="attachment",
                list_tool="list_document_attachments",
                what="Document 'S/D'",
            )

        assert "1-x.png" in str(exc.value)


class TestFetchAttachmentIds:
    async def test_ids_collected_across_pages(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = [
            attachments_response(
                [f"{n}-x.png" for n in range(GUARD_PAGE_SIZE)], meta=False
            ),
            attachments_response(["last-y.png"], meta=False),
        ]

        ids = await fetch_attachment_ids(
            mock_client, "/p", "document_attachments", what="w", project_id="P"
        )

        assert len(ids) == GUARD_PAGE_SIZE + 1
        assert "last-y.png" in ids

    async def test_uses_basic_fieldset_param(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = attachments_response([], meta=False)

        await fetch_attachment_ids(
            mock_client, "/p", "document_attachments", what="w", project_id="P"
        )

        params = mock_client.get.call_args.kwargs["params"]
        assert params["fields[document_attachments]"] == "@basic"

    async def test_auth_error_becomes_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await fetch_attachment_ids(
                mock_client, "/p", "document_attachments", what="w", project_id="P"
            )

    async def test_generic_error_becomes_runtime_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down", status_code=502)

        with pytest.raises(RuntimeError, match="backend down"):
            await fetch_attachment_ids(
                mock_client, "/p", "workitem_attachments", what="w", project_id="P"
            )

    async def test_attributes_less_entries_skipped(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {"type": "attachments", "id": "no-attrs"},
                {"type": "attachments", "id": "y", "attributes": {"id": "y-real.png"}},
            ]
        }

        ids = await fetch_attachment_ids(
            mock_client, "/p", "document_attachments", what="w", project_id="P"
        )

        assert ids == frozenset({"y-real.png"})

    async def test_non_dict_entries_skipped(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {
            "data": [
                "not-a-dict",
                {"type": "attachments", "id": "y", "attributes": {"id": "y-real.png"}},
            ]
        }

        ids = await fetch_attachment_ids(
            mock_client, "/p", "document_attachments", what="w", project_id="P"
        )

        assert ids == frozenset({"y-real.png"})
